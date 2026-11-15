"""Compositor-managed capture exclusion for the Linux answer panel.

Pure standard library: no GTK, no Wayland client library, no D-Bus binding.
The GTK shell uses :class:`CaptureExclusion` to offer an explicit, reversible
setup action; tests can inject a fake command runner and a temporary home.

What is real
------------
Three compositors document a per-window capture-exclusion rule:

* niri (Wayland): ``block-out-from "screen-capture"``
* Hyprland (Wayland): ``no_screen_share = true`` in a window rule
* KDE Plasma/KWin 6.7+: the ``excludefromcapture`` window rule
  ("Hide from Screenshots and Screen Recordings")

The window stays visible on the local display; the compositor replaces it with
a black rectangle in captures (or omits it).  This module writes the rule into
the compositor's own configuration and asks the compositor to reload it.

What is not real
----------------
Stock GNOME Shell / mutter has no per-window capture-exclusion API, and neither
do X11, sway or other wlroots compositors.  Those sessions are reported as
unsupported and the module never pretends that a rule was applied.

GNOME with the in-tree custom Mutter filter
-------------------------------------------
``native/mutter-capture-filter`` patches Mutter so the compositor can drop the
panel from capture renders.  That filter needs a deployed patched runtime *and*
``SOCKET_STREAM_CAPTURE_EXCLUDE=1`` in the environment of the GNOME Shell
process.  :mod:`mutter_capture` detects whether that is true for the shell that
owns this session and can install one managed ``systemd --user`` drop-in for
the exact unit that runs it.  Four separate questions are reported separately:
whether the runtime is deployed (files match their manifest), whether activation
is configured for this unit, whether the running compositor actually loaded it,
and whether the session must be restarted first.  ``verified`` stays false:
only a real whole-screen share proves pixel-level exclusion.

``configured`` means the override on disk is byte-identical to the one rendered
for *this* runtime version directory and the unit's effective ``ExecStart``
names that same directory.  The override deliberately pins the immutable
version directory instead of the ``current`` symlink, so republishing the
runtime needs an explicit setup step and never changes an installed
activation's meaning.  GNOME on X11 cannot activate anything, yet it still
reports and removes an override left behind by an earlier Wayland session.

Safety
------
* Nothing is written until the caller explicitly invokes ``install()``.
* The original file is copied to ``<name>.socket_stream.bak`` first.
* Writes are atomic and preserve the file mode.
* A managed marker / dedicated KWin group makes uninstall exact and
  idempotent.
* If a compositor rejects the generated configuration, the previous bytes are
  restored before an error is raised.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from overlay_guard import SESSION_WAYLAND, SessionInfo, detect_session

import mutter_capture

PANEL_APP_ID = "socket_stream_overlay"
PANEL_WM_CLASS = PANEL_APP_ID
PANEL_TITLE = "SocketStream — Practice Answer"

MANAGED_BEGIN = ">>> socket_stream capture exclusion (managed) >>>"
MANAGED_END = "<<< socket_stream capture exclusion (managed) <<<"

KDE_GROUP = "socket_stream_capture"
KDE_MIN_VERSION = (6, 7)

GROUP_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")

Runner = Callable[..., subprocess.CompletedProcess]
Which = Callable[[str], Optional[str]]


class CaptureRuleError(RuntimeError):
    """A rule could not be written, validated, or removed."""


@dataclass(frozen=True)
class RuleStatus:
    """One honest snapshot of the capture-exclusion setup.

    ``configured`` means the compositor configuration (or, on GNOME, the
    managed systemd user override) is in place.  ``verified`` is never true for
    a configured rule: it needs a real screen share.  The GNOME backend adds
    ``deployed`` (the custom runtime files match their manifest),
    ``runtime_active`` (the running compositor really loaded them and has the
    opt-in variable), ``restart_required`` (the on-disk activation and the
    running shell disagree, so only a fresh login can settle it),
    ``fallback_latched`` (the activation is configured but the guard latched
    the stock fallback, so a fresh login will not help and Set up must rearm),
    and ``startup_guard`` (the guard's own phase: armed, pending, ready,
    blocked or missing).
    """

    backend: str
    supported: bool
    configured: bool
    verified: bool
    detail: str
    guidance: str = ""
    path: str = ""
    can_install: bool = False
    can_remove: bool = False
    config_validated: bool = False
    deployed: bool = False
    runtime_active: bool = False
    restart_required: bool = False
    fallback_latched: bool = False
    startup_guard: str = ""

    def as_dict(self) -> dict:
        return {
            "backend": self.backend,
            "supported": self.supported,
            "configured": self.configured,
            "verified": self.verified,
            "detail": self.detail,
            "guidance": self.guidance,
            "path": self.path,
            "can_install": self.can_install,
            "can_remove": self.can_remove,
            "config_validated": self.config_validated,
            "deployed": self.deployed,
            "runtime_active": self.runtime_active,
            "restart_required": self.restart_required,
            "fallback_latched": self.fallback_latched,
            "startup_guard": self.startup_guard,
        }


def _run(argv, timeout=15, env=None):
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout, env=env,
        check=False,
    )


def _config_home(environ: Mapping[str, str], home: Path) -> Path:
    value = (environ.get("XDG_CONFIG_HOME") or "").strip()
    if value:
        return Path(value).expanduser()
    return home / ".config"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise CaptureRuleError(f"cannot read {path}: {exc}") from exc


def _backup(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    backup = path.with_name(path.name + ".socket_stream.bak")
    try:
        shutil.copy2(path, backup)
    except OSError as exc:
        raise CaptureRuleError(f"cannot back up {path}: {exc}") from exc
    return backup


def _restore(path: Path, backup: Optional[Path], existed: bool) -> None:
    try:
        if backup is not None and backup.exists():
            shutil.copy2(backup, path)
        elif not existed:
            path.unlink(missing_ok=True)
    except OSError:
        # The caller is already handling a failure. Keep the original error;
        # the backup remains on disk for manual recovery.
        pass


def _write_atomic(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(text)
            if mode is not None:
                os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError as exc:
        raise CaptureRuleError(f"cannot write {path}: {exc}") from exc


def _text(status: Optional[subprocess.CompletedProcess]) -> str:
    if status is None:
        return ""
    return ((status.stdout or "") + (status.stderr or "")).strip()


def _markers(comment: str) -> tuple[str, str]:
    return f"{comment} {MANAGED_BEGIN}", f"{comment} {MANAGED_END}"


def _block_state(text: str, comment: str) -> str:
    """Return ``present``, ``absent`` or ``malformed`` for our block."""
    begin, end = _markers(comment)
    lines = [line.strip() for line in text.splitlines()]
    if begin not in lines:
        return "absent"
    try:
        start = lines.index(begin)
        finish = lines.index(end, start + 1)
    except ValueError:
        return "malformed"
    if finish < start:
        return "malformed"
    return "present"


def _strip_block(text: str, comment: str) -> tuple[str, bool]:
    begin, end = _markers(comment)
    lines = text.splitlines(keepends=True)
    kept = []
    index = 0
    removed = False
    while index < len(lines):
        if lines[index].strip() != begin:
            kept.append(lines[index])
            index += 1
            continue
        finish = None
        for candidate in range(index + 1, len(lines)):
            if lines[candidate].strip() == end:
                finish = candidate
                break
        if finish is None:
            raise CaptureRuleError(
                "the managed capture-exclusion block has no end marker; "
                "refusing to modify the file"
            )
        removed = True
        index = finish + 1
    return "".join(kept), removed


def _append_block(text: str, block: str) -> str:
    base = text.rstrip("\n")
    rendered = block.strip("\n")
    if base:
        return f"{base}\n\n{rendered}\n"
    return f"{rendered}\n"


class _UnsupportedBackend:
    name = "unsupported"
    supported = False

    def __init__(self, name: str, detail: str, guidance: str):
        self.name = name
        self._detail = detail
        self._guidance = guidance

    def status(self) -> RuleStatus:
        return RuleStatus(
            backend=self.name,
            supported=False,
            configured=False,
            verified=False,
            detail=self._detail,
            guidance=self._guidance,
        )

    def install(self) -> RuleStatus:
        raise CaptureRuleError(f"{self._detail} {self._guidance}".strip())

    def remove(self) -> RuleStatus:
        return self.status()


class _TextRuleBackend:
    """Base for compositors whose configuration is a text file."""

    name = "text"
    comment = "#"
    signature = ""
    create_if_missing = False

    def __init__(self, path: Path, *, runner: Optional[Runner] = None,
                 which: Optional[Which] = None, environ: Optional[Mapping[str, str]] = None):
        self.path = Path(path).expanduser().resolve()
        self._runner = runner or _run
        self._which = which or shutil.which
        self._environ = dict(os.environ if environ is None else environ)
        self._last_validation = False
        self._last_note = ""

    # -- content ---------------------------------------------------------
    def _block(self) -> str:
        raise NotImplementedError

    def _status_detail(self) -> str:
        return f"{self.name} capture-exclusion rule"

    def _status_guidance(self) -> str:
        return "Use Set up exclusion in the main window to add the managed rule."

    def _read(self) -> str:
        return _read_text(self.path)

    def _state(self, text: str) -> str:
        return _block_state(text, self.comment)

    def _configured(self, text: str) -> bool:
        begin, end = _markers(self.comment)
        if _block_state(text, self.comment) != "present":
            return False
        start = text.find(begin)
        finish = text.find(end, start + len(begin))
        return bool(self.signature) and self.signature in text[start:finish]

    # -- commands --------------------------------------------------------
    def _validate(self, text: str) -> tuple[Optional[bool], str]:
        return None, "no external validator for this compositor"

    def _reload(self) -> tuple[bool, str]:
        return True, "the compositor reloads its configuration automatically"

    # -- public ----------------------------------------------------------
    def status(self) -> RuleStatus:
        try:
            text = self._read()
        except CaptureRuleError as exc:
            return RuleStatus(
                backend=self.name, supported=True, configured=False, verified=False,
                detail=str(exc), guidance="Fix the configuration file and retry.",
                path=str(self.path),
            )
        state = self._state(text)
        configured = self._configured(text)
        exists = self.path.exists()
        can_install = (
            state != "malformed" and not configured
            and (exists or self.create_if_missing)
        )
        if state == "malformed":
            detail = "the managed capture-exclusion block is incomplete"
            guidance = f"Remove the block from {self.path} manually, then retry."
        elif configured:
            detail = self._status_detail()
            guidance = self._status_guidance()
        elif not exists and not self.create_if_missing:
            detail = f"{self.name} configuration was not found at {self.path}"
            guidance = (
                f"Start {self.name} once so it creates {self.path.name}, then retry."
            )
        else:
            detail = f"no managed {self.name} capture-exclusion rule is installed"
            guidance = self._status_guidance()
        return RuleStatus(
            backend=self.name, supported=True, configured=configured,
            verified=False,
            detail=detail, guidance=guidance, path=str(self.path),
            can_install=can_install, can_remove=configured,
            config_validated=bool(configured and self._last_validation),
        )

    def install(self) -> RuleStatus:
        existed = self.path.exists()
        original = self._read()
        if not existed and not self.create_if_missing:
            raise CaptureRuleError(
                f"{self.name} configuration was not found at {self.path}. "
                f"Start {self.name} once so it creates the file, then retry."
            )
        new_text = _append_block(_strip_block(original, self.comment)[0], self._block())
        if new_text == original:
            self._last_note = "rule was already installed"
            return self.status()
        backup = _backup(self.path) if existed else None
        _write_atomic(self.path, new_text)
        try:
            validation, message = self._validate(new_text)
        except Exception as exc:  # noqa: BLE001 - restore on any validator failure
            _restore(self.path, backup, existed)
            raise CaptureRuleError(f"{self.name} validation failed: {exc}") from exc
        if validation is False:
            _restore(self.path, backup, existed)
            raise CaptureRuleError(
                f"{self.name} rejected the generated rule: {message or 'invalid configuration'}"
            )
        self._last_validation = validation is True
        try:
            reloaded, reload_message = self._reload()
        except CaptureRuleError:
            _restore(self.path, backup, existed)
            raise
        self._last_note = message
        if not reloaded:
            self._last_note = f"{message}; {reload_message}".strip("; ")
        elif message:
            self._last_note = message
        return self.status()

    def remove(self) -> RuleStatus:
        if not self.path.exists():
            return self.status()
        original = self._read()
        new_text, removed = _strip_block(original, self.comment)
        if not removed:
            return self.status()
        backup = _backup(self.path)
        _write_atomic(self.path, new_text)
        try:
            validation, message = self._validate(new_text)
        except Exception as exc:  # noqa: BLE001
            _restore(self.path, backup, True)
            raise CaptureRuleError(f"{self.name} validation failed: {exc}") from exc
        if validation is False:
            _restore(self.path, backup, True)
            raise CaptureRuleError(
                f"{self.name} rejected the file after removing the rule: "
                f"{message or 'invalid configuration'}"
            )
        try:
            reloaded, reload_message = self._reload()
        except CaptureRuleError:
            _restore(self.path, backup, True)
            raise
        self._last_validation = False
        self._last_note = message if reloaded else f"{message}; {reload_message}".strip("; ")
        return self.status()

    def note(self) -> str:
        return self._last_note


class NiriBackend(_TextRuleBackend):
    name = "niri"
    comment = "//"
    signature = "block-out-from"
    create_if_missing = False

    def _block(self) -> str:
        return (
            f"{self.comment} {MANAGED_BEGIN}\n"
            "window-rule {\n"
            f'    match app-id=r#"^{PANEL_APP_ID}$"#\n'
            '    block-out-from "screen-capture"\n'
            "}\n"
            f"{self.comment} {MANAGED_END}"
        )

    def _status_detail(self) -> str:
        return "niri window rule is installed (block-out-from \"screen-capture\")"

    def _status_guidance(self) -> str:
        return (
            "The panel stays visible locally and is replaced by a black rectangle "
            "in captures. Run `niri validate` after editing if you change the file."
        )

    def _validate(self, text: str) -> tuple[Optional[bool], str]:
        tool = self._which("niri")
        if not tool:
            return None, "niri is not in PATH; the rule was not syntax-validated"
        try:
            result = self._runner([tool, "validate"], 20)
        except Exception as exc:  # noqa: BLE001
            return None, f"niri validate could not run: {exc}"
        if result.returncode == 0:
            return True, "niri validate passed"
        return False, _text(result) or "niri validate failed"

    def _reload(self) -> tuple[bool, str]:
        return True, "niri reloads config.kdl when it is saved"


class HyprlandBackend(_TextRuleBackend):
    name = "hyprland"
    signature = "no_screen_share"

    def __init__(self, path: Path, **kwargs):
        super().__init__(path, **kwargs)
        self.lua = self.path.suffix.lower() == ".lua"
        self.comment = "--" if self.lua else "#"

    def _block(self) -> str:
        if self.lua:
            return (
                f"{self.comment} {MANAGED_BEGIN}\n"
                "hl.window_rule({\n"
                '    name = "socket_stream_capture-exclusion",\n'
                '    match = { class = "^(socket_stream_overlay)$" },\n'
                "    no_screen_share = true,\n"
                "})\n"
                f"{self.comment} {MANAGED_END}"
            )
        return (
            f"{self.comment} {MANAGED_BEGIN}\n"
            "windowrule {\n"
            "    name = socket_stream_capture-exclusion\n"
            f"    match:class = ^({PANEL_APP_ID})$\n"
            "    no_screen_share = true\n"
            "}\n"
            f"{self.comment} {MANAGED_END}"
        )

    def _status_detail(self) -> str:
        return "Hyprland window rule is installed (no_screen_share)"

    def _status_guidance(self) -> str:
        return (
            "The panel stays visible locally; Hyprland draws black rectangles "
            "over it in screen sharing. Run `hyprctl reload` after manual edits."
        )

    def _validate(self, text: str) -> tuple[Optional[bool], str]:
        tool = self._which("Hyprland")
        if not tool:
            return None, "Hyprland is not in PATH; the rule was not syntax-validated"
        env = dict(self._environ)
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        try:
            result = self._runner(
                [tool, "--verify-config", "-c", str(self.path)], 30, env=env,
            )
        except Exception as exc:  # noqa: BLE001
            return None, f"Hyprland --verify-config could not run: {exc}"
        if result.returncode == 0:
            return True, "Hyprland --verify-config passed"
        return False, _text(result) or "Hyprland --verify-config failed"

    def _reload(self) -> tuple[bool, str]:
        tool = self._which("hyprctl")
        if not tool:
            return False, "hyprctl is not in PATH; the rule applies on next start"
        try:
            result = self._runner([tool, "reload"], 15)
        except Exception as exc:  # noqa: BLE001
            return False, f"hyprctl reload could not run: {exc}"
        try:
            errors = self._runner([tool, "configerrors"], 15)
        except Exception:  # noqa: BLE001 - reload result is still useful
            errors = None
        message = _text(errors)
        lowered = message.lower()
        if "error" in lowered and (
            "socket_stream" in lowered or "no_screen_share" in lowered
        ):
            raise CaptureRuleError(
                f"Hyprland rejected the generated rule: {message}"
            )
        if result.returncode != 0:
            return False, _text(result) or (
                "Hyprland is not running; the rule applies on next start"
            )
        return True, "hyprctl reload succeeded"


class KdeBackend:
    name = "kde"
    supported = True

    def __init__(self, path: Path, *, version: Optional[tuple[int, int]],
                 runner: Optional[Runner] = None, which: Optional[Which] = None,
                 environ: Optional[Mapping[str, str]] = None):
        self.path = Path(path).expanduser().resolve()
        self.version = version
        self._runner = runner or _run
        self._which = which or shutil.which
        self._environ = dict(os.environ if environ is None else environ)
        self._last_note = ""

    def _block(self) -> str:
        return "\n".join([
            f"[{KDE_GROUP}]",
            "Description=SocketStream capture exclusion",
            "Enabled=true",
            f"wmclass={PANEL_WM_CLASS}",
            "wmclassmatch=1",
            "wmclasscomplete=false",
            "excludefromcapture=true",
            "excludefromcapturerule=2",
        ]) + "\n"

    def _version_ok(self) -> bool:
        return bool(self.version and self.version >= KDE_MIN_VERSION)

    def _version_text(self) -> str:
        if not self.version:
            return "unknown"
        return ".".join(str(part) for part in self.version)

    def _configured(self, text: str) -> bool:
        group = _parse_ini(text).get(KDE_GROUP)
        if not group:
            return False
        return (
            KDE_GROUP in _kwin_rule_ids(text, modern=bool(self.version and self.version >= (6, 8)))
            and group.get("Enabled", "true").lower() == "true"
            and group.get("excludefromcapturerule") == "2"
            and group.get("wmclassmatch") == "1"
            and group.get("wmclasscomplete", "false").lower() == "false"
            and group.get("excludefromcapture", "").strip().lower() == "true"
            and group.get("wmclass", "").strip() == PANEL_WM_CLASS
        )

    def status(self) -> RuleStatus:
        try:
            text = _read_text(self.path)
            _validate_kwin_ini(text)
            ids = _kwin_rule_ids(text, modern=bool(self.version and self.version >= (6, 8)))
            configured = self._configured(text)
            removable = KDE_GROUP in _parse_ini(text) or KDE_GROUP in ids
        except CaptureRuleError as exc:
            return RuleStatus(
                backend=self.name, supported=self._version_ok(), configured=False,
                verified=False, detail=str(exc), path=str(self.path),
                guidance="Fix the KWin rules file and retry.",
            )
        if not self._version_ok():
            detail = (
                f"KDE Plasma {self._version_text()} has no Hide from Screencast rule"
            )
            guidance = (
                "KWin 6.7 or newer on Wayland is required for the persistent rule. On older Plasma, share a "
                "single application window instead of the entire screen."
            )
        elif configured:
            detail = "KWin 'Hide from Screencast' window rule is installed"
            guidance = (
                "The panel stays visible locally; KWin's output/region capture filter uses this rule. "
                "Explicit panel-window capture is not covered by the rule. "
                "KWin is asked to reload; the rule also applies on next login."
            )
        else:
            detail = "no managed KWin capture-exclusion rule is installed"
            guidance = "Use Set up exclusion to add the rule for this application."
        return RuleStatus(
            backend=self.name, supported=self._version_ok(), configured=configured,
            verified=False, detail=detail, guidance=guidance, path=str(self.path),
            can_install=self._version_ok() and not configured,
            can_remove=removable,
        )

    def install(self) -> RuleStatus:
        if not self._version_ok():
            raise CaptureRuleError(
                f"KWin 6.7 or newer is required; detected {self._version_text()}. "
                "Share a single application window instead."
            )
        existed = self.path.exists()
        original = _read_text(self.path)
        _validate_kwin_ini(original)
        new_text = _append_block(_strip_ini_group(original), self._block())
        new_text = _kwin_register_rule(new_text, install=True,
                                       modern=bool(self.version and self.version >= (6, 8)))
        if new_text == original:
            return self.status()
        backup = _backup(self.path) if existed else None
        _write_atomic(self.path, new_text)
        if not self._configured(new_text):
            _restore(self.path, backup, existed)
            raise CaptureRuleError(
                "the generated KWin rule could not be read back; the previous file was restored"
            )
        reloaded, message = self._reload()
        self._last_note = message
        if not reloaded:
            self._last_note = f"{message}; the rule applies on next login"
        return self.status()

    def remove(self) -> RuleStatus:
        if not self.path.exists():
            return self.status()
        original = _read_text(self.path)
        _validate_kwin_ini(original)
        if KDE_GROUP not in _parse_ini(original) and KDE_GROUP not in _kwin_rule_ids(original):
            return self.status()
        new_text = _kwin_register_rule(_strip_ini_group(original), install=False,
                                       modern=bool(self.version and self.version >= (6, 8)))
        if new_text == original:
            return self.status()
        backup = _backup(self.path)
        _write_atomic(self.path, new_text)
        if self._configured(new_text):
            _restore(self.path, backup, True)
            raise CaptureRuleError(
                "the KWin group could not be removed cleanly; the previous file was restored"
            )
        reloaded, message = self._reload()
        self._last_note = message if reloaded else f"{message}; retry after login"
        return self.status()

    def _reload(self) -> tuple[bool, str]:
        for command in ("qdbus6", "qdbus"):
            tool = self._which(command)
            if tool:
                try:
                    result = self._runner(
                        [tool, "org.kde.KWin", "/KWin", "reconfigure"], 15,
                    )
                except Exception:  # noqa: BLE001
                    continue
                if result.returncode == 0:
                    return True, "KWin reconfigure requested"
        tool = self._which("dbus-send")
        if tool:
            try:
                result = self._runner([
                    tool, "--session", "--type=method_call", "--print-reply",
                    "--dest=org.kde.KWin", "/KWin",
                    "org.kde.KWin.reconfigure",
                ], 15)
            except Exception:  # noqa: BLE001
                result = None
            if result is not None and result.returncode == 0:
                return True, "KWin reconfigure requested"
        return False, "KWin is not reachable from this session"

    def note(self) -> str:
        return self._last_note


def _validate_kwin_ini(text: str) -> None:
    """Refuse KConfig extensions we cannot round-trip, before any file write."""
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("["):
            group = GROUP_RE.match(line)
            if not group or "\\" in group.group(1) or group.group(1) == "$i":
                raise CaptureRuleError("KWin file has extended/nested/escaped group syntax; edit the rule in KWin settings")
            continue
        key = line.partition("=")[0]
        if any(char in key for char in "[]\\"):
            raise CaptureRuleError("KWin file has immutable/localized keys; edit the rule in KWin settings")


def _parse_ini(text: str) -> dict[str, dict[str, str]]:
    groups: dict[str, dict[str, str]] = {}
    current: Optional[dict[str, str]] = None
    for line in text.splitlines():
        match = GROUP_RE.match(line)
        if match:
            current = groups.setdefault(match.group(1), {})
            continue
        if current is None or "=" not in line:
            continue
        stripped = line.lstrip()
        if stripped.startswith(("#", ";")):
            continue
        key, _, value = line.partition("=")
        current[key.strip()] = value.strip()
    return groups


def _strip_ini_group(text: str) -> str:
    """Remove every ``[socket_stream_capture]`` section, preserving all else."""
    lines = text.splitlines(keepends=True)
    kept = []
    skipping = False
    for line in lines:
        match = GROUP_RE.match(line)
        if match:
            skipping = match.group(1) == KDE_GROUP
            if skipping:
                continue
        if not skipping:
            kept.append(line)
    return "".join(kept)


def _decode_kwin_list(value: str) -> list[str]:
    """Decode INI backslashes, then QStringList's comma/backslash escapes.

    Other INI escapes are deliberately refused rather than normalized through
    a partial KConfig parser. Group-name escapes are likewise refused above.
    """
    outer = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\":
            index += 1
            if index == len(value) or value[index] != "\\":
                raise CaptureRuleError("KWin registry has unsupported INI escapes; edit the rule in KWin settings")
        outer.append(char)
        index += 1
    ids, current = [], []
    escaped = False
    for char in outer:
        if escaped:
            if char not in (",", "\\"):
                raise CaptureRuleError("KWin registry has unsupported list escapes; edit the rule in KWin settings")
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ",":
            ids.append("".join(current))
            current = []
        else:
            current.append(char)
    if escaped:
        raise CaptureRuleError("KWin registry has an incomplete list escape")
    ids.append("".join(current))
    return [item for item in ids if item]


def _encode_kwin_list(ids: list[str]) -> str:
    value = ",".join(item.replace("\\", "\\\\").replace(",", "\\,") for item in ids)
    return value.replace("\\", "\\\\")


def _kwin_rule_ids(text: str, *, modern: bool = False) -> list[str]:
    """Read effective IDs without activating retired legacy groups."""
    groups = _parse_ini(text)
    general = groups.get("General", {})
    modern = modern or "Order" in general
    # In 6.8 a nonempty legacy list overrides Order during migration.
    value = general.get("rules", "") or general.get("Order", "")
    ids = []
    if value:
        ids = _decode_kwin_list(value)
    elif not modern:
        try:
            count = int(general.get("count", "0"))
        except ValueError as exc:
            raise CaptureRuleError("KWin's legacy rule count is invalid") from exc
        if not 0 <= count <= 10000:
            raise CaptureRuleError("KWin's legacy rule count is out of range")
        ids = [str(index) for index in range(1, count + 1)]
    if modern:
        available = [name for name in sorted(groups)
                      if name != "General" and not name.startswith("$")]
        if general.get("rules", "") and any(name not in ids and name != KDE_GROUP for name in available):
            # KWin purges these during migration. Neither delete user groups
            # nor activate them by emitting Order: let KWin migrate first.
            raise CaptureRuleError("KWin legacy registry has retired groups; open and save Window Rules in KWin settings before retrying")
        ids = [item for item in ids if item in available]
        if general.get("rules", ""):
            return ids
        for name in available:
            if name not in ids:
                ids.insert(0, name)
    return ids


def _kwin_register_rule(text: str, *, install: bool, modern: bool = False) -> str:
    """Register our group in [General], preserving other rules and their order.

    KWin 6.7 uses `rules/count`; 6.8 uses `Order` and all nonmetadata groups.
    Force rules are first-match, so explicit installation puts ours first.
    """
    modern = modern or "Order" in _parse_ini(text).get("General", {})
    ids = [item for item in _kwin_rule_ids(text, modern=modern) if item != KDE_GROUP]
    if install:
        ids.insert(0, KDE_GROUP)
    value = _encode_kwin_list(ids)
    entries = f"Order={value}\n" if modern else f"rules={value}\ncount={len(ids)}\n"
    lines = text.splitlines(keepends=True)
    result = []
    in_general = False
    found = False
    for line in lines:
        match = GROUP_RE.match(line)
        if match:
            in_general = match.group(1) == "General"
            if in_general:
                if found:
                    raise CaptureRuleError("duplicate [General] sections in kwinrulesrc")
                found = True
                result.append(line.rstrip("\r\n") + "\n" + entries)
                continue
        if in_general and line.partition("=")[0].strip() in ("rules", "count", "Order"):
            continue
        result.append(line)
    if not found:
        return _append_block(text, "[General]\n" + entries)
    return "".join(result)


def _kde_version(runner: Runner, which: Which) -> Optional[tuple[int, int]]:
    # The compositor implements the feature, not plasmashell. Mixed package
    # versions must not turn a newer shell into a false capability claim.
    for command in ("kwin_wayland",):
        tool = which(command)
        if not tool:
            continue
        try:
            result = runner([tool, "--version"], 10)
        except Exception:  # noqa: BLE001 - try the next tool
            continue
        if result.returncode != 0:
            continue
        match = re.search(r"\bkwin\s+(\d+)\.(\d+)", _text(result), re.IGNORECASE)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def _failed_status(name: str, exc: BaseException, config_home: Path) -> RuleStatus:
    """A status that claims nothing, yet still offers a real removal.

    A failing probe must not strand a managed override the user wants gone, so
    the removal half is answered from the filesystem alone.
    """
    managed = [item for item in mutter_capture.scan_activation(config_home)
               if item.managed]
    return RuleStatus(
        backend=name, supported=False, configured=False, verified=False,
        detail=f"capture-exclusion status failed: {type(exc).__name__}",
        guidance=(
            "Check the GNOME Shell user service and the custom runtime manually. "
            "SocketStream never restarts your desktop or your session."
        ),
        path=str(managed[0].path) if managed else "",
        can_install=False, can_remove=bool(managed),
    )


class GnomeMutterBackend:
    """Custom Mutter runtime status plus managed systemd user activation.

    The runtime is never installed by this class: ``native/mutter-capture-filter``
    stages it.  This backend reports whether it is deployed, whether activation
    is configured for the exact unit that runs this session, and whether the
    running compositor really loaded it.
    """

    name = "gnome"

    def __init__(self, *, config_home: Path, runner: Runner, which: Which,
                 environ: Mapping[str, str], proc_root: Path, uid: int,
                 runtime_owner_uid: int = 0):
        self._config_home = Path(config_home)
        self._runner = runner
        self._which = which
        self._environ = dict(environ)
        self._proc_root = Path(proc_root)
        self._uid = int(uid)
        # The native validator runs as root and requires a root-owned runtime.
        # Only tests, which cannot create root-owned directories, override it.
        self._runtime_owner_uid = int(runtime_owner_uid)
        self._last_note = ""

    # -- inspection ------------------------------------------------------
    def _inspect(self):
        runtime = mutter_capture.load_runtime(
            self._environ, owner_uid=self._runtime_owner_uid)
        shell = mutter_capture.detect_shell(
            runner=self._runner, which=self._which, proc_root=self._proc_root,
            uid=self._uid, environ=self._environ,
        )
        compatible = None
        compatibility = ""
        if runtime.deployed:
            compatible, compatibility = mutter_capture.packages_match(
                runtime.packages, runner=self._runner, which=self._which,
            )
        return runtime, shell, compatible, compatibility

    def _status(self) -> RuleStatus:
        runtime, shell, compatible, compatibility = self._inspect()
        entry = (mutter_capture.read_activation(self._config_home, shell.unit)
                 if shell.ok else None)
        found = mutter_capture.scan_activation(self._config_home)
        managed = [item for item in found if item.managed]
        blocked = [item.path for item in found if not item.regular]
        foreign = [item.path for item in found if item.regular and not item.managed]

        state = (mutter_capture.guard_state_path(
            self._config_home, runtime.root, shell.mode)
            if shell.ok and runtime.deployed else None)
        guard = (mutter_capture.read_guard_state(state, root=runtime.root, mode=shell.mode) if state is not None
                  else mutter_capture.GuardState(mutter_capture.GUARD_MISSING))

        # ``configured`` needs every half: the exact schema-2 override rendered
        # for this runtime directory, an effective ExecStart that names the same
        # directory, state and mode, and both lifecycle hooks present and
        # unambiguous in the effective unit.  Any other managed override is
        # stale and is offered for replacement.
        configured = bool(
            shell.ok and runtime.deployed and state is not None
            and shell.exec_kind == "ours"
            and shell.managed_root == runtime.root
            and shell.guarded_state == str(state)
            and mutter_capture.activation_configured(
                self._config_home, shell.unit, runtime.root, shell.mode, str(state))
            and mutter_capture.own_post_hooks(
                exec_start_post=shell.exec_start_post,
                exec_stop_post=shell.exec_stop_post,
                root=runtime.root, mode=shell.mode, state=str(state))
        )
        live = (
            mutter_capture.read_live_state(shell, runtime, self._proc_root)
            if shell.ok and runtime.deployed
            else mutter_capture.LiveState("unknown")
        )
        supported = bool(runtime.deployed and compatible is True and shell.ok)
        # Activation only counts once the running compositor is the compatible
        # runtime: a deployed build is not evidence of a loaded renderer.
        runtime_active = bool(supported and live.active)

        # The guard latched the stock fallback at the last login.  The override
        # is configured, but logging out again will not help: Set up must run a
        # fresh isolated startup check and rearm, so the row must not keep
        # telling the user to log out.
        current_attempt = mutter_capture.guard_matches_running(guard, shell, runtime, self._proc_root)
        ready_current = current_attempt and guard.phase == "ready" and runtime_active
        fallback_latched = bool(configured and not guard.armed and not ready_current)

        # A fresh login is the only fix while the running shell has not loaded
        # the configured runtime -- including a shell that loaded it before the
        # launcher started exporting the opt-in.  It is also the fix when the
        # activation was just removed while custom libraries are still live.
        stale_states = ("stock", "mixed", "stale", "missing", "inactive")
        restart_required = bool(supported and not fallback_latched and (
            (configured and live.state in stale_states)
            or (not configured and live.state in ("active", "inactive"))
        ))

        blocked_at_target = bool(entry is not None and not entry.regular)
        foreign_at_target = bool(entry is not None and entry.regular and not entry.managed)

        path = ""
        if entry is not None:
            path = str(entry.path)
        else:
            remaining = [str(item.path) for item in managed] + [str(p) for p in blocked + foreign]
            if remaining:
                path = remaining[0]
            elif runtime.deployed:
                path = runtime.root

        if not runtime.deployed:
            detail = runtime.reason or "custom Mutter capture runtime is not installed"
            guidance = (
                f"Stock GNOME Shell cannot exclude one window from an entire-screen "
                f"share; share a single application window instead. The runtime is "
                f"staged separately at {mutter_capture.DEFAULT_MANIFEST}. "
                f"SocketStream does not install it."
            )
        elif compatible is not True:
            detail = (
                "the custom Mutter runtime does not match the installed packages"
                if compatible is False else
                "cannot confirm that the custom Mutter runtime matches the installed packages"
            )
            guidance = (
                f"{compatibility or 'the installed package versions could not be read'}. "
                "The launcher falls back to the stock shell when validation fails. "
                "Rebuild or remove the runtime."
            )
        elif not shell.ok:
            detail = f"capture exclusion cannot be activated: {shell.reason}"
            guidance = (
                "Activation needs the systemd --user service that runs GNOME Shell. "
                "SocketStream does not change session startup."
            )
        elif fallback_latched:
            # Configured but the guard latched the stock fallback: a fresh login
            # cannot fix it, so the row must not ask for one again.
            detail = ("capture-exclusion activation is configured, but its startup "
                      "guard is not armed for the next custom launch")
            if live.state == "stock":
                detail += "; stock GNOME Shell is running"
            guidance = (
                "Use Set up exclusion to rerun the isolated startup check and rearm "
                "the activation. Capture is not verified; only a real whole-screen "
                "share can show whether the panel is excluded."
            )
            if current_attempt:
                guidance = ("The running custom attempt has not completed its readiness hook. "
                            "Its guard cannot be rearmed while it is running; the next login "
                            "uses stock GNOME if readiness remains unconfirmed. Capture is unverified.")
        elif restart_required and configured:
            # A configured override the running shell has not honoured yet --
            # including a shell that loaded the runtime without the opt-in the
            # launcher now exports.  A fresh login is the fix in both cases.
            detail = (
                live.detail if live.state == "inactive" else
                f"capture-exclusion activation is configured for {shell.unit}, but the "
                "running shell has not loaded the validated runtime"
            )
            guidance = (
                "Log out and back in so GNOME Shell starts from the configured runtime. "
                "SocketStream never restarts your desktop or your session."
            )
        elif restart_required:
            detail = (
                live.detail if live.state == "inactive" else
                "activation changed; the running shell still uses the previous setup"
            )
            guidance = (
                "Log out and back in so GNOME Shell restarts with the current setup. "
                "SocketStream never restarts your desktop or your session."
            )
        elif runtime_active:
            detail = (
                f"custom Mutter runtime {runtime.runtime_id} is loaded by GNOME Shell "
                "with the capture opt-in set"
            )
            guidance = (
                "The custom capture renderer is enabled; capture is still unverified. "
                "Only a real whole-screen share can show whether the panel is excluded."
            )
        elif configured:
            detail = (
                f"capture-exclusion activation is configured for {shell.unit}; the "
                "running shell state could not be read"
            )
            guidance = (
                "Log out and back in, or check the compositor process manually. "
                "SocketStream never restarts your desktop or your session."
            )
        elif blocked_at_target:
            detail = (
                f"{entry.path} exists but is not a regular file SocketStream may read, so "
                "it was left alone"
            )
            guidance = (
                "Remove or rename that entry yourself, then retry. SocketStream never "
                "follows, overwrites, or deletes it."
            )
        elif foreign_at_target:
            detail = (
                f"{entry.path} already exists and was not written by SocketStream"
            )
            guidance = (
                "Rename or remove that file yourself, then retry. SocketStream "
                "never overwrites or deletes a file it did not write."
            )
        else:
            detail = "the custom Mutter runtime is installed; activation is not set up"
            guidance = (
                f"Use Set up exclusion to add the user-service override for "
                f"{shell.unit}, then log out and back in."
            )

        return RuleStatus(
            backend=self.name, supported=supported, configured=configured,
            verified=False, detail=detail, guidance=guidance, path=path,
            can_install=bool(
                supported and (not configured or fallback_latched)
                and not blocked_at_target and not foreign_at_target
                and not (current_attempt and not ready_current)
            ),
            can_remove=bool(managed),
            config_validated=bool(configured and compatible is True),
            deployed=bool(runtime.deployed),
            runtime_active=runtime_active,
            restart_required=restart_required,
            fallback_latched=fallback_latched,
            startup_guard=guard.phase,
        )

    def status(self) -> RuleStatus:
        try:
            return self._status()
        except Exception as exc:  # noqa: BLE001 - status must never crash the UI
            return _failed_status(self.name, exc, self._config_home)

    # -- mutation --------------------------------------------------------
    def install(self) -> RuleStatus:
        runtime, shell, compatible, compatibility = self._inspect()
        if not runtime.deployed:
            raise CaptureRuleError(
                runtime.reason or "the custom Mutter runtime is not deployed")
        if compatible is not True:
            raise CaptureRuleError(
                compatibility or "the custom runtime does not match the installed packages"
            )
        if not shell.ok:
            raise CaptureRuleError(
                shell.reason or "the GNOME Shell user service is not identified")
        try:
            self._last_note = mutter_capture.install_activation(
                config_home=self._config_home, runtime=runtime, shell=shell,
                runner=self._runner, which=self._which, proc_root=self._proc_root,
            )
        except mutter_capture.MutterCaptureError as exc:
            raise CaptureRuleError(str(exc)) from exc
        return self.status()

    def remove(self) -> RuleStatus:
        try:
            self._last_note = mutter_capture.remove_activation(
                config_home=self._config_home, runner=self._runner,
                which=self._which,
            )
        except mutter_capture.MutterCaptureError as exc:
            raise CaptureRuleError(str(exc)) from exc
        return self.status()

    def note(self) -> str:
        return self._last_note


class GnomeX11Backend:
    """GNOME on X11: no capture exclusion, but a stale override is still ours.

    The custom Mutter runtime only runs on Wayland, so nothing can be
    installed here.  A previous Wayland session may however have left the
    managed ``systemd --user`` override behind, and the user must still be able
    to see and remove it from this backend.
    """

    name = "gnome"

    def __init__(self, *, config_home: Path, runner: Runner, which: Which):
        self._config_home = Path(config_home)
        self._runner = runner
        self._which = which
        self._last_note = ""

    def _managed(self) -> list:
        return [
            item for item in mutter_capture.scan_activation(self._config_home)
            if item.managed
        ]

    def _status(self) -> RuleStatus:
        managed = self._managed()
        if managed:
            detail = (
                "a capture-exclusion activation from an earlier GNOME Wayland "
                "session is still installed"
            )
        else:
            detail = "stock GNOME Shell on X11 has no per-window screen-capture exclusion"
        guidance = (
            "X11 clients with display access can read the whole root window, so share "
            "a single application window. The custom Mutter runtime only runs on "
            "Wayland; SocketStream never restarts your desktop or your session."
        )
        return RuleStatus(
            backend=self.name, supported=False, configured=False, verified=False,
            detail=detail, guidance=guidance,
            path=str(managed[0].path) if managed else "",
            can_install=False, can_remove=bool(managed),
        )

    def status(self) -> RuleStatus:
        try:
            return self._status()
        except Exception as exc:  # noqa: BLE001 - status must never crash the UI
            return _failed_status(self.name, exc, self._config_home)

    def install(self) -> RuleStatus:
        raise CaptureRuleError(
            "the custom Mutter runtime can only be activated on a GNOME Wayland "
            "session"
        )

    def remove(self) -> RuleStatus:
        try:
            self._last_note = mutter_capture.remove_activation(
                config_home=self._config_home, runner=self._runner,
                which=self._which,
            )
        except mutter_capture.MutterCaptureError as exc:
            raise CaptureRuleError(str(exc)) from exc
        return self.status()

    def note(self) -> str:
        return self._last_note


def _select_backend(session: SessionInfo, *, config_home: Path,
                    runner: Runner, which: Which,
                    environ: Mapping[str, str],
                    proc_root: Path, uid: int,
                    runtime_owner_uid: int = 0):
    compositor = (session.compositor or "unknown").lower()
    if compositor == "niri" and session.session_type == SESSION_WAYLAND:
        return NiriBackend(
            config_home / "niri" / "config.kdl",
            runner=runner, which=which, environ=environ,
        )
    if compositor == "hyprland" and session.session_type == SESSION_WAYLAND:
        base = config_home / "hypr"
        lua = base / "hyprland.lua"
        path = lua if lua.exists() else base / "hyprland.conf"
        return HyprlandBackend(path, runner=runner, which=which, environ=environ)
    if compositor == "kde" and session.session_type == SESSION_WAYLAND:
        return KdeBackend(
            config_home / "kwinrulesrc", version=_kde_version(runner, which),
            runner=runner, which=which, environ=environ,
        )
    if compositor == "gnome":
        if session.session_type == SESSION_WAYLAND:
            return GnomeMutterBackend(
                config_home=config_home, runner=runner, which=which,
                environ=environ, proc_root=proc_root, uid=uid,
                runtime_owner_uid=runtime_owner_uid,
            )
        return GnomeX11Backend(config_home=config_home, runner=runner, which=which)
    if compositor in ("sway", "wlroots"):
        return _UnsupportedBackend(
            compositor,
            f"{compositor} has no documented per-window capture-exclusion rule",
            "Share a single application window, or use niri, Hyprland, or KDE "
            "Plasma with KWin 6.7+ for compositor-level exclusion.",
        )
    if session.session_type == "x11":
        return _UnsupportedBackend(
            "x11",
            "X11 has no per-window screen-capture exclusion",
            "A screen-share client can read the whole root window. Share a "
            "single application window, or use niri, Hyprland, or KDE Plasma "
            "with KWin 6.7+ on Wayland for compositor-level exclusion.",
        )
    return _UnsupportedBackend(
        compositor,
        f"no documented capture-exclusion rule for {compositor}",
        "Share a single application window. SocketStream does not hide the panel.",
    )


class CaptureExclusion:
    """Facade used by the GTK shell, CLI, and QA probe."""

    def __init__(self, session: Optional[SessionInfo] = None, *,
                 environ: Optional[Mapping[str, str]] = None,
                 home: Optional[Path] = None,
                 runner: Optional[Runner] = None,
                 which: Optional[Which] = None,
                 proc_root: Optional[Path] = None,
                 uid: Optional[int] = None,
                 runtime_owner_uid: int = 0):
        self._environ = dict(os.environ if environ is None else environ)
        self._home = Path(home).expanduser() if home is not None else Path.home()
        self._runner = runner or _run
        self._which = which or shutil.which
        self._proc_root = Path(proc_root) if proc_root is not None else Path("/proc")
        self._uid = os.getuid() if uid is None else int(uid)
        self.session = session if session is not None else detect_session(self._environ)
        config_home = _config_home(self._environ, self._home)
        self.backend = _select_backend(
            self.session, config_home=config_home, runner=self._runner,
            which=self._which, environ=self._environ,
            proc_root=self._proc_root, uid=self._uid,
            runtime_owner_uid=runtime_owner_uid,
        )

    def status(self) -> RuleStatus:
        try:
            return self.backend.status()
        except Exception as exc:  # noqa: BLE001 - status must never crash the UI
            return RuleStatus(
                backend=getattr(self.backend, "name", "unknown"),
                supported=False, configured=False, verified=False,
                detail=f"capture-exclusion status failed: {type(exc).__name__}: {exc}",
                guidance="Check the compositor configuration manually.",
            )

    def install(self) -> RuleStatus:
        return self.backend.install()

    def remove(self) -> RuleStatus:
        return self.backend.remove()

    def note(self) -> str:
        getter = getattr(self.backend, "note", None)
        return getter() if callable(getter) else ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or manage the compositor capture-exclusion rule for the "
            "SocketStream answer panel. Installing writes the compositor's own "
            "configuration after making a .socket_stream.bak copy."
        )
    )
    parser.add_argument(
        "action", nargs="?", default="status",
        choices=("status", "install", "remove"),
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)

    exclusion = CaptureExclusion()
    try:
        status = getattr(exclusion, args.action)()
    except CaptureRuleError as exc:
        print(f"capture exclusion: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(status.as_dict(), indent=2, sort_keys=True))
    else:
        print(f"backend:    {status.backend}")
        print(f"supported:  {'yes' if status.supported else 'no'}")
        print(f"configured: {'yes' if status.configured else 'no'}")
        print(f"verified:   {'yes' if status.verified else 'no'}")
        print(f"detail:     {status.detail}")
        if status.backend == "gnome":
            print(f"deployed:   {'yes' if status.deployed else 'no'}")
            print(f"active:     {'yes' if status.runtime_active else 'no'}")
            print(f"restart:    {'yes' if status.restart_required else 'no'}")
        if status.path:
            print(f"path:       {status.path}")
        if status.guidance:
            print(f"guidance:   {status.guidance}")
        note = exclusion.note()
        if note:
            print(f"note:       {note}")
    if args.action != "status" and not status.supported:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
