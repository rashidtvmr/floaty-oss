"""Capture-exclusion strategy for the Linux floating overlay.

Pure standard library: importable with no GTK, no X11 and no D-Bus. The GTK
layer (``floating_overlay.py``) and the QA probe (``security-lab/overlay_probe.py``)
both consume this module.

Honest scope: Linux has no public API equivalent to Windows
``SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`` or macOS
``NSWindow.sharingType = .none``. Window hints below are best effort. The only
mechanism that can honestly keep a panel out of a full-screen capture is the
behavioural guard in :class:`ScreenShareWatcher`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

SESSION_X11 = "x11"
SESSION_WAYLAND = "wayland"
SESSION_UNKNOWN = "unknown"

# EWMH state atoms that keep an unmanaged panel out of window pickers.
_X11_STATES = (
    "_NET_WM_STATE_ABOVE",
    "_NET_WM_STATE_STAYS_ON_TOP",
    "_NET_WM_STATE_SKIP_TASKBAR",
    "_NET_WM_STATE_SKIP_PAGER",
)
_SKIP_CAPTURE = "_NET_WM_STATE_SKIP_CAPTURE"

# Compositors that can exclude a window from a screencast when the user has
# configured a matching rule (niri: ``block-out-from=screencast``).
_COMPOSITOR_RULE_HINT = {
    "niri": "add a niri window rule: block-out-from=screencast",
}

_PROBE_TIMEOUT = 0.4


class ProbeUnavailable(RuntimeError):
    """Raised when no supported screen-share probe is installed."""


@dataclass(frozen=True)
class SessionInfo:
    """Best-effort description of the current Linux session."""

    session_type: str
    desktop: str
    compositor: str
    has_x11_display: bool
    has_wayland_display: bool


@dataclass(frozen=True)
class ExclusionPlan:
    """What the overlay should try in order to stay out of a capture."""

    strategy: str
    guaranteed: bool
    override_redirect: bool
    keep_above: bool
    skip_taskbar: bool
    window_type: str
    atoms: Mapping[str, tuple]
    layer: str
    notes: tuple = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "guaranteed": self.guaranteed,
            "override_redirect": self.override_redirect,
            "keep_above": self.keep_above,
            "skip_taskbar": self.skip_taskbar,
            "window_type": self.window_type,
            "atoms": {name: list(values) for name, values in self.atoms.items()},
            "layer": self.layer,
            "notes": list(self.notes),
        }


def _compositor(desktop: str, session_type: str) -> str:
    name = desktop.lower()
    for token, compositor in (
        ("niri", "niri"),
        ("gnome", "gnome"),
        ("unity", "gnome"),
        ("plasma", "kde"),
        ("kde", "kde"),
        ("sway", "sway"),
        ("hyprland", "hyprland"),
        ("wlroots", "wlroots"),
    ):
        if token in name:
            return compositor
    if session_type == SESSION_X11:
        return "x11"
    return "unknown"


def detect_session(environ: Optional[Mapping[str, str]] = None) -> SessionInfo:
    """Classify the session from the environment. Never raises."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    desktop = (env.get("XDG_CURRENT_DESKTOP") or env.get("XDG_SESSION_DESKTOP") or "").lower()
    has_wayland = bool(env.get("WAYLAND_DISPLAY"))
    has_x11 = bool(env.get("DISPLAY"))
    declared = (env.get("XDG_SESSION_TYPE") or "").lower()
    if declared == "wayland" or has_wayland:
        session_type = SESSION_WAYLAND
    elif declared == "x11" or has_x11:
        session_type = SESSION_X11
    else:
        session_type = SESSION_UNKNOWN
    return SessionInfo(
        session_type=session_type,
        desktop=desktop,
        compositor=_compositor(desktop, session_type),
        has_x11_display=has_x11,
        has_wayland_display=has_wayland,
    )


def build_plan(session: SessionInfo, *, compositor_skip_capture: bool = False) -> ExclusionPlan:
    """Return the best-effort exclusion plan for ``session``.

    ``compositor_skip_capture`` means the user has *already configured* the
    compositor to exclude this window (for example a niri
    ``block-out-from=screencast`` rule). Only then is exclusion reported as
    guaranteed; the application cannot set that up by itself.
    """
    if session.session_type == SESSION_X11:
        states = list(_X11_STATES)
        notes = [
            "X11 has no per-window exclusion from a full-screen XGetImage capture.",
            "Override-redirect and skip-taskbar keep the panel out of most window pickers.",
        ]
        if compositor_skip_capture:
            states.append(_SKIP_CAPTURE)
            notes.append("Compositor was configured to honour _NET_WM_STATE_SKIP_CAPTURE.")
        else:
            notes.append(
                "Set compositor_skip_capture=True only when the compositor honours "
                "_NET_WM_STATE_SKIP_CAPTURE."
            )
        return ExclusionPlan(
            strategy="x11-override-redirect",
            guaranteed=bool(compositor_skip_capture),
            override_redirect=True,
            keep_above=True,
            skip_taskbar=True,
            window_type="_NET_WM_WINDOW_TYPE_NOTIFICATION",
            atoms={"_NET_WM_STATE": tuple(states)},
            layer="",
            notes=tuple(notes),
        )

    if session.session_type == SESSION_WAYLAND:
        notes = [
            "A Wayland portal screencast captures whole outputs; a toplevel cannot opt out.",
            "The behavioural auto-hide is the only reliable mitigation otherwise.",
        ]
        if compositor_skip_capture:
            notes.append("Compositor exclusion rule is configured for this window.")
        else:
            hint = _COMPOSITOR_RULE_HINT.get(session.compositor)
            if hint:
                notes.append(f"On {session.compositor}, {hint}.")
        return ExclusionPlan(
            strategy="wayland-layer-shell",
            guaranteed=bool(compositor_skip_capture),
            override_redirect=False,
            keep_above=True,
            skip_taskbar=True,
            window_type="",
            atoms={},
            layer="overlay",
            notes=tuple(notes),
        )

    return ExclusionPlan(
        strategy="none",
        guaranteed=False,
        override_redirect=False,
        keep_above=False,
        skip_taskbar=False,
        window_type="",
        atoms={},
        layer="",
        notes=("Unknown session type; capture exclusion is unavailable.",),
    )


def capability_report(session: SessionInfo) -> dict:
    """Honest, human-readable verdict for the current session."""
    plan = build_plan(session)
    limitations = [
        "Linux has no per-window exclusion API equivalent to Windows WDA_EXCLUDEFROMCAPTURE.",
        "X11 clients with display access can still read the window with XGetImage.",
        "Wayland portal screencast captures whole outputs, not individual toplevels.",
        "Window hints are best effort and compositor dependent.",
    ]
    if plan.guaranteed:
        mechanism = (
            "compositor exclusion rule configured for this window "
            f"({session.compositor}); behavioural auto-hide still active"
        )
    elif session.session_type == SESSION_X11:
        mechanism = "override-redirect + EWMH skip hints; behavioural auto-hide"
    elif session.session_type == SESSION_WAYLAND:
        mechanism = "layer-shell overlay hints; behavioural auto-hide"
    else:
        mechanism = "behavioural auto-hide only"
    recommended = (
        "Enable the behavioural guard (auto-hide while a screen share is detected). "
        "It is the only mechanism that reliably keeps the panel out of a full-screen capture."
    )
    hint = _COMPOSITOR_RULE_HINT.get(session.compositor)
    if hint and not plan.guaranteed:
        recommended = f"On {session.compositor}, {hint}. Otherwise {recommended[0].lower()}{recommended[1:]}"
    return {
        "session": session.session_type,
        "compositor": session.compositor,
        "guaranteed": plan.guaranteed,
        "mechanism": mechanism,
        "limitations": limitations,
        "recommended": recommended,
    }


def should_hide(plan: Optional[ExclusionPlan], share_active: bool, user_hidden: bool) -> bool:
    """Pure visibility rule.

    A manually hidden panel stays hidden. Otherwise the panel hides whenever a
    screen share is detected, even on a "guaranteed" plan, because a compositor
    rule may not cover every capture method.
    """
    if user_hidden:
        return True
    return bool(share_active)


def _forced_state() -> Optional[bool]:
    """QA/test override: CONTEXT_LENS_SCREEN_SHARE=1|0 forces the probe result."""
    value = (os.environ.get("CONTEXT_LENS_SCREEN_SHARE") or "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return None


def _pipewire_screencast_active() -> Optional[bool]:
    """Return True/False when PipeWire can tell us, None when it cannot."""
    tool = shutil.which("pw-dump")
    if not tool:
        return None
    try:
        result = subprocess.run(
            [tool], capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        nodes = json.loads(result.stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if not isinstance(node, dict):
            continue
        info = node.get("info") if isinstance(node.get("info"), dict) else {}
        props = info.get("props") if isinstance(info.get("props"), dict) else {}
        media_class = str(props.get("media.class", ""))
        if media_class != "Stream/Output/Video":
            continue
        haystack = " ".join(
            str(props.get(key, "")).lower()
            for key in ("node.name", "node.description", "application.name", "media.name")
        )
        if any(token in haystack for token in ("screencast", "screen-cast", "screen cast", "capture")):
            return True
    return False


def default_probe() -> bool:
    """Best-effort screen-share detection.

    Returns True when a screencast is detected. Raises :class:`ProbeUnavailable`
    when no supported probe is installed, so callers can report
    ``checked=False`` instead of pretending the screen is clean.
    """
    forced = _forced_state()
    if forced is not None:
        return forced
    detected = _pipewire_screencast_active()
    if detected is None:
        raise ProbeUnavailable("install PipeWire (pw-dump) to detect screen shares")
    return detected


class ScreenShareWatcher:
    """Polls a probe and remembers the last result for the UI."""

    def __init__(self, probe: Optional[Callable[[], bool]] = None, interval: float = 2.0):
        self._probe = probe if probe is not None else default_probe
        self.interval = float(interval)
        self._active = False
        self._checked = False
        self._detail = "not polled yet"

    def poll(self) -> bool:
        try:
            self._active = bool(self._probe())
            self._checked = True
            self._detail = "screencast detected" if self._active else "no screencast detected"
        except Exception as exc:  # noqa: BLE001 - a probe failure must never crash the UI
            self._active = False
            self._checked = False
            self._detail = f"probe unavailable: {type(exc).__name__}"
        return self._active

    def status(self) -> dict:
        return {"active": self._active, "checked": self._checked, "detail": self._detail}
