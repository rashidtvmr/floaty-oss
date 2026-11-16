"""Global-shortcut model for the SocketStream desktop shells.

Pure standard library: this module imports cleanly with no GTK, no display and
no X11/Wayland session, so it can be unit tested headless. ``linux-gtk.py``
consumes it to build a :class:`ShortcutMap`, register GTK accelerators and
dispatch key events.

Honest scope: SocketStream never installs a global hotkey grab by itself and
never bypasses OS permission. In-app accelerators work while a SocketStream
window is focused. Out-of-focus global shortcuts need the desktop's own hotkey
tool (X11: ``sxhkd``/``xbindkeys``, Wayland: the
``org.freedesktop.portal.GlobalShortcuts`` portal).

See ``docs/A11Y-TREE-SHORTCUTS.md`` section 3 and ``docs/SHORTCUTS.md``.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import stat
import tempfile
from pathlib import Path

#: Stable action names, in the order used by the docs, the config file, the
#: Rust core and the desktop shells.
ACTIONS = (
    "start",
    "stop",
    "pause_resume",
    "toggle_overlay",
    "show_overlay",
    "hide_overlay",
    "copy_answer",
    "compact_toggle",
    "toggle_stealth",
    "open_settings",
    "refresh_question",
    "quit",
    "listen_audio",
    "pin_question",
    "previous_question",
    "next_question",
    "follow_latest",
)

#: Default accelerator for each action. Must match ``shortcuts.rs`` and the
#: table in ``docs/A11Y-TREE-SHORTCUTS.md`` section 3.2.
DEFAULT_BINDINGS = {
    "start": "ctrl+alt+s",
    "stop": "ctrl+alt+x",
    "pause_resume": "ctrl+alt+p",
    "toggle_overlay": "ctrl+alt+o",
    "show_overlay": "ctrl+alt+shift+o",
    "hide_overlay": "ctrl+alt+h",
    "copy_answer": "ctrl+alt+c",
    "compact_toggle": "ctrl+alt+m",
    "toggle_stealth": "ctrl+alt+k",
    "open_settings": "ctrl+alt+comma",
    "refresh_question": "ctrl+alt+r",
    "quit": "ctrl+alt+q",
    "listen_audio": "ctrl+alt+a",
    "pin_question": "ctrl+alt+b",
    "previous_question": "ctrl+alt+left",
    "next_question": "ctrl+alt+right",
    "follow_latest": "ctrl+alt+l",
}

#: Friendly labels for UI.
ACTION_LABELS = {
    "start": "Start observation",
    "stop": "Stop",
    "pause_resume": "Pause/Resume",
    "toggle_overlay": "Toggle panel",
    "show_overlay": "Show panel",
    "hide_overlay": "Hide panel",
    "copy_answer": "Copy answer",
    "compact_toggle": "Toggle compact",
    "toggle_stealth": "Toggle stealth",
    "open_settings": "Open settings",
    "refresh_question": "Refresh status",
    "quit": "Quit",
    "listen_audio": "Listen audio",
    "pin_question": "Pin question",
    "previous_question": "Previous question",
    "next_question": "Next question",
    "follow_latest": "Follow latest",
}

#: Default appearance values (top-level desktop.appearance in config).
#: ``cursor_blending`` keeps the shipped hover pass-through behavior: the
#: panel background lets hover/clicks reach the window below while the
#: interactive controls stay usable. Off makes the whole panel accept input
#: for text selection and scrolling.
DEFAULT_APPEARANCE = {
    "opacity": 0.90,
    "font_size": 16,
    "reduced_motion": False,
    "cursor_blending": True,
}

_MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "opt": "alt",
    "option": "alt",
    "shift": "shift",
    "meta": "meta",
    "cmd": "meta",
    "super": "meta",
    "win": "meta",
}

_NAMED_KEYS = frozenset({
    "space", "comma", "period", "slash", "semicolon", "apostrophe",
    "minus", "equals", "tab", "enter", "escape", "backspace",
    "left", "right", "up", "down",
})

_MODIFIER_ORDER = ("ctrl", "alt", "shift", "meta")
_MODIFIER_LABELS = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "meta": "Meta"}


class ShortcutError(ValueError):
    """Raised for an unparseable accelerator or an unknown action."""


def _valid_key(key):
    if len(key) == 1:
        return key.isascii() and (key.isalpha() or key.isdigit())
    if key in _NAMED_KEYS:
        return True
    if key.startswith("f") and key[1:].isdigit():
        return 1 <= int(key[1:]) <= 24
    return False


def _display_key(key):
    if len(key) == 1:
        return key.upper()
    if key.startswith("f") and key[1:].isdigit():
        return key.upper()
    return key[:1].upper() + key[1:]


class Accelerator:
    """A parsed accelerator: modifiers plus exactly one key."""

    __slots__ = ("ctrl", "alt", "shift", "meta", "key")

    def __init__(self, ctrl=False, alt=False, shift=False, meta=False, key=""):
        self.ctrl = bool(ctrl)
        self.alt = bool(alt)
        self.shift = bool(shift)
        self.meta = bool(meta)
        self.key = str(key)

    @classmethod
    def parse(cls, value):
        """Parse ``ctrl+alt+shift+o``-style text (case-insensitive).

        Accepts ``ctrl``/``control``, ``alt``/``opt``/``option``, ``shift`` and
        ``meta``/``cmd``/``super``/``win``. Exactly one non-modifier key is
        required. Raises :class:`ShortcutError` with a clear message.
        """
        if not isinstance(value, str) or not value.strip():
            raise ShortcutError("accelerator is empty")
        ctrl = alt = shift = meta = False
        key = ""
        for raw in value.split("+"):
            part = raw.strip().lower()
            if not part:
                raise ShortcutError("accelerator contains an empty part (check for a stray '+')")
            if part in _MODIFIER_ALIASES:
                name = _MODIFIER_ALIASES[part]
                if name == "ctrl":
                    if ctrl:
                        raise ShortcutError("duplicate modifier 'ctrl'")
                    ctrl = True
                elif name == "alt":
                    if alt:
                        raise ShortcutError("duplicate modifier 'alt'")
                    alt = True
                elif name == "shift":
                    if shift:
                        raise ShortcutError("duplicate modifier 'shift'")
                    shift = True
                else:
                    if meta:
                        raise ShortcutError("duplicate modifier 'meta'")
                    meta = True
                continue
            if not _valid_key(part):
                raise ShortcutError(f"unknown key '{part}'")
            if key:
                raise ShortcutError(f"accelerator has more than one key (found '{key}' and '{part}')")
            key = part
        if not key:
            raise ShortcutError("accelerator needs exactly one non-modifier key")
        return cls(ctrl=ctrl, alt=alt, shift=shift, meta=meta, key=key)

    def display(self):
        """Canonical display form, e.g. ``Ctrl+Alt+Shift+O``."""
        parts = [_MODIFIER_LABELS[name] for name in _MODIFIER_ORDER
                 if getattr(self, name)]
        parts.append(_display_key(self.key))
        return "+".join(parts)

    def as_gtk(self):
        """GTK accelerator syntax, e.g. ``<Control><Alt>S``."""
        parts = []
        if self.ctrl:
            parts.append("<Control>")
        if self.alt:
            parts.append("<Alt>")
        if self.shift:
            parts.append("<Shift>")
        if self.meta:
            parts.append("<Meta>")
        parts.append(_display_key(self.key))
        return "".join(parts)

    def to_config_string(self):
        """Canonical lowercase parseable form for config JSON, e.g. ``ctrl+alt+left``."""
        parts = []
        if self.ctrl:
            parts.append("ctrl")
        if self.alt:
            parts.append("alt")
        if self.shift:
            parts.append("shift")
        if self.meta:
            parts.append("meta")
        parts.append(self.key)
        return "+".join(parts)

    def __eq__(self, other):
        if not isinstance(other, Accelerator):
            return NotImplemented
        return (self.ctrl, self.alt, self.shift, self.meta, self.key) == (
            other.ctrl, other.alt, other.shift, other.meta, other.key)

    def __hash__(self):
        return hash((self.ctrl, self.alt, self.shift, self.meta, self.key))

    def __repr__(self):
        return f"Accelerator({self.display()!r})"


class ShortcutMap:
    """The resolved action -> accelerator table plus action -> callback map."""

    def __init__(self, bindings=None):
        if bindings is None:
            bindings = {action: Accelerator.parse(value)
                        for action, value in DEFAULT_BINDINGS.items()}
        elif isinstance(bindings, dict):
            bindings = dict(bindings)
        else:
            bindings = dict(bindings)
        self.bindings = bindings
        self._callbacks = {}
        self.validate()

    @classmethod
    def defaults(cls):
        return cls()

    def validate(self):
        """Reject unknown action names and duplicate accelerators."""
        seen = {}
        for action, accel in self.bindings.items():
            if action not in ACTIONS:
                raise ShortcutError(f"unknown action '{action}'")
            if not isinstance(accel, Accelerator):
                raise ShortcutError(f"action '{action}' is not bound to an Accelerator")
            display = accel.display()
            if display in seen:
                raise ShortcutError(
                    f"duplicate accelerator '{display}' ({seen[display]} and {action})")
            seen[display] = action
        return True

    def actions(self):
        """Bound action names, in the shared ``ACTIONS`` order."""
        return [action for action in ACTIONS if action in self.bindings]

    def accelerator_for(self, action):
        return self.bindings.get(action)

    def action_for(self, accel):
        if isinstance(accel, str):
            try:
                accel = Accelerator.parse(accel)
            except ShortcutError:
                return None
        for action, bound in self.bindings.items():
            if bound == accel:
                return action
        return None

    def describe(self):
        """A readable multi-line table of action and accelerator."""
        width = max((len(action) for action in self.actions()), default=0)
        return "\n".join(
            f"{action.ljust(width)}  {self.bindings[action].display()}"
            for action in self.actions()
        )

    def register(self, action, callback):
        """Bind ``callback`` (called with no arguments) to ``action``."""
        if action not in ACTIONS:
            raise ShortcutError(f"unknown action '{action}'")
        if not callable(callback):
            raise ShortcutError("callback must be callable")
        self._callbacks[action] = callback
        return callback

    def dispatch(self, accelerator):
        """Invoke the callback for ``accelerator``; return whether one ran."""
        if isinstance(accelerator, str):
            try:
                accelerator = Accelerator.parse(accelerator)
            except ShortcutError:
                return False
        action = self.action_for(accelerator)
        if action is None:
            return False
        callback = self._callbacks.get(action)
        if callback is None:
            return False
        callback()
        return True


def parse_bindings(mapping):
    """Build a :class:`ShortcutMap` from an action -> accelerator mapping."""
    if mapping is None:
        return ShortcutMap.defaults()
    if not isinstance(mapping, dict):
        raise ShortcutError("bindings must be an object of action -> accelerator")
    bindings = {action: Accelerator.parse(value)
                for action, value in DEFAULT_BINDINGS.items()}
    for action, value in mapping.items():
        if action not in ACTIONS:
            raise ShortcutError(f"unknown action '{action}'")
        if isinstance(value, Accelerator):
            bindings[action] = value
        else:
            bindings[action] = Accelerator.parse(value)
    return ShortcutMap(bindings)


def default_config_path():
    """Mirror ``socket_stream_core::config::Config::default_path`` for the shells."""
    override = os.environ.get("SOCKET_STREAM_CONFIG")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "socket_stream" / "config.json"
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".config" / "socket_stream" / "config.json"
    return Path(".socket_stream.json")


def _validate_enabled(enabled):
    if not isinstance(enabled, bool):
        raise ShortcutError("enabled must be a boolean")


# Both Linux and macOS define these flags. The zero fallbacks keep the module
# importable elsewhere; without O_NOFOLLOW the inode comparison below is the
# best-effort substitute, and non-regular files are still refused.
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_CONFIG_MODE = 0o600


def _open_regular_nofollow(path, flags, mode=_CONFIG_MODE):
    """Open ``path`` without following symlinks and require a regular file.

    ``O_NONBLOCK`` is applied before ``fstat`` so opening a FIFO cannot block
    (which is what made the previous "check then open" code hang), and
    ``O_NOFOLLOW`` turns both live and dangling links into ``ELOOP`` instead of
    silently resolving them. Returns an owned descriptor that the caller must
    close; on every refusal the descriptor is closed before raising.
    """
    path = Path(path)
    open_flags = flags | _O_CLOEXEC | _O_NONBLOCK
    if _O_NOFOLLOW:
        open_flags |= _O_NOFOLLOW
    try:
        fd = os.open(path, open_flags, mode)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ShortcutError(f"refusing to follow symlink: {path}") from exc
        if exc.errno == errno.ENOENT and os.path.islink(path):
            raise ShortcutError(f"refusing to follow dangling symlink: {path}") from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ShortcutError(f"refusing non-regular file: {path}")
        if not _O_NOFOLLOW:
            # No O_NOFOLLOW on this platform: verify the descriptor still refers
            # to the path we were asked about, so a swap is detected, not read.
            link_info = os.lstat(path)
            if (link_info.st_dev, link_info.st_ino) != (info.st_dev, info.st_ino):
                raise ShortcutError(f"config path changed while opening: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _refuse_unsafe_target(path):
    """Refuse a symlink or non-regular existing config path before writing."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise OSError(f"cannot inspect config path {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ShortcutError(f"refusing to write through symlink: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise ShortcutError(f"refusing to replace non-regular file: {path}")


def _read_config_object(path):
    """Read and parse one config file safely; ``None`` when it is missing.

    Refuses symlinks (including dangling links), non-regular files, empty
    content, invalid JSON and a JSON root that is not an object. Every loader in
    this module goes through this helper, so "safe read" means the same thing
    for shortcuts, enabled and appearance; callers decide whether to raise or
    fall back to defaults. The descriptor is closed on every path.
    """
    path = Path(path)
    try:
        fd = _open_regular_nofollow(path, os.O_RDONLY)
    except FileNotFoundError:
        if os.path.islink(path):
            raise ShortcutError(f"refusing to follow dangling symlink: {path}")
        return None
    try:
        handle = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        raw = handle.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ShortcutError(f"config file {path} is not valid UTF-8: {exc}") from exc
    if not text.strip():
        raise ShortcutError(f"malformed config JSON in {path}: file is empty")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ShortcutError(f"malformed config JSON in {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ShortcutError(f"malformed config JSON in {path}: expected a JSON object")
    return parsed


@contextlib.contextmanager
def _config_lock(target):
    """Hold the exclusive ``<config>.lock`` flock shared with the Rust core.

    The lock file is opened with ``O_NOFOLLOW`` and checked to be a regular file,
    so a symlinked or pipe lock path is refused instead of followed or blocked
    on. The descriptor is closed on every failure path, including a raising
    ``flock`` or a raising caller.
    """
    target = Path(target)
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"cannot create config directory {parent}: {exc}") from exc
    lock_path = target.with_name(target.name + ".lock")
    fd = _open_regular_nofollow(lock_path, os.O_RDWR | os.O_CREAT, _CONFIG_MODE)
    try:
        try:
            os.fchmod(fd, _CONFIG_MODE)
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield lock_path
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


def _fsync_directory(directory):
    """Best-effort directory fsync so a completed rename survives a crash."""
    try:
        fd = os.open(directory, os.O_RDONLY | _O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_json(target, data):
    """Serialize ``data`` and replace ``target`` atomically with mode 0600.

    Serialization completes before a temporary file is created, and a
    serialization failure propagates: it must never be turned into ``{}``. The
    temporary file appears in the same directory under a random ``mkstemp``
    name, is fsynced, renamed into place, and is removed on every failure path,
    so a failed save leaves the previous bytes untouched. The directory is
    fsynced when the platform supports it.
    """
    try:
        text = json.dumps(data, indent=2) + "\n"
    except (TypeError, ValueError) as exc:
        raise ShortcutError(f"cannot serialize config JSON: {exc}") from exc
    parent = target.parent
    fd = -1
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(parent), prefix=f".{target.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        os.fchmod(fd, _CONFIG_MODE)
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            fd = -1
            raise
        fd = -1
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
        _fsync_directory(parent)
    except OSError as exc:
        raise OSError(f"cannot write config file {target}: {exc}") from exc
    finally:
        if fd != -1:
            os.close(fd)
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _lock_and_update(target, mutator):
    """Apply ``mutator`` to the config object under the shared flock.

    The whole read/merge/atomic-replace sequence runs inside one exclusive
    ``<config>.lock`` flock, using the same convention as the Rust core, so a
    cooperating Python or Rust writer cannot lose the other's section. A missing
    file starts from ``{}``; symlinks, empty content, malformed JSON and a
    non-object root raise :class:`ShortcutError` without touching the existing
    bytes. Returns the path written.
    """
    target = Path(target)
    _refuse_unsafe_target(target)
    with _config_lock(target):
        _refuse_unsafe_target(target)  # re-check now that the flock is held
        existing = _read_config_object(target)
        data = mutator({} if existing is None else existing)
        if not isinstance(data, dict):
            raise ShortcutError("internal error: config mutator did not return an object")
        _atomic_write_json(target, data)
    return target


def save_config_file(bindings, *, enabled=True, path=None):
    """Validate and save shortcut bindings into the shared config JSON.

    **FULL SNAPSHOT.** :func:`parse_bindings` overlays ``bindings`` on the
    defaults, so the file always receives all 17 actions: an action omitted from
    ``bindings`` is written back at its default value. ``enabled`` defaults to
    ``True`` on purpose — the GTK layer always passes all 17 bindings plus the
    current toggle, so a direct caller asking to save bindings means "shortcuts
    on" unless it says otherwise.

    Unrelated top-level settings and unknown sibling keys inside ``shortcuts``
    are preserved. ``shortcuts.bindings`` itself is replaced by the resolved
    map, so an unknown action name in the old mapping is dropped. Malformed,
    empty or non-object existing JSON is reported and never clobbered. Returns
    the path written.
    """
    _validate_enabled(enabled)
    shortcut_map = parse_bindings(bindings)
    target = Path(path) if path is not None else default_config_path()

    def _mut(existing):
        data = dict(existing)
        if "shortcuts" in data:
            shortcuts_obj = data["shortcuts"]
            if not isinstance(shortcuts_obj, dict):
                raise ShortcutError(
                    f"malformed config JSON in {target}: 'shortcuts' must be an object")
        else:
            shortcuts_obj = {}
        new_shortcuts = dict(shortcuts_obj)
        new_shortcuts["enabled"] = bool(enabled)
        new_shortcuts["bindings"] = {
            action: shortcut_map.bindings[action].to_config_string()
            for action in ACTIONS
            if action in shortcut_map.bindings
        }
        data["shortcuts"] = new_shortcuts
        return data

    return _lock_and_update(target, _mut)


def _validate_appearance_values(values):
    if not isinstance(values, dict):
        raise ShortcutError("appearance values must be an object")
    allowed = {"opacity", "font_size", "reduced_motion", "cursor_blending"}
    unknown = set(values.keys()) - allowed
    if unknown:
        raise ShortcutError(f"unknown appearance keys: {', '.join(sorted(unknown))}")
    if "opacity" not in values or "font_size" not in values or "reduced_motion" not in values:
        raise ShortcutError("appearance requires keys: opacity, font_size, reduced_motion")
    opacity = values["opacity"]
    if isinstance(opacity, bool) or not isinstance(opacity, (int, float)):
        raise ShortcutError("opacity must be a number")
    if not (0.55 <= float(opacity) <= 1.0):
        raise ShortcutError("opacity must be between 0.55 and 1.0")
    fs = values["font_size"]
    if isinstance(fs, bool) or not isinstance(fs, int):
        raise ShortcutError("font_size must be an integer")
    if not (13 <= fs <= 22):
        raise ShortcutError("font_size must be between 13 and 22")
    rm = values["reduced_motion"]
    if not isinstance(rm, bool):
        raise ShortcutError("reduced_motion must be a boolean")
    validated = {
        "opacity": float(opacity),
        "font_size": int(fs),
        "reduced_motion": bool(rm),
    }
    # cursor_blending is optional on write so older callers keep working; the
    # loader always supplies it from DEFAULT_APPEARANCE. When present it must
    # be a real boolean and is preserved in the written object.
    if "cursor_blending" in values:
        cb = values["cursor_blending"]
        if not isinstance(cb, bool):
            raise ShortcutError("cursor_blending must be a boolean")
        validated["cursor_blending"] = cb
    return validated


def load_appearance(path=None):
    """Load ``desktop.appearance`` from the config.

    Strict loader: a missing file yields the defaults, but a symlinked or
    non-regular config path, malformed/empty/non-object JSON, a non-object
    ``desktop``/``desktop.appearance``, or out-of-range values raise
    :class:`ShortcutError` (a :class:`ValueError`) or :class:`OSError`. The GTK
    controller catches ``ValueError``/``OSError``, falls back to the defaults
    and surfaces the message rather than starting with silent garbage.
    """
    target = Path(path) if path is not None else default_config_path()
    parsed = _read_config_object(target)
    if parsed is None:
        return dict(DEFAULT_APPEARANCE)
    if "desktop" not in parsed:
        return dict(DEFAULT_APPEARANCE)
    desktop = parsed["desktop"]
    if not isinstance(desktop, dict):
        raise ShortcutError(f"malformed config JSON in {target}: 'desktop' must be an object")
    if "appearance" not in desktop:
        return dict(DEFAULT_APPEARANCE)
    appearance = desktop["appearance"]
    if not isinstance(appearance, dict):
        raise ShortcutError(
            f"malformed config JSON in {target}: 'desktop.appearance' must be an object")
    merged = dict(DEFAULT_APPEARANCE)
    for key, value in appearance.items():
        merged[key] = value
    try:
        return _validate_appearance_values(merged)
    except ShortcutError as exc:
        raise ShortcutError(f"invalid desktop.appearance in {target}: {exc}") from exc


def save_appearance(values, *, path=None):
    """Validate and save ``desktop.appearance`` into the shared config JSON.

    Validated values: ``opacity`` in 0.55..1.0, integer ``font_size`` in
    13..22, boolean ``reduced_motion`` and optional boolean ``cursor_blending``
    (the desktop shells always pass it; direct callers may omit it and the
    loader then supplies the default). Unknown appearance keys are rejected.
    Other ``desktop`` siblings, unrelated top-level settings and unknown keys
    are preserved; the read/merge/write runs under the shared config flock.
    """
    validated = _validate_appearance_values(values)
    target = Path(path) if path is not None else default_config_path()

    def _mut(existing):
        data = dict(existing)
        if "desktop" in data:
            desktop_obj = data["desktop"]
            if not isinstance(desktop_obj, dict):
                raise ShortcutError(
                    f"malformed config JSON in {target}: 'desktop' must be an object")
        else:
            desktop_obj = {}
        new_desktop = dict(desktop_obj)
        new_desktop["appearance"] = validated
        data["desktop"] = new_desktop
        return data

    return _lock_and_update(target, _mut)


def save_cursor_blending(enabled, *, path=None):
    """Save only the toolbar choice, preserving other saved appearance values.

    Read and merge inside the shared config lock so another settings writer's
    changes are retained. Unsaved Appearance previews are never passed here.
    """
    if not isinstance(enabled, bool):
        raise ShortcutError("cursor_blending must be a boolean")
    target = Path(path) if path is not None else default_config_path()

    def _mut(existing):
        data = dict(existing)
        desktop = data.get("desktop", {})
        if not isinstance(desktop, dict):
            raise ShortcutError(
                f"malformed config JSON in {target}: 'desktop' must be an object")
        appearance = desktop.get("appearance", {})
        if not isinstance(appearance, dict):
            raise ShortcutError(
                f"malformed config JSON in {target}: 'desktop.appearance' must be an object")
        appearance = dict(appearance)
        appearance["cursor_blending"] = enabled
        _validate_appearance_values({**DEFAULT_APPEARANCE, **appearance})
        data["desktop"] = {**desktop, "appearance": appearance}
        return data

    return _lock_and_update(target, _mut)


def from_config_file(path=None):
    """Load the bindings from the JSON config, falling back to the defaults.

    Any error (missing file, symlinked or non-regular path, malformed JSON,
    wrong types, an invalid accelerator) yields :meth:`ShortcutMap.defaults`
    rather than raising, so the desktop shell always has a usable map.
    """
    target = Path(path) if path is not None else default_config_path()
    try:
        data = _read_config_object(target)
    except (ShortcutError, OSError, ValueError):
        return ShortcutMap.defaults()
    if data is None:
        return ShortcutMap.defaults()
    try:
        shortcuts = data.get("shortcuts", {})
        if not isinstance(shortcuts, dict):
            return ShortcutMap.defaults()
        return parse_bindings(shortcuts.get("bindings", {}))
    except (ShortcutError, ValueError):
        return ShortcutMap.defaults()


def enabled_from_config_file(path=None):
    """Whether shortcuts are enabled in the config; defaults to ``True``.

    Best-effort like :func:`from_config_file`: any problem (missing file,
    symlinked path, malformed JSON, wrong type) keeps the default.
    """
    target = Path(path) if path is not None else default_config_path()
    try:
        data = _read_config_object(target)
    except (ShortcutError, OSError, ValueError):
        return True
    if data is None:
        return True
    shortcuts = data.get("shortcuts", {})
    if isinstance(shortcuts, dict) and "enabled" in shortcuts:
        return bool(shortcuts["enabled"])
    return True


def detect_platform():
    """Best-effort session type: ``"wayland"``, ``"x11"`` or ``"unknown"``."""
    if (os.environ.get("WAYLAND_DISPLAY")
            or os.environ.get("XDG_SESSION_TYPE", "").strip().lower() == "wayland"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def platform_note(platform=None):
    """The honest, one-line platform note shown next to the shortcut list."""
    platform = platform or detect_platform()
    if platform == "x11":
        return ("Out-of-focus global shortcuts need your desktop's own hotkey tool "
                "(sxhkd or xbindkeys); SocketStream installs nothing by itself.")
    if platform == "wayland":
        return ("Out-of-focus global shortcuts need the "
                "org.freedesktop.portal.GlobalShortcuts portal; SocketStream installs "
                "nothing by itself.")
    return ("Session type not detected; shortcuts work only while the SocketStream "
            "window is focused. No global grab is installed.")
