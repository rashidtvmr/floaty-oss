"""Global-shortcut model for the ContextLens desktop shells.

Pure standard library: this module imports cleanly with no GTK, no display and
no X11/Wayland session, so it can be unit tested headless. ``linux-gtk.py``
consumes it to build a :class:`ShortcutMap`, register GTK accelerators and
dispatch key events.

Honest scope: ContextLens never installs a global hotkey grab by itself and
never bypasses OS permission. In-app accelerators work while a ContextLens
window is focused. Out-of-focus global shortcuts need the desktop's own hotkey
tool (X11: ``sxhkd``/``xbindkeys``, Wayland: the
``org.freedesktop.portal.GlobalShortcuts`` portal).

See ``docs/A11Y-TREE-SHORTCUTS.md`` section 3 and ``docs/SHORTCUTS.md``.
"""
from __future__ import annotations

import json
import os
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
                raise ShortcutError(f"accelerator has more than one key (found '{part}')")
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
        # bindings: dict action -> Accelerator, or list of (action, Accelerator).
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

    # ---- validation -----------------------------------------------------

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

    # ---- lookups --------------------------------------------------------

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

    # ---- dispatch -------------------------------------------------------

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
    """Build a :class:`ShortcutMap` from an action -> accelerator mapping.

    Starts from the defaults and overrides them with every entry in ``mapping``.
    An invalid accelerator or unknown action raises :class:`ShortcutError`; it
    is never silently ignored.
    """
    if mapping is None:
        return ShortcutMap.defaults()
    if not isinstance(mapping, dict):
        raise ShortcutError("bindings must be an object of action -> accelerator")
    bindings = {action: Accelerator.parse(value)
                for action, value in DEFAULT_BINDINGS.items()}
    for action, value in mapping.items():
        if action not in ACTIONS:
            raise ShortcutError(f"unknown action '{action}'")
        bindings[action] = Accelerator.parse(value)
    return ShortcutMap(bindings)


def default_config_path():
    """Mirror ``context_core::config::Config::default_path`` for the shells."""
    override = os.environ.get("CONTEXT_LENS_CONFIG")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "contextlens" / "config.json"
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".config" / "contextlens" / "config.json"
    return Path.home() / ".contextlens.json"


def from_config_file(path=None):
    """Load the bindings from the JSON config, falling back to the defaults.

    Any error (missing file, malformed JSON, wrong types, an invalid
    accelerator) yields :meth:`ShortcutMap.defaults` rather than raising, so the
    desktop shell always has a usable map.
    """
    try:
        target = Path(path) if path is not None else default_config_path()
        data = json.loads(target.read_text(encoding="utf-8"))
        shortcuts = data.get("shortcuts", {})
        if not isinstance(shortcuts, dict):
            return ShortcutMap.defaults()
        return parse_bindings(shortcuts.get("bindings", {}))
    except Exception:  # noqa: BLE001 - any problem falls back to defaults
        return ShortcutMap.defaults()


def enabled_from_config_file(path=None):
    """Whether shortcuts are enabled in the config; defaults to ``True``."""
    try:
        target = Path(path) if path is not None else default_config_path()
        data = json.loads(target.read_text(encoding="utf-8"))
        shortcuts = data.get("shortcuts", {})
        if isinstance(shortcuts, dict) and "enabled" in shortcuts:
            return bool(shortcuts["enabled"])
    except Exception:  # noqa: BLE001 - any problem keeps the default
        pass
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
                "(sxhkd or xbindkeys); ContextLens installs nothing by itself.")
    if platform == "wayland":
        return ("Out-of-focus global shortcuts need the "
                "org.freedesktop.portal.GlobalShortcuts portal; ContextLens installs "
                "nothing by itself.")
    return ("Session type not detected; shortcuts work only while the ContextLens "
            "window is focused. No global grab is installed.")
