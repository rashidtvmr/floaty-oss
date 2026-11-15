"""Best-effort capture-exclusion strategy for the Linux floating overlay.

Pure standard library: importable with no GTK, no X11 and no D-Bus. The GTK
layer (``floating_overlay.py``) and the QA probe (``security-lab/overlay_probe.py``)
both consume this module.

Honest scope: Linux has no public API equivalent to Windows
``SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`` or macOS
``NSWindow.sharingType = .none``. Window hints below are best effort. Three
Wayland compositors document a per-window exclusion *rule* the user can
install (niri, Hyprland, KWin 6.7+); :mod:`capture_rules` writes it.
GNOME Shell / mutter, X11 and sway have no such rule. The floating panel
remains visible during screen shares and may be captured.
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
# The dock layer is above normal and above fullscreen windows in mutter, and
# dock windows are not focusable. That is the only client-side way to keep the
# panel above a fullscreen window on GNOME Wayland.
_X11_DOCK_STATES = (
    "_NET_WM_STATE_ABOVE",
    "_NET_WM_STATE_SKIP_TASKBAR",
    "_NET_WM_STATE_SKIP_PAGER",
)
_SKIP_CAPTURE = "_NET_WM_STATE_SKIP_CAPTURE"

# Compositors that document a per-window capture-exclusion rule. The setup
# itself lives in capture_rules.py; these hints are for status/reporting only.
_COMPOSITOR_RULE_HINT = {
    "niri": 'add a niri window rule: block-out-from "screen-capture"',
    "hyprland": "add a Hyprland window rule: no_screen_share = true",
    "kde": "use KWin 6.7+ on Wayland: Window Rules → Hide from Screencast",
}
_RULE_COMPOSITORS = frozenset(_COMPOSITOR_RULE_HINT)

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


def prefer_x11_backend(session: SessionInfo) -> bool:
    """Return True when the panel should run through XWayland.

    GNOME Shell has no always-on-top protocol for Wayland clients and does not
    implement wlr-layer-shell. A Wayland toplevel is therefore covered by any
    focused or fullscreen window. mutter does honour the EWMH *dock* layer for
    X11 clients, and that layer stays above fullscreen windows without taking
    focus (verified on GNOME Shell 50 / mutter 18). KDE, niri and Hyprland are
    left on Wayland so their capture-exclusion rules keep applying.
    """
    return (
        session.session_type == SESSION_WAYLAND
        and session.compositor == "gnome"
        and session.has_x11_display
    )


def ensure_panel_backend(session: Optional[SessionInfo] = None) -> str:
    """Choose the GDK backend before GTK is imported and return the choice.

    An explicit ``GDK_BACKEND`` from the user always wins, including
    ``GDK_BACKEND=wayland`` to opt out of XWayland.
    """
    session = session or detect_session()
    explicit = (os.environ.get("GDK_BACKEND") or "").strip()
    if explicit:
        return explicit
    if prefer_x11_backend(session):
        os.environ["GDK_BACKEND"] = "x11"
    return os.environ.get("GDK_BACKEND", "")


def backend_is_x11(backend: Optional[str]) -> bool:
    """Return True when a ``GDK_BACKEND`` value selects X11."""
    parts = [part.strip().lower() for part in (backend or "").split(",")]
    return "x11" in parts


def display_is_x11(display_name: Optional[str]) -> bool:
    """Return True for a GDK display name that is not Wayland.

    GDK names Wayland displays ``wayland-0``; X11 displays are ``:0`` or
    ``:0.0``.
    """
    name = (display_name or "").strip().lower()
    return bool(name) and not name.startswith("wayland")


def build_plan(
    session: SessionInfo, *, compositor_skip_capture: bool = False,
    xwayland_dock: bool = False,
) -> ExclusionPlan:
    """Return the best-effort exclusion plan for ``session``.

    ``compositor_skip_capture`` means the user has *already configured* the
    compositor to exclude this window (for example a niri
    ``block-out-from=screencast`` rule). A configured rule is not evidence of
    successful runtime exclusion; no plan claims a capture guarantee.

    ``xwayland_dock`` requests the X11 dock-layer overlay used on GNOME
    Wayland, where a Wayland toplevel cannot stay above fullscreen windows.
    It changes stacking, not capture exclusion.
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
            guaranteed=False,
            override_redirect=True,
            keep_above=True,
            skip_taskbar=True,
            window_type="_NET_WM_WINDOW_TYPE_NOTIFICATION",
            atoms={"_NET_WM_STATE": tuple(states)},
            layer="",
            notes=tuple(notes),
        )

    if session.session_type == SESSION_WAYLAND:
        if xwayland_dock:
            notes = [
                "The panel runs through XWayland so mutter can place it in the dock layer.",
                "The dock layer stays above fullscreen windows and does not take focus.",
                "X11 has no per-window capture exclusion; a whole-screen share still includes the panel.",
            ]
            if compositor_skip_capture:
                notes.append("Compositor exclusion rule is configured for this window.")
            elif session.compositor == "gnome":
                notes.append(
                    "GNOME Shell has no per-window capture exclusion; share a "
                    "single application window instead of the whole screen."
                )
            return ExclusionPlan(
                strategy="xwayland-dock",
                guaranteed=False,
                override_redirect=False,
                keep_above=True,
                skip_taskbar=True,
                window_type="_NET_WM_WINDOW_TYPE_DOCK",
                atoms={"_NET_WM_STATE": tuple(_X11_DOCK_STATES)},
                layer="dock",
                notes=tuple(notes),
            )
        notes = [
            "A Wayland portal screencast captures whole outputs unless the "
            "compositor supports a per-window exclusion rule.",
            "SocketStream keeps the panel visible during screen shares; it may be captured.",
            "Wayland ignores client always-on-top and skip-taskbar hints; the "
            "compositor decides stacking. Use the window menu (GNOME: Always on "
            "Top) if the panel must stay above other windows.",
        ]
        if compositor_skip_capture:
            notes.append("Compositor exclusion rule is configured for this window.")
        else:
            hint = _COMPOSITOR_RULE_HINT.get(session.compositor)
            if hint:
                notes.append(f"On {session.compositor}, {hint}.")
            elif session.compositor == "gnome":
                notes.append(
                    "GNOME Shell has no per-window capture exclusion; share a "
                    "single application window instead of the whole screen."
                )
        return ExclusionPlan(
            strategy="wayland-toplevel",
            guaranteed=False,
            override_redirect=False,
            keep_above=False,
            skip_taskbar=False,
            window_type="",
            atoms={},
            layer="toplevel",
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


def capability_report(
    session: SessionInfo, *, compositor_skip_capture: bool = False,
    xwayland_dock: bool = False,
    capture_rule_supported: Optional[bool] = None,
) -> dict:
    """Honest, human-readable verdict for the current session.

    ``compositor_skip_capture`` means a compositor rule for this window is
    already installed; :mod:`capture_rules` is the only supported setup path.
    ``runtime_verified`` is always False here: proving exclusion needs a real
    screen share, which the application cannot perform on the user's behalf.
    """
    plan = build_plan(
        session, compositor_skip_capture=compositor_skip_capture,
        xwayland_dock=xwayland_dock,
    )
    compositor = session.compositor
    rule_backend = (
        compositor if compositor in _RULE_COMPOSITORS
        and session.session_type == SESSION_WAYLAND else ""
    )
    rule_supported = bool(rule_backend) and (
        capture_rule_supported if capture_rule_supported is not None else compositor != "kde"
    )
    limitations = [
        "Linux has no per-window exclusion API equivalent to Windows WDA_EXCLUDEFROMCAPTURE.",
        "X11 clients with display access can still read the window with XGetImage.",
        "A Wayland portal screencast captures whole outputs unless the compositor applies a per-window rule.",
        "Window hints are best effort and compositor dependent.",
    ]
    if compositor == "gnome":
        limitations.append(
            "GNOME Shell 50 / mutter 18 has no per-window capture-exclusion rule."
        )
    elif session.session_type == SESSION_WAYLAND and not rule_backend:
        limitations.append(
            f"{compositor or 'This compositor'} has no documented per-window "
            "capture-exclusion rule."
        )
    if compositor_skip_capture and rule_supported and session.session_type == SESSION_WAYLAND:
        mechanism = f"compositor exclusion rule configured for this window ({compositor})"
        recommended = (
            "Keep the compositor rule installed and confirm it with a real screen "
            "share. SocketStream never hides the panel automatically."
        )
    elif session.session_type == SESSION_X11:
        mechanism = "override-redirect + EWMH skip hints; panel remains visible during shares"
        recommended = (
            "X11 cannot exclude a window from a full-screen capture. Share a single "
            "application window, or use niri, Hyprland, or KWin 6.7+ on "
            "Wayland with a compositor rule. SocketStream does not auto-hide the panel."
        )
    elif session.session_type == SESSION_WAYLAND and plan.strategy == "xwayland-dock":
        mechanism = (
            "XWayland dock layer (stays above fullscreen windows); panel remains "
            "visible during shares"
        )
        recommended = (
            "GNOME cannot exclude one window from an entire-screen share. The panel "
            "does stay above other windows and fullscreen apps. Share a single "
            "application window to keep the panel out of the capture, or use niri, "
            "Hyprland, or KWin 6.7+ with a compositor rule. SocketStream does "
            "not auto-hide the panel."
        )
    elif session.session_type == SESSION_WAYLAND:
        mechanism = "Wayland toplevel hints; panel remains visible during shares"
        if rule_backend:
            hint = _COMPOSITOR_RULE_HINT[rule_backend]
            recommended = (
                f"On {compositor}, {hint}. Run `python3 capture_rules.py install` "
                "(or use Set up exclusion in the app) to add it. Until then the "
                "visible panel may appear in a whole-screen capture. SocketStream "
                "does not auto-hide the panel."
            )
        elif compositor == "gnome":
            recommended = (
                "GNOME cannot exclude one window from an entire-screen share. Share "
                "a single application window instead; the panel is then not part of "
                "the capture. SocketStream does not auto-hide the panel."
            )
        else:
            recommended = (
                "This compositor has no documented per-window exclusion rule. Share a "
                "single application window; the visible panel may appear in a "
                "whole-screen capture. SocketStream does not auto-hide the panel."
            )
    else:
        mechanism = "no supported exclusion hints; panel remains visible during shares"
        recommended = (
            "Capture exclusion is unavailable in this session. The visible panel may "
            "appear in screen captures. SocketStream does not auto-hide the panel."
        )
    return {
        "session": session.session_type,
        "compositor": compositor,
        "guaranteed": plan.guaranteed,
        "mechanism": mechanism,
        "limitations": limitations,
        "recommended": recommended,
        "capture_rule": {
            "backend": rule_backend,
            "supported": rule_supported,
            "configured": bool(compositor_skip_capture),
            "runtime_verified": False,
            "setup": "python3 capture_rules.py install" if rule_supported else "",
        },
    }


def should_hide(plan: Optional[ExclusionPlan], share_active: bool, user_hidden: bool) -> bool:
    """Return whether the user explicitly hid the panel.

    ``plan`` and ``share_active`` remain in the signature for compatibility;
    focus and screen-share state never alter panel visibility.
    """
    del plan, share_active
    return bool(user_hidden)


def _forced_state() -> Optional[bool]:
    """QA/test override: SOCKET_STREAM_SCREEN_SHARE=1|0 forces the probe result."""
    value = (os.environ.get("SOCKET_STREAM_SCREEN_SHARE") or "").strip().lower()
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
