#!/usr/bin/env python3
"""Visible, consent-based Linux GTK3 companion to the Rust SocketStream CLI."""
import os
import json
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(os.environ.get("SOCKET_STREAM_HOME", Path(__file__).resolve().parents[3])).resolve()
_DEFAULT_CLI = ROOT / "bin/socket_stream"
if not _DEFAULT_CLI.is_file():
    _DEFAULT_CLI = ROOT / "target/debug/socket_stream"
CLI = Path(os.environ.get("SOCKET_STREAM_CLI", str(_DEFAULT_CLI)))

# The shortcut model is pure stdlib and GTK-free, so it is safe to import at
# module level (the module must stay importable without GTK).
_UI_DIR = Path(__file__).resolve().parent
if str(_UI_DIR) not in sys.path:
    sys.path.insert(0, str(_UI_DIR))

from shortcuts import (  # noqa: E402  (path set just above)
    ACTION_LABELS,
    DEFAULT_APPEARANCE,
    Accelerator,
    ShortcutError,
    ShortcutMap,
    enabled_from_config_file,
    from_config_file,
    load_appearance,
    parse_bindings,
    save_appearance as save_appearance_config,
    save_config_file,
    save_cursor_blending,
)
from answer_format import question_title  # noqa: E402
from ui_theme import style  # noqa: E402
from provider_settings import (  # noqa: E402  (path set just above)
    clear_provider_settings,
    load_provider_settings,
    save_provider_settings,
)


def capture_status(proc, stopping=False):
    if proc is None or proc.poll() is not None:
        return "Stopped"
    return "Stopping" if stopping else "Active"


def observe_args(provider, interval=5):
    if provider not in ("mock", "codex", "openai-compatible", "ollama"):
        raise ValueError("Unsupported provider")
    if not 2 <= interval <= 3600:
        raise ValueError("Interval must be 2-3600 seconds")
    return ["observe", "--authorized", "--interval", str(interval),
            "--max-captures", "1000", "--provider", provider]


def floating_panel_size(compact):
    """Return normal window dimensions for visible full and compact layouts."""
    from floating_overlay import COMPACT_PANEL_SIZE, PANEL_SIZE

    return COMPACT_PANEL_SIZE if compact else PANEL_SIZE


def minimize_app_window(window):
    """Minimize only the main UI without stopping the session or panel."""
    window.iconify()


# GDK key names for the named keys our accelerator grammar accepts. Letters,
# digits and function keys are handled by gtk_key_name below.
_GTK_KEY_NAMES = {
    "space": "space",
    "comma": "comma",
    "period": "period",
    "slash": "slash",
    "semicolon": "semicolon",
    "apostrophe": "apostrophe",
    "minus": "minus",
    "equals": "equal",
    "tab": "Tab",
    "enter": "Return",
    "escape": "Escape",
    "backspace": "BackSpace",
    "left": "Left",
    "right": "Right",
    "up": "Up",
    "down": "Down",
}


def gtk_key_name(key):
    """Map a parsed accelerator key to the GDK key name for AccelGroup."""
    if key in _GTK_KEY_NAMES:
        return _GTK_KEY_NAMES[key]
    if key.startswith("f") and key[1:].isdigit():
        return key.upper()
    return key


def gtk_modifier_mask(Gdk, accelerator):
    """Translate an Accelerator's modifiers into a Gdk.ModifierType mask."""
    types = Gdk.ModifierType
    mask = 0
    if accelerator.ctrl:
        mask |= types.CONTROL_MASK
    if accelerator.alt:
        # GTK3 has no ALT_MASK; Alt is MOD1.
        mask |= getattr(types, "MOD1_MASK", 1 << 3)
    if accelerator.shift:
        mask |= types.SHIFT_MASK
    if accelerator.meta:
        # Our portable Meta/Cmd/Super spelling maps to the physical Super key
        # on Linux. GDK's META_MASK is a different virtual modifier (often Alt).
        mask |= getattr(types, "SUPER_MASK", 1 << 26)
    return mask


def sanitize_manual_question(text):
    """Flatten a typed panel question into one bounded stdin control line.

    Newlines and tabs become single spaces (the stdin protocol is line-based)
    and the question is bounded to the panel entry's 2000-character limit.
    """
    return " ".join(str(text or "").split())[:2000]


def terminate_tree(proc):
    """Terminate only child processes started in our private process group."""
    if proc is None or proc.poll() is not None:
        return False
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def create_window():
    """Construct the real GTK controller without starting its main loop."""
    from live_state import LiveState
    from workspace_ui import build_workspace
    from capture_rules import CaptureExclusion, CaptureRuleError
    from overlay_guard import (
        backend_is_x11, build_plan, capability_report, detect_session,
        ensure_panel_backend, ScreenShareWatcher,
    )

    session = detect_session()
    # Must run before GTK is imported: GDK picks its backend once per process.
    # On GNOME Wayland this selects XWayland so the panel can use the dock
    # layer and stay above fullscreen windows.
    panel_backend = ensure_panel_backend(session)
    panel_x11 = backend_is_x11(panel_backend)

    from floating_overlay import FloatingOverlay
    try:
        import gi
        gi.require_version("Gdk", "3.0")
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk, Gdk, GLib, Pango
    except (ImportError, ValueError) as exc:
        raise SystemExit(f"GTK3 unavailable: {exc}. Install python3-gi and gir1.2-gtk-3.0.")

    capture_exclusion = CaptureExclusion(session=session)
    initial_capture_status = capture_exclusion.status()
    plan = build_plan(
        session, compositor_skip_capture=initial_capture_status.configured,
        xwayland_dock=panel_x11,
        runtime_active=initial_capture_status.runtime_active,
    )

    class SocketStream(Gtk.Window):
        def __init__(self):
            super().__init__(title="SocketStream — Stopped")
            self._exiting = False
            self._process_generation = 0
            self._events_closed = False
            self._live_mode = False
            # True only while a stdin-controlled live command (observe-live or
            # audio-live) owns the child; one-shot ask runs accept no controls.
            self._stdin_live = False
            self._pause_pending = False
            self._accel_group = None
            self._recording_action = ""
            self._navigating = False
            self._syncing_blending = False
            self._history_signature = None
            self._force_stop_source = None
            self.proc = None
            self.local_ai = None
            self.stopping = False
            self.pause_supported = False
            self.active_provider = "not selected"
            self.live_state = LiveState()
            self.share_timeout = None
            self.share_polls = 0
            self.capture_exclusion = capture_exclusion
            self.capture_rules = initial_capture_status
            self.capture_busy = False
            self.share_watcher = ScreenShareWatcher(interval=2.0)
            self.env = {
                name: os.environ.get(name, default)
                for name, default in {
                    "SOCKET_STREAM_BASE_URL": "https://api.openai.com/v1",
                    "SOCKET_STREAM_MODEL": "",
                    "SOCKET_STREAM_API_KEY": "",
                    "SOCKET_STREAM_OLLAMA_URL": "http://127.0.0.1:11434/v1",
                    "SOCKET_STREAM_OLLAMA_MODEL": "qwen2.5-coder:1.5b",
                }.items()
            }
            # Saved provider settings (API base/model/key) restore into memory;
            # the file is written with user-only permissions (0600).
            self.env.update(load_provider_settings())
            appearance_error = ""
            try:
                self.appearance = load_appearance()
            except (ValueError, OSError) as exc:
                self.appearance = dict(DEFAULT_APPEARANCE)
                appearance_error = f"Appearance defaults loaded: {exc}"
            self.floating = FloatingOverlay(
                plan=plan, session=session, stealth=True, state=self.live_state,
                on_stop=self.stop, on_pause=self.toggle_pause,
                on_minimize=self.minimize_app, on_settings=self.open_appearance,
                on_selection_changed=self.refresh_session,
                on_ask=self.send_manual_question, on_capture=self.manual_capture,
                on_cursor_blending=self.on_panel_cursor_blending,
                **self.appearance,
            )
            build_workspace(self)
            self._install_shortcuts()
            self._fill_shortcut_form()
            self.preview_appearance()
            self.sync_provider_label()
            self.refresh_session()
            self.report_capabilities()
            self.share_timeout = GLib.timeout_add_seconds(2, self.poll_share_state)
            self.poll_share_state()
            if appearance_error:
                self.feedback(self.appearance_message, appearance_error, error=True)
                self.write(appearance_error + "\n")
            else:
                self.footer_status.set_text("Ready · Choose a source to begin.")
            self.connect("delete-event", self.close_to_background)
            self.connect("destroy", self.on_exit)

        def add_button(self, row, text, handler):
            button = Gtk.Button(label=text)
            button.connect("clicked", handler)
            row.pack_start(button, False, False, 0)
            return button

        def _set_pause_controls(self, label=None, sensitive=None):
            for button in (self.floating.pause_button, self.control_pause_button):
                if label is not None:
                    button.set_label(label)
                if sensitive is not None:
                    button.set_sensitive(sensitive)

        # ---- workspace ---------------------------------------------------

        def navigate(self, name):
            if self._navigating or name not in self.nav_buttons:
                return
            self._navigating = True
            try:
                if name != "shortcuts":
                    self._cancel_shortcut_recording()
                self.workspace_stack.set_visible_child_name(name)
                for key, button in self.nav_buttons.items():
                    button.set_active(key == name)
            finally:
                self._navigating = False

        def open_appearance(self):
            self.navigate("appearance")
            self.restore_from_background()

        @staticmethod
        def feedback(label, text, error=False):
            label.set_text(text)
            context = label.get_style_context()
            if error:
                context.add_class("ss-error")
            else:
                context.remove_class("ss-error")

        def sync_provider_label(self):
            self.provider_hint.set_text(self.provider_display())

        def select_question(self, request_id):
            self.floating.pin_question(request_id)
            self.show_answer_panel()

        def refresh_session(self):
            view = self.live_state.display_snapshot()
            history = self.live_state.history()
            self.question_count.set_text(f"{len(history)} question{'s' if len(history) != 1 else ''}")
            self.current_question.set_text(
                question_title(view["question_display"], view["answer"], limit=180)
                or "Your recognized questions will appear here.")
            self.workspace_pin_button.set_label("Unpin question" if view["pinned"] else "Pin current question")
            self.workspace_pin_button.set_sensitive(bool(view["request_id"]))
            self.workspace_latest_button.set_sensitive(view["pinned"])
            self.workspace_latest_button.set_label(
                f"Follow latest · {view['newer_count']} new" if view["newer_count"] else "Follow latest")
            signature = (view["request_id"], view["pinned"], tuple(
                (item["request_id"], item["question_display"], item["status"]) for item in history))
            if signature != self._history_signature:
                self._history_signature = signature
                for row in self.session_questions.get_children():
                    self.session_questions.remove(row)
                    row.destroy()
                for item in reversed(history):
                    row = Gtk.ListBoxRow()
                    row.request_id = item["request_id"]
                    content = Gtk.Box(spacing=10)
                    title = Gtk.Label(label=question_title(item["question_display"], limit=120))
                    title.set_xalign(0)
                    title.set_width_chars(1)
                    title.set_ellipsize(Pango.EllipsizeMode.END)
                    content.pack_start(title, True, True, 0)
                    status = "Pinned" if view["pinned"] and row.request_id == view["request_id"] else {
                        "thinking": "Answering", "ready": "Answered", "interrupted": "Partial",
                        "paused": "Paused", "error": "Error"}.get(item["status"], "")
                    content.pack_end(style(Gtk.Label(label=status), "ss-muted"), False, False, 0)
                    row.add(content)
                    self.session_questions.add(row)
                    if row.request_id == view["request_id"]:
                        self.session_questions.select_row(row)
                self.session_questions.show_all()
            self.set_status()

        def appearance_values(self):
            values = dict(opacity=round(self.opacity_scale.get_value() / 100, 2),
                          font_size=self.font_size.get_value_as_int(),
                          reduced_motion=self.reduced_motion.get_active())
            if getattr(self.floating, "input_shape_supported", False):
                values["cursor_blending"] = self.cursor_blending_check.get_active()
            else:
                # Native Wayland cannot apply input shapes; keep the stored
                # preference instead of silently rewriting it to off.
                values["cursor_blending"] = bool(self.appearance.get("cursor_blending", True))
            return values

        def preview_appearance(self, _widget=None):
            values = self.appearance_values()
            self.opacity_value.set_text(f"{round(values['opacity'] * 100)}%")
            self.floating.set_appearance(**values)
            settings = Gtk.Settings.get_default()
            animate = not values["reduced_motion"] and settings.get_property("gtk-enable-animations")
            self.workspace_stack.set_transition_duration(160 if animate else 0)
            context = self.get_style_context()
            if animate:
                context.remove_class("ss-reduced-motion")
            else:
                context.add_class("ss-reduced-motion")
            self.feedback(self.appearance_message,
                          "Preview updated · Save to keep these settings." if values != self.appearance else "")

        def on_cursor_blending_toggled(self, button):
            """Appearance checkbox: preview the panel live; Save persists it."""
            if getattr(self, "_syncing_blending", False):
                return
            self.floating.set_cursor_blending(button.get_active(), notify=False)
            self.preview_appearance()

        def on_panel_cursor_blending(self, enabled):
            """Panel toggle changed cursor blending: mirror it and save the choice.

            The panel toggle is a live control, so the choice is persisted
            immediately through the shared atomic config; a failed save stays
            visible in the Appearance message and the diagnostics log.
            """
            self._syncing_blending = True
            try:
                self.cursor_blending_check.set_active(bool(enabled))
            finally:
                self._syncing_blending = False
            try:
                save_cursor_blending(bool(enabled))
            except (ValueError, OSError) as exc:
                self.feedback(self.appearance_message,
                              f"Could not save cursor blending: {exc}", error=True)
                self.floating.set_status("Cursor blending changed · could not save")
                self.write("Cursor blending changed for this session, but the setting "
                           "could not be saved.\n")
            else:
                self.appearance["cursor_blending"] = bool(enabled)
                state = "on" if enabled else "off"
                pending = (" Other appearance previews still need Save."
                           if self.appearance_values() != self.appearance else "")
                self.feedback(self.appearance_message,
                              f"Cursor blending {state} · saved for next time.{pending}")
                self.write(f"Cursor blending {state} · saved for next time.\n")

        def save_appearance(self, _button=None):
            values = self.appearance_values()
            try:
                save_appearance_config(values)
            except (ValueError, OSError) as exc:
                self.feedback(self.appearance_message, f"Could not save appearance: {exc}", error=True)
                return False
            self.appearance = values
            self.feedback(self.appearance_message, "Appearance saved.")
            return True

        # ---- keyboard shortcuts ------------------------------------------

        def _install_shortcuts(self, shortcut_map=None, enabled=None):
            """Build the shortcut map and register every action.

            In-app accelerators are installed on the main window with
            Gtk.AccelGroup. Out-of-focus global shortcuts are NOT grabbed: they
            need the desktop's own hotkey tool (see docs/SHORTCUTS.md). The app
            never installs a grab silently and never bypasses OS permission.
            """
            self.shortcuts = shortcut_map if shortcut_map is not None else from_config_file()
            self.shortcuts_enabled = enabled if enabled is not None else enabled_from_config_file()
            handlers = {
                "start": lambda: self.start(None),
                "stop": lambda: self.stop(None),
                "pause_resume": lambda: self.toggle_pause(None),
                # Visibility is click-to-close: overlay shortcuts may show it,
                # but must never hide it.
                "toggle_overlay": self.floating.show,
                "show_overlay": self.floating.show,
                "hide_overlay": self._hide_overlay,
                "copy_answer": self.floating.copy_answer,
                "compact_toggle": self._toggle_compact,
                "toggle_stealth": self._toggle_stealth,
                "open_settings": self.open_appearance,
                "refresh_question": self._refresh_question,
                "quit": lambda: self.on_exit(),
                "listen_audio": lambda: self.listen_audio(None),
                "pin_question": self.floating.toggle_pin,
                "previous_question": lambda: self.floating.step_question(-1),
                "next_question": lambda: self.floating.step_question(1),
                "follow_latest": self.floating.follow_latest,
            }
            for action in self.shortcuts.actions():
                self.shortcuts.register(action, handlers[action])
            if self._accel_group is not None:
                self.remove_accel_group(self._accel_group)
                self._accel_group = None
            if not self.shortcuts_enabled:
                return
            group = Gtk.AccelGroup()
            self._accel_group = group
            self.add_accel_group(group)
            for action in self.shortcuts.actions():
                accelerator = self.shortcuts.accelerator_for(action)
                keyval = Gdk.keyval_from_name(gtk_key_name(accelerator.key))
                if not keyval:
                    self.write(f"Shortcut for {action} could not be registered.\n")
                    continue
                group.connect(
                    keyval, Gdk.ModifierType(gtk_modifier_mask(Gdk, accelerator)),
                    Gtk.AccelFlags.VISIBLE,
                    lambda *_args, name=action: self._run_shortcut(name),
                )

        def _run_shortcut(self, action):
            """Dispatch an action from the AccelGroup; never raise into GTK."""
            # GTK accelerators can run before an Entry's key-press handler.
            # Recording Quit/Start/Stop must never invoke those actions.
            if (self._exiting or self._recording_action
                    or self.get_focus() in self.shortcut_entries.values()):
                return False
            try:
                self.shortcuts.dispatch(self.shortcuts.accelerator_for(action))
            except Exception as exc:  # noqa: BLE001 - a shortcut must not crash the UI
                self.write(f"Shortcut '{action}' failed: {exc}\n")
            return True

        def _fill_shortcut_form(self, shortcut_map=None, enabled=None):
            self._cancel_shortcut_recording()
            values = shortcut_map if shortcut_map is not None else self.shortcuts
            for action, entry in self.shortcut_entries.items():
                entry.set_text(values.accelerator_for(action).display())
            self.shortcut_enabled.set_active(self.shortcuts_enabled if enabled is None else enabled)

        def _cancel_shortcut_recording(self):
            action = self._recording_action
            if not action:
                return
            self._recording_action = ""
            entry = self.shortcut_entries[action]
            entry.set_text(self._recording_previous)
            entry.get_style_context().remove_class("ss-recording")
            self.feedback(self.shortcut_message, "Recording cancelled.")

        def record_shortcut(self, action):
            self._cancel_shortcut_recording()
            entry = self.shortcut_entries[action]
            self._recording_action = action
            self._recording_previous = entry.get_text()
            entry.set_text("")
            entry.set_placeholder_text("Press a shortcut…")
            style(entry, "ss-recording")
            entry.grab_focus()
            self.feedback(self.shortcut_message,
                          f"Press the keys for {ACTION_LABELS[action]}. Escape cancels.")

        def record_shortcut_key(self, entry, event, action):
            if self._recording_action != action:
                return False
            types = Gdk.ModifierType
            state = event.state
            keyval = event.keyval
            # Resolve the base key before Shift (e.g. Shift+1 rather than '!').
            try:
                keymap = Gdk.Keymap.get_for_display(self.get_display())
                translated = keymap.translate_keyboard_state(
                    event.hardware_keycode, state & ~(types.SHIFT_MASK | types.LOCK_MASK), event.group)
                if translated[0]:
                    keyval = translated[1]
            except (AttributeError, TypeError):
                pass
            name = Gdk.keyval_name(Gdk.keyval_to_lower(keyval)) or ""
            if name in ("Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
                        "Super_L", "Super_R", "Meta_L", "Meta_R", "ISO_Level3_Shift"):
                return True
            ctrl = bool(state & types.CONTROL_MASK)
            alt = bool(state & types.MOD1_MASK)
            shift = bool(state & types.SHIFT_MASK)
            meta = bool(state & (types.META_MASK | types.SUPER_MASK))
            if name == "Escape" and not (ctrl or alt or shift or meta):
                self._cancel_shortcut_recording()
                return True
            key = next((key for key, gdk_name in _GTK_KEY_NAMES.items() if gdk_name == name), name.lower())
            try:
                accelerator = Accelerator.parse(Accelerator(ctrl, alt, shift, meta, key).display())
            except ShortcutError:
                self.feedback(self.shortcut_message,
                              "Choose a letter, number, arrow, function key or supported punctuation.", error=True)
                return True
            self._recording_action = ""
            entry.get_style_context().remove_class("ss-recording")
            entry.set_placeholder_text("")
            entry.set_text(accelerator.display())
            try:
                parse_bindings({key: field.get_text() for key, field in self.shortcut_entries.items()})
            except ShortcutError as exc:
                self.feedback(self.shortcut_message, str(exc), error=True)
            else:
                self.feedback(self.shortcut_message, "Shortcut recorded · Save to apply.")
            return True

        def save_shortcuts(self, _button=None):
            if self._recording_action:
                self.feedback(self.shortcut_message, "Finish recording or press Escape before saving.")
                return
            bindings = {action: entry.get_text() for action, entry in self.shortcut_entries.items()}
            enabled = self.shortcut_enabled.get_active()
            try:
                shortcut_map = parse_bindings(bindings)
                save_config_file(bindings, enabled=enabled)
            except (ValueError, OSError) as exc:
                self.feedback(self.shortcut_message, f"Could not save shortcuts: {exc}", error=True)
                return
            self._install_shortcuts(shortcut_map, enabled)
            self._fill_shortcut_form()
            self.feedback(self.shortcut_message, "Shortcuts saved and applied." if enabled else "Saved · Shortcuts disabled.")

        def reset_shortcuts(self, _button=None):
            self._fill_shortcut_form(ShortcutMap.defaults(), enabled=True)
            self.feedback(self.shortcut_message, "Default bindings restored in this form · Save to apply.")

        def _hide_overlay(self):
            """Preserve click-to-close behavior when the legacy hide hotkey is used."""
            self.write("Click Close panel on the answer panel to close it; the hide shortcut does not hide it.\n")

        def minimize_app(self, _button=None):
            """Minimize the main window; capture, inference, and panel keep running."""
            minimize_app_window(self)
            self.write("SocketStream minimized; the current session and answer panel keep running.\n")

        def _toggle_compact(self):
            button = getattr(self.floating, "compact_button", None)
            if button is None:
                return
            button.set_active(not button.get_active())

        def _toggle_stealth(self):
            self.stealth.set_active(not self.stealth.get_active())

        def _refresh_question(self):
            """Best-effort no-op: the observation process decides when a new
            question is read, so this only reports the current state."""
            state = self.live_state.snapshot()
            question = (state.get("question") or "").strip()
            self.write(
                "refresh_question is best-effort in this shell: the observation "
                "process reads the next question on its own interval.\n"
            )
            self.floating.set_status(
                "Refresh requested · waiting for the observation process"
                if not question else "Refresh requested · current question unchanged"
            )

        def show_shortcuts(self, _button=None):
            self.navigate("shortcuts")
            self.restore_from_background()

        def on_stealth_toggled(self, button):
            self.floating.set_stealth(button.get_active())

        def report_capabilities(self):
            status = self.refresh_capture_status()
            report = capability_report(
                detect_session(), compositor_skip_capture=status.configured,
                capture_rule_supported=status.supported,
                xwayland_dock=panel_x11,
                runtime_deployed=status.deployed,
                runtime_active=status.runtime_active,
                restart_required=status.restart_required,
            )
            guaranteed = "yes" if report.get("guaranteed") else "no"
            self.write("Stealth overlay capability report:\n")
            self.write(
                f"  session: {report.get('session')} · "
                f"compositor: {report.get('compositor')}\n")
            self.write(f"  mechanism: {report.get('mechanism')}\n")
            self.write(f"  guaranteed exclusion from capture: {guaranteed}\n")
            rule = report.get("capture_rule", {})
            self.write(
                "  compositor capture rule: "
                f"supported={'yes' if rule.get('supported') else 'no'} · "
                f"configured={'yes' if rule.get('configured') else 'no'} · "
                f"runtime-verified={'yes' if status.verified else 'no'}\n")
            if status.backend == "gnome":
                # Deployment, activation, guard phase and restart are separate facts.
                self.write(
                    "  custom Mutter runtime: "
                    f"deployed={'yes' if status.deployed else 'no'} · "
                    f"active={'yes' if status.runtime_active else 'no'} · "
                    f"guard={status.startup_guard or 'unknown'} · "
                    f"fallback-latched={'yes' if status.fallback_latched else 'no'} · "
                    f"restart={'yes' if status.restart_required else 'no'}\n")
            for limitation in report.get("limitations", ()):
                self.write(f"  limitation: {limitation}\n")
            self.write(f"  recommended: {report.get('recommended')}\n")
            self.write(
                "  The answer panel stays visible during screen shares. A configured "
                "rule is not proof of exclusion until a real share is checked.\n")

        def refresh_capture_status(self):
            """Read the compositor rule state and keep the row/buttons in sync."""
            status = self.capture_exclusion.status()
            self.capture_rules = status
            if status.backend == "gnome":
                text = self._gnome_capture_text(status)
            elif status.configured:
                text = f"Capture exclusion: {status.backend} rule installed"
                if status.config_validated:
                    text += " · config validated"
                text += " · capture unverified"
            elif status.supported:
                text = f"Capture exclusion: available on {status.backend} · not set up"
            else:
                text = f"Capture exclusion: unavailable ({status.backend})"
            self.capture_label.set_text(text)
            self.capture_label.set_tooltip_text(
                "\n".join(part for part in (status.detail, status.guidance) if part)
            )
            self.capture_setup_button.set_sensitive(bool(status.can_install))
            self.capture_setup_button.set_tooltip_text(status.guidance or status.detail)
            self.capture_remove_button.set_sensitive(bool(status.can_remove))
            self.capture_restart.set_visible(bool(status.restart_required))
            if status.restart_required:
                if status.configured:
                    self.capture_restart.set_text(
                        "Restart needed: log out and back in so GNOME Shell loads the "
                        "capture-exclusion runtime. SocketStream never restarts your "
                        "desktop or your session."
                    )
                else:
                    self.capture_restart.set_text(
                        "Activation changed: log out and back in so GNOME Shell restarts "
                        "with the current setup. SocketStream never restarts your desktop "
                        "or your session."
                    )
            return status

        @staticmethod
        def _gnome_capture_text(status):
            """One line for the GNOME row; the detail stays in the tooltip."""
            # The guard latched the stock fallback: logging out again cannot fix
            # it, so the row asks for a rearm instead of another restart.
            if status.fallback_latched:
                return "Capture exclusion: stock fallback latched · Set up to rearm"
            # The running shell and the on-disk setup disagree, so the restart
            # hint outranks whatever the current process happens to have
            # loaded: after logging out the setup itself changes.
            if status.restart_required:
                if status.configured:
                    return "Capture exclusion: activation installed · log out and back in"
                return "Capture exclusion: activation changed · log out and back in"
            if status.runtime_active:
                return "Capture exclusion: custom renderer enabled · capture unverified"
            if status.deployed and status.can_install:
                return "Capture exclusion: custom runtime installed · not set up"
            if not status.deployed:
                return "Capture exclusion: unavailable (gnome) · custom runtime not installed"
            return "Capture exclusion: unavailable (gnome)"

        def _apply_capture_plan(self):
            """Recompute the plan after a rule is installed or removed."""
            status = self.capture_exclusion.status()
            self.floating.plan = build_plan(
                detect_session(), compositor_skip_capture=status.configured,
                xwayland_dock=panel_x11,
                runtime_active=status.runtime_active,
            )

        def _run_capture_action(self, action):
            """Run install/remove off the main loop; the startup check is slow.

            The isolated startup probe can take up to a minute, so the action
            runs on a worker thread and the row is updated back on the main
            loop.  The buttons are disabled while it runs so a second click
            cannot start a concurrent transaction.
            """
            if getattr(self, "capture_busy", False):
                return
            self.capture_busy = True
            self.capture_setup_button.set_sensitive(False)
            self.capture_remove_button.set_sensitive(False)
            if action == "install":
                self.write(
                    "Capture exclusion: running the isolated startup check; this can "
                    "take up to a minute...\n")
            else:
                self.write("Capture exclusion: removing the managed rule...\n")

            def worker():
                try:
                    status = getattr(self.capture_exclusion, action)()
                    note = self.capture_exclusion.note()
                    error = None
                except CaptureRuleError as exc:
                    status, note, error = None, "", str(exc)
                except Exception as exc:  # noqa: BLE001 - never crash the UI thread
                    status, note, error = None, "", f"{type(exc).__name__}: {exc}"
                GLib.idle_add(finish, status, note, error)
                return False

            def finish(status, note, error):
                self.capture_busy = False
                if self._exiting:
                    return False
                if error is not None:
                    self.write(f"Capture exclusion was not {action}ed: {error}\n")
                    self.refresh_capture_status()
                    return False
                self.refresh_capture_status()
                if action == "install":
                    self.write(f"Capture exclusion: {status.detail}\n")
                    if note:
                        self.write(f"  {note}\n")
                    if status.path:
                        self.write(f"  rule file: {status.path}\n")
                    self.write(
                        "  The panel stays visible. Verify with a real whole-screen "
                        "share; runtime exclusion is not asserted.\n")
                else:
                    self.write("Capture exclusion rule removed.\n")
                    if note:
                        self.write(f"  {note}\n")
                    self.write(f"  {status.detail}\n")
                    if status.restart_required:
                        self.write(
                            "  Activation changed: the running shell still uses the "
                            "previous setup, so log out and back in. SocketStream never "
                            "restarts your desktop or your session.\n")
                self._apply_capture_plan()
                self.report_capabilities()
                return False

            threading.Thread(target=worker, daemon=True).start()

        def setup_capture_exclusion(self, _button=None):
            self._run_capture_action("install")

        def remove_capture_exclusion(self, _button=None):
            self._run_capture_action("remove")

        def show_capture_details(self, _button=None):
            status = self.refresh_capture_status()
            lines = [
                f"Backend: {status.backend}",
                f"Supported: {'yes' if status.supported else 'no'}",
                f"Configured: {'yes' if status.configured else 'no'}",
                f"Config validated: {'yes' if status.config_validated else 'no'}",
                f"Runtime verified: {'yes' if status.verified else 'no'} "
                "(a real screen share is required)",
            ]
            if status.backend == "gnome":
                lines.extend([
                    f"Custom runtime deployed: {'yes' if status.deployed else 'no'}",
                    f"Runtime active in GNOME Shell: "
                    f"{'yes' if status.runtime_active else 'no'}",
                    f"Startup guard: {status.startup_guard or 'unknown'}",
                    f"Stock fallback latched: {'yes' if status.fallback_latched else 'no'}",
                    f"Restart required: {'yes' if status.restart_required else 'no'}",
                ])
            lines.append(f"Detail: {status.detail}")
            if status.path:
                lines.append(f"Rule file: {status.path}")
            if status.guidance:
                lines.append(f"Guidance: {status.guidance}")
            self.write("\n".join(lines) + "\n")
            self.navigate("tools")

        def poll_share_state(self):
            active = self.share_watcher.poll()
            self.floating.apply_share_state(active)
            self.share_polls += 1
            if self.share_polls % 10 == 0:
                self.refresh_capture_status()
            status = self.share_watcher.status()
            if active:
                text = "Screen share: detected — answer panel remains visible and may be captured"
            elif status.get("checked"):
                text = "Screen share: none detected"
            else:
                text = "Screen share: none detected (probe unavailable)"
            detail = status.get("detail") or ""
            self.share_status.set_text(text + (f" · {detail}" if detail else ""))
            return True

        def selected_provider(self):
            return self.provider.get_active_text() or "mock"

        def provider_display(self):
            selected = self.selected_provider()
            if selected == "mock":
                return "mock · preview only"
            if selected == "ollama":
                model = self.env.get("SOCKET_STREAM_OLLAMA_MODEL", "")
            elif selected == "openai-compatible":
                model = self.env.get("SOCKET_STREAM_MODEL", "")
            else:
                return "Codex CLI"
            return f"{selected} · {model}" if model else f"{selected} · model not configured"

        def set_status(self, detail=""):
            process_status = capture_status(self.proc, self.stopping)
            view = self.live_state.display_snapshot()
            active = self.proc is not None and self.proc.poll() is None
            self.set_title(f"SocketStream — {process_status}")
            label = process_status
            if active and self._live_mode and not self.stopping:
                label = {"observing": "Listening" if view["source"] in ("microphone", "playback") else "Reading screen",
                         "thinking": "Answering", "paused": "Paused", "error": "Needs attention",
                         "ready": "Listening" if view["source"] in ("microphone", "playback") else "Reading screen",
                         "idle": "Starting", "stopped": "Stopped"}.get(view["session_status"], "Active")
            self.status.set_text(label)
            message = detail or view["live_error"]
            if not message:
                if active:
                    message = ("Capture paused · Your answers stay available." if view["session_status"] == "paused"
                               else "Question pinned · New questions are saved in this session." if view["pinned"]
                               else "Answers appear in the floating panel." if self._live_mode else "Processing · See Tools & diagnostics.")
                else:
                    message = "Session ended · Your answers stay here until you start again." if self.live_state.history() else "Choose a source to begin. Nothing is being captured."
            self.feedback(self.session_message, message, error=bool(view["live_error"]))
            self.control_stop_button.set_sensitive(active and not self.stopping)
            self.floating.stop_button.set_sensitive(active and not self.stopping)
            for widget in (self.start_button, self.listen_button, self.provider):
                widget.set_sensitive(self.proc is None)
            self._set_pause_controls(
                label="Resume" if view["session_status"] == "paused" else "Pause",
                sensitive=active and self.pause_supported and not self._pause_pending
                and not self.stopping and view["session_status"] != "stopped")

        def write(self, message):
            if self._exiting:
                return False
            buffer = self.output.get_buffer()
            buffer.insert(buffer.get_end_iter(), message)
            excess = buffer.get_char_count() - 90000
            if excess > 0:
                buffer.delete(buffer.get_start_iter(), buffer.get_iter_at_offset(excess))
            mark = buffer.create_mark(None, buffer.get_end_iter(), False)
            self.output.scroll_to_mark(mark, 0, False, 0, 0)
            buffer.delete_mark(mark)
            if message.strip():
                self.footer_status.set_text(message.strip().splitlines()[-1][:240])
            return False

        def show_answer_panel(self, _button=None):
            # Focus and share state never hide the panel; full-screen captures
            # may include it because Linux exclusion is only best effort.
            self.floating.show()

        def replay_demo(self, _button=None):
            fixture = ROOT / "tests/fixtures/live_practice/events.jsonl"
            if not fixture.is_file():
                self.write("Synthetic replay fixture is not installed.\n")
                return
            if self.run_cli(["replay-events", "--file", str(fixture)]):
                self.show_answer_panel()

        def apply_live_event(self, event):
            if isinstance(event, dict) and event.get("type") == "session_archive":
                self.write(f"Saving a local session archive: {event.get('text', '')}\n")
                return
            if self._events_closed or self._exiting:
                return False
            if not self.live_state.apply(event):
                return False
            if event.get("type") == "session_started":
                self.pause_supported = self.proc is not None and self.proc.stdin is not None
            elif event.get("type") == "session_stopped":
                self.pause_supported = False
            if event.get("type") in ("session_started", "session_paused", "session_resumed", "session_stopped", "error"):
                self._pause_pending = False
            self.floating.render_state(self.active_provider)
            self.refresh_session()
            return False

        def run_cli(self, arguments):
            if self.proc is not None:
                self.write("An operation is already running; select Stop first.\n")
                return False
            if not CLI.is_file():
                self.write(f"CLI not installed: {CLI}\n")
                return False
            env = os.environ.copy()
            env.update(self.env)
            env["SOCKET_STREAM_GUI_PID"] = str(os.getpid())
            env["SOCKET_STREAM_HOME"] = str(ROOT)
            live_mode = arguments[0] in ("observe-live", "audio-live", "replay-events", "ask")
            # One-shot ask feeds the same JSONL event stream into the panel but
            # accepts no stdin controls; only the long-running live commands do.
            self._stdin_live = arguments[0] in ("observe-live", "audio-live")
            try:
                proc = subprocess.Popen(
                    [str(CLI), *arguments], cwd=str(ROOT), env=env,
                    stdin=subprocess.PIPE if self._stdin_live else None,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL if live_mode else subprocess.STDOUT,
                    text=True, bufsize=1, errors="replace", start_new_session=True,
                )
            except OSError as exc:
                self.pause_supported = False
                self._set_pause_controls(sensitive=False)
                self.write(f"CLI launch failed: {exc}\n")
                self.set_status("Launch failed")
                return False
            self.proc = proc
            self._process_generation += 1
            generation = self._process_generation
            self._events_closed = False
            self._live_mode = live_mode
            self._pause_pending = False
            self.stopping = False
            self.pause_supported = False
            if live_mode:
                self.live_state = LiveState()
                self.floating.bind_state(self.live_state)
                self.active_provider = "Synthetic replay" if arguments[0] == "replay-events" else self.provider_display()
                self.floating.render_state(self.active_provider)
                self.floating.set_status("Starting replay" if arguments[0] == "replay-events" else "Checking provider")
                self.refresh_session()
                self.navigate("session")
            else:
                self.navigate("tools")
            self.set_status("Checking provider…" if live_mode else "Processing · See Tools & diagnostics.")

            # Bound reader-to-main-loop ownership, not just the OS pipe. If
            # GTK stalls, stop draining and terminate this owned CLI session.
            pending_ui = threading.BoundedSemaphore(32)

            def deliver(action, *values):
                if not pending_ui.acquire(timeout=1):
                    proc.terminate()
                    return False

                def consume():
                    try:
                        if (self.proc is proc and generation == self._process_generation
                                and not self._exiting and not (live_mode and self._events_closed)):
                            action(*values)
                    finally:
                        pending_ui.release()
                    return False

                GLib.idle_add(consume)
                return True

            def reader():
                try:
                    for line in proc.stdout or ():
                        if live_mode:
                            try:
                                event = json.loads(line)
                            except (ValueError, TypeError):
                                if not deliver(self.write, "Invalid live event received.\n"):
                                    break
                                continue
                            if not deliver(self.apply_live_event, event):
                                break
                        else:
                            if not deliver(self.write, line):
                                break
                    returncode = proc.wait()
                except Exception as exc:
                    returncode = -1
                    deliver(self.write, f"Output reader error: {exc}\n")
                    terminate_tree(proc)
                    try:
                        proc.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
                        proc.wait()
                finally:
                    for stream in (proc.stdin, proc.stdout):
                        if stream is not None:
                            try:
                                stream.close()
                            except OSError:
                                pass

                def finished():
                    if self.proc is not proc or generation != self._process_generation:
                        return False
                    self._events_closed = True
                    self.proc = None
                    self.stopping = False
                    self.pause_supported = False
                    self._stdin_live = False
                    self._pause_pending = False
                    if self._force_stop_source is not None:
                        GLib.source_remove(self._force_stop_source)
                        self._force_stop_source = None
                    if live_mode:
                        self.live_state.stop()
                        if not self._exiting:
                            self.floating.render_state(self.active_provider)
                            self.refresh_session()
                    if self._exiting:
                        return False
                    self.set_status()
                    self.write(f"\nCommand exited with status {returncode}.\n")
                    return False

                GLib.idle_add(finished)

            threading.Thread(target=reader, daemon=True).start()
            return True

        def toggle_pause(self, _button=None):
            proc = self.proc
            if (not self.pause_supported or self._pause_pending or self.stopping
                    or proc is None or proc.poll() is not None or proc.stdin is None):
                return
            status = self.live_state.snapshot()["status"]
            command = "resume" if status == "paused" else "pause"
            try:
                proc.stdin.write(command + "\n")
                proc.stdin.flush()
                self._pause_pending = True
                self._set_pause_controls(sensitive=False)
                self.floating.set_status(
                    "Resume requested" if command == "resume" else "Pause requested"
                )
            except (BrokenPipeError, OSError):
                self.write("Could not send pause/resume to the observation process.\n")

        def send_manual_question(self, text):
            """Typed question from the answer panel.

            Inside a stdin-controlled live session it travels as a ``question``
            control line and is answered with the latest screen context.
            Otherwise it runs as a one-shot ``ask`` command that streams the
            same JSONL events into the panel.
            """
            question = sanitize_manual_question(text)
            if not question:
                return
            proc = self.proc
            if self._stdin_live and proc is not None and proc.poll() is None and proc.stdin is not None:
                try:
                    proc.stdin.write(f"question {question}\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    self.write("Could not send the question to the live session.\n")
                return
            if proc is not None:
                self.write("Stop the active operation before asking a standalone question.\n")
                return
            if self.run_cli(["ask", "--question", question,
                             "--provider", self.selected_provider()]):
                self.show_answer_panel()

        def manual_capture(self):
            """Capture button: read the screen now and answer what is on it.

            Inside a live session this is a ``read-screen`` control line (the
            read becomes the question). Otherwise it runs one-shot
            ``ask --read-screen`` with the same consent confirmation as the
            observation start.
            """
            proc = self.proc
            if self._stdin_live and proc is not None and proc.poll() is None and proc.stdin is not None:
                try:
                    proc.stdin.write("read-screen\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    self.write("Could not request a screen read from the live session.\n")
                return
            if proc is not None:
                self.write("Stop the active operation before running a standalone capture.\n")
                return
            if not self.confirmation("Read the screen once?",
                    "The app reads foreground accessibility text and, when no text "
                    "is readable, takes a local screenshot for OCR and model vision. "
                    "Only capture applications where assistance is permitted."):
                self.set_status("Permission declined")
                return
            args = ["ask", "--read-screen", "--authorized", "--allow-ocr",
                    "--provider", self.selected_provider()]
            if self.run_cli(args):
                self.show_answer_panel()

        def confirmation(self, title, explanation):
            dialog = Gtk.MessageDialog(
                transient_for=self, flags=0,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.OK_CANCEL, text=title)
            style(dialog, "ss-dialog")
            dialog.format_secondary_text(explanation)
            accepted = dialog.run() == Gtk.ResponseType.OK
            dialog.destroy()
            return accepted

        def start(self, _button):
            if self.proc is not None:
                self.write("Stop the active operation before starting screen reading.\n")
                return
            if not self.confirmation("Start authorized screen reading?",
                    "The app reads foreground accessibility text and may ask for "
                    "screen selection through the OS ScreenCast portal for local OCR. "
                    "Only capture applications where assistance is permitted."):
                self.set_status("Permission declined")
                return
            try:
                args = observe_args(self.selected_provider(), self.interval.get_value_as_int())
            except ValueError as exc:
                self.write(f"{exc}\n")
                return
            if self.force_ocr.get_active():
                args.append("--allow-ocr")
            if self.portal.get_active():
                args.append("--portal")
            args[0] = "observe-live"
            self.write("Switch to a permitted question window. Stop ends observation.\n")
            if self.run_cli(args):
                self.show_answer_panel()

        def stop(self, _button=None):
            # Stop closes the presentation gate before waiting for the child:
            # queued session_started/tokens cannot reopen a stopped session.
            self._events_closed = True
            self._pause_pending = False
            self.pause_supported = False
            self._stdin_live = False
            if self._live_mode:
                self.live_state.stop()
                self.floating.render_state(self.active_provider)
                self.refresh_session()
            if self.proc is None:
                self.set_status("No active operation")
                return
            if self.stopping:
                return
            self.stopping = True
            self._set_pause_controls(sensitive=False)
            if terminate_tree(self.proc):
                self.write("Stop requested; terminating authorized capture and subprocesses.\n")
                proc = self.proc

                def force_stop():
                    self._force_stop_source = None
                    if proc.poll() is None:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                            self.write("Forced stuck operation to stop.\n")
                        except (ProcessLookupError, PermissionError):
                            pass
                    return False

                self._force_stop_source = GLib.timeout_add_seconds(4, force_stop)
            self.set_status("Stopping capture and inference…")

        def file_dialog(self, title):
            chooser = Gtk.FileChooserDialog(
                title=title, parent=self, action=Gtk.FileChooserAction.OPEN)
            chooser.add_buttons(
                Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
            path = chooser.get_filename() if chooser.run() == Gtk.ResponseType.OK else None
            chooser.destroy()
            return path

        def choose_file(self, _button):
            path = self.file_dialog("Select an authorized practice text file")
            if path:
                self.run_cli(["analyze-file", "--file", path, "--provider", self.selected_provider()])

        def choose_image(self, _button):
            path = self.file_dialog("Select a screenshot/image for local OCR")
            if path:
                self.run_cli(["analyze-image", "--file", path, "--provider", self.selected_provider()])

        def analyze_audio(self, _button):
            path = self.file_dialog("Select an authorized recording for offline transcription")
            if not path:
                return
            model = ROOT / "models/ggml-tiny.en.bin"
            if not model.is_file():
                self.write("Missing Whisper model in models/; install a GGML model first.\n")
                return
            self.run_cli(["analyze-audio", "--file", path, "--model", str(model),
                          "--provider", self.selected_provider()])

        def doctor(self, _button):
            self.run_cli(["doctor"])

        def audio_devices(self, _button):
            self.run_cli(["audio-devices"])

        def provider_models(self, _button):
            if self.selected_provider() not in ("openai-compatible", "ollama"):
                self.write("Choose OpenAI-compatible or Ollama to check the configured endpoint.\n")
                return
            self.run_cli(["provider-models", "--provider", self.selected_provider()])

        def provider_test(self, _button):
            if not self.confirmation("Test the selected AI provider?",
                    "This sends one fixed test prompt and may consume your configured provider's quota. "
                    "It does not capture or send screen/audio content."):
                return
            self.run_cli(["provider-test", "--provider", self.selected_provider(), "--confirm-inference"])

        def codex_login(self, _button=None):
            if self.proc is not None:
                self.write("Stop the active operation before signing in to Codex.\n")
                return
            if self.confirmation("Sign in with your ChatGPT account?",
                    "SocketStream opens the official Codex browser sign-in flow. "
                    "Codex stores credentials; SocketStream never requests your password."):
                self.run_cli(["codex-login", "--method", "browser"])

        def codex_status(self, _button=None):
            self.run_cli(["codex-status"])

        def codex_logout(self, _button=None):
            if self.confirmation("Sign out of Codex?",
                    "This signs out the shared official Codex CLI, including other apps using it."):
                self.run_cli(["codex-logout", "--confirm"])

        def settings(self, _button):
            dialog = Gtk.Dialog(
                title="AI provider settings",
                transient_for=self, flags=0)
            style(dialog, "ss-dialog")
            dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                               Gtk.STOCK_APPLY, Gtk.ResponseType.OK)
            grid = Gtk.Grid(column_spacing=10, row_spacing=9)
            grid.set_border_width(12)
            form = (
                ("API base or full chat-completions URL", "SOCKET_STREAM_BASE_URL"),
                ("Cloud model name", "SOCKET_STREAM_MODEL"),
                ("API key", "SOCKET_STREAM_API_KEY"),
                ("Ollama local URL", "SOCKET_STREAM_OLLAMA_URL"),
                ("Ollama local model", "SOCKET_STREAM_OLLAMA_MODEL"),
            )
            entries = {}
            for row, (label, key) in enumerate(form):
                control = Gtk.Entry()
                control.set_text(self.env.get(key, ""))
                control.set_width_chars(48)
                if key == "SOCKET_STREAM_API_KEY":
                    control.set_visibility(False)
                entries[key] = control
                grid.attach(Gtk.Label(label=label), 0, row, 1, 1)
                grid.attach(control, 1, row, 1, 1)
            persist = Gtk.CheckButton(
                label="Keep these settings for future runs (saved with user-only permissions)")
            persist.set_active(True)
            grid.attach(persist, 0, len(form), 2, 1)
            note = Gtk.Label(
                label="The API key is stored unencrypted in your home directory, "
                "readable only by your user. Clear saved settings removes the file.")
            note.set_line_wrap(True)
            note.set_xalign(0)
            grid.attach(note, 0, len(form) + 1, 2, 1)
            clear_button = Gtk.Button(label="Clear saved settings")
            clear_button.connect(
                "clicked", lambda _b: self.write(
                    "Saved provider settings removed.\n" if clear_provider_settings()
                    else "No saved provider settings found.\n"))
            grid.attach(clear_button, 0, len(form) + 2, 2, 1)
            dialog.get_content_area().add(grid)
            dialog.show_all()
            if dialog.run() == Gtk.ResponseType.OK:
                for key, control in entries.items():
                    self.env[key] = control.get_text().strip()
                if persist.get_active():
                    try:
                        path = save_provider_settings(self.env)
                        self.write(f"Provider settings saved to {path} (0600).\n")
                    except OSError as exc:
                        self.write(f"Could not save provider settings: {exc}\n")
                else:
                    self.write("Provider settings updated in memory only; key was not logged.\n")
                self.sync_provider_label()
            dialog.destroy()

        def listen_audio(self, _button):
            """Built-in Moonshine streaming speech in the normal answer panel."""
            if self.proc is not None:
                self.write("Stop the active operation before enabling audio listening.\n")
                return
            dialog = Gtk.Dialog(title="Audio listening settings", transient_for=self, flags=0)
            style(dialog, "ss-dialog")
            dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                               "Start listening", Gtk.ResponseType.OK)
            grid = Gtk.Grid(column_spacing=10, row_spacing=9)
            grid.set_border_width(12)
            source = Gtk.ComboBoxText()
            source.append_text("microphone")
            source.append_text("system")
            source.set_active(0)
            device = Gtk.Entry()
            device.set_text("default")
            speaker_role = Gtk.ComboBoxText()
            for role_id, role_label in (
                    ("interviewee", "My answers · suppress automatic answers"),
                    ("interviewer", "Other person's questions · answer questions only"),
                    ("unknown", "Mixed/unknown speakers · manual Send only"),
                    ("practice", "Solo practice · answer all finalized speech")):
                speaker_role.append(role_id, role_label)
            speaker_role.set_active_id("interviewee")
            source.connect("changed", lambda widget: speaker_role.set_active_id(
                "interviewer" if widget.get_active_text() == "system" else "interviewee"))
            for row, (label, widget) in enumerate((
                ("Audio source", source),
                ("Linux input/monitor device", device),
                ("Speech engine", Gtk.Label(label="Built-in Moonshine · English · offline")),
            )):
                grid.attach(Gtk.Label(label=label), 0, row, 1, 1)
                grid.attach(widget, 1, row, 1, 1)
            hint = Gtk.Label(
                label="microphone captures your voice; system captures audio the "
                "machine plays (YouTube, videos, calls)."
            )
            hint.set_line_wrap(True)
            hint.set_xalign(0)
            grid.attach(hint, 0, 3, 2, 1)
            # Parallel screen reading: while listening, a reader keeps the
            # latest screen text (accessibility first, OCR fallback) so spoken
            # and typed questions carry on-screen context.
            read_screen = Gtk.CheckButton(label="Read the screen in parallel (context for answers)")
            read_screen.set_active(bool(getattr(self, "read_screen_check", None)
                                        and self.read_screen_check.get_active()))
            read_screen.get_accessible().set_name("Parallel screen reading")
            grid.attach(read_screen, 0, 4, 2, 1)
            grid.attach(Gtk.Label(label="Speaker/source role"), 0, 5, 1, 1)
            grid.attach(speaker_role, 1, 5, 1, 1)
            role_note = Gtk.Label(label=(
                "Roles are assigned by you, not voice recognition. Use system audio for "
                "the other person; a mixed recording cannot identify speakers. Your answers "
                "do not trigger replies. Use Send for a clarification or to rephrase a question."))
            role_note.set_line_wrap(True)
            role_note.set_max_width_chars(65)
            grid.attach(role_note, 0, 6, 2, 1)
            save_archive = Gtk.CheckButton(label="Save audio, transcript and answers locally")
            grid.attach(save_archive, 0, 7, 2, 1)
            dialog.get_content_area().add(grid)
            dialog.show_all()
            confirmed = dialog.run() == Gtk.ResponseType.OK
            selection = (source.get_active_text(), device.get_text().strip(),
                         read_screen.get_active(), speaker_role.get_active_id(), save_archive.get_active())
            dialog.destroy()
            if not confirmed:
                return
            audio_source, device_name, parallel_screen, role, archive = selection
            explanation = ("Stream the selected source through local Moonshine speech recognition. "
                           "Finalized speech is sent to your selected AI provider for answers. "
                           "Pause/Stop end capture immediately.")
            if archive:
                explanation += " Raw audio, finalized speech, questions and answers are saved locally. Only continue with everyone's consent."
            else:
                explanation += " No recording is saved."
            if parallel_screen:
                explanation += (" With parallel screen reading, the app also reads foreground "
                                "accessibility text (local OCR fallback) as answer context; "
                                "screenshots are only sent to vision-capable providers you configure.")
            if not self.confirmation("Authorize audio practice listening?", explanation):
                return
            args = ["audio-live", "--authorized", "--source", audio_source,
                    "--stt", "moonshine", "--speaker-role", role,
                    "--provider", self.selected_provider()]
            if archive:
                folder = Gtk.FileChooserDialog(title="Choose a session archive folder", transient_for=self,
                                               action=Gtk.FileChooserAction.SELECT_FOLDER)
                folder.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Choose folder", Gtk.ResponseType.OK)
                selected = folder.get_filename() if folder.run() == Gtk.ResponseType.OK else None
                folder.destroy()
                if not selected:
                    return
                args.extend(["--session-archive", selected])
            if device_name:
                args.extend(["--device", device_name])
            if parallel_screen:
                args.extend(["--read-screen", "--allow-ocr"])
            if self.run_cli(args):
                self.show_answer_panel()

        def audio_practice(self, _button):
            if self.proc is not None:
                self.write("Stop the current operation before starting audio practice.\n")
                return
            dialog = Gtk.Dialog(title="Audio practice", transient_for=self, flags=0)
            style(dialog, "ss-dialog")
            dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                               Gtk.STOCK_OK, Gtk.ResponseType.OK)
            area = dialog.get_content_area()
            grid = Gtk.Grid(column_spacing=10, row_spacing=10)
            grid.set_border_width(12)
            source = Gtk.ComboBoxText()
            source.append_text("microphone")
            source.append_text("system")
            source.set_active(0)
            duration = Gtk.SpinButton.new_with_range(1, 900, 1)
            duration.set_value(15)
            device = Gtk.Entry()
            device.set_text("default")
            device.set_tooltip_text("PulseAudio source name (mic), or monitor source name for system audio.")
            default_model = ROOT / "models/ggml-tiny.en.bin"
            model = Gtk.Entry()
            model.set_text(str(default_model) if default_model.is_file() else "")
            model.set_width_chars(43)
            for row, (label, widget) in enumerate((
                ("Audio source", source),
                ("Duration (seconds)", duration),
                ("PulseAudio device (optional for system)", device),
                ("Whisper GGML model path", model),
            )):
                grid.attach(Gtk.Label(label=label), 0, row, 1, 1)
                grid.attach(widget, 1, row, 1, 1)
            area.add(grid)
            dialog.show_all()
            accepted = dialog.run() == Gtk.ResponseType.OK
            details = (source.get_active_text(), duration.get_value_as_int(),
                       device.get_text().strip(), model.get_text().strip())
            dialog.destroy()
            if not accepted:
                return
            audio_source, seconds, name, path = details
            if not Path(path).is_file():
                self.write("Whisper model missing. Download/select ggml-tiny.en.bin first.\n")
                return
            if not self.confirmation("Authorize audio capture for practice?",
                    "The selected local mic or playback monitor is recorded for a finite duration. "
                    "Whisper transcribes locally; only the transcript is passed to your selected provider."):
                return
            args = [
                "audio-practice", "--authorized", "--source", audio_source,
                "--duration", str(seconds), "--model", path,
                "--provider", self.selected_provider(),
            ]
            if name and (audio_source == "microphone" or name != "default"):
                args.extend(["--device", name])
            self.run_cli(args)

        def start_local_ai(self, _button):
            if self.local_ai is not None and self.local_ai.poll() is None:
                self.write("Local AI already launched by SocketStream.\n")
                return
            path = shutil.which("ollama")
            if not path:
                self.write("Ollama unavailable. Install Ollama, pull a model, then try again.\n")
                return
            try:
                env = os.environ.copy()
                env["OLLAMA_HOST"] = "127.0.0.1:11434"
                self.local_ai = subprocess.Popen(
                    [path, "serve"], env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                self.write("Local Ollama start requested at http://127.0.0.1:11434.\n")
            except OSError as exc:
                self.write(f"Failed starting local AI: {exc}\n")

        def stop_local_ai(self, _button=None):
            if terminate_tree(self.local_ai):
                self.write("Stopped the Ollama server launched by SocketStream.\n")
            self.local_ai = None

        def close_to_background(self, *_args):
            self.hide()
            return True

        def restore_from_background(self):
            if self._exiting:
                return False
            self.show_all()
            self.deiconify()
            self.present()
            return False

        def on_exit(self, *_args):
            if self._exiting:
                return
            self._exiting = True
            self.stop()
            self.stop_local_ai()
            if self.share_timeout is not None:
                GLib.source_remove(self.share_timeout)
                self.share_timeout = None
            self.hide()
            self.floating.hide()

            def finish_exit():
                # Keep the loop alive for Stop's escalation until the owned
                # CLI has exited; closing the UI must not strand capture.
                if self.proc is not None and self.proc.poll() is None:
                    return True
                self._process_generation += 1
                self.floating.destroy()
                self.destroy()
                if Gtk.main_level():
                    Gtk.main_quit()
                return False

            if self.proc is None or self.proc.poll() is not None:
                finish_exit()
            else:
                GLib.timeout_add(100, finish_exit)

    return SocketStream()


def main():
    window = create_window()
    from gi.repository import Gtk, GLib

    signal.signal(signal.SIGUSR1, lambda *_: GLib.idle_add(window.restore_from_background))
    window.show_all()
    if os.environ.get("SOCKET_STREAM_PREVIEW_DEMO") == "1":
        def show_preview():
            window.active_provider = "Local preview · no capture"
            samples = (
                ("session_started", {}),
                ("question_detected", {"request_id": "preview", "text": "How can you solve Two Sum in O(n) time?"}),
                ("answer_delta", {"request_id": "preview", "text": (
                    "Question: How can you solve Two Sum in O(n) time?\nAnswer: "
                    "Use a **hash map** to remember the indices of numbers you've already seen.\n\n"
                    "### Solution\n```python\ndef two_sum(nums, target):\n    seen = {}\n"
                    "    for i, value in enumerate(nums):\n        complement = target - value\n"
                    "        if complement in seen:\n            return [seen[complement], i]\n"
                    "        seen[value] = i\n    return []\n```\n"
                    "Look up the complement **before** inserting the current value, so an index is never reused.\n\n"
                    "**Time:** O(n) average · **Space:** O(n)\n\n"
                    "Works with duplicates, negative values, and a solution using the first element."
                )}),
                ("answer_completed", {"request_id": "preview"}),
                ("session_stopped", {}),
            )
            for sequence, (kind, fields) in enumerate(samples, 1):
                window.apply_live_event(dict(schema=1, type=kind, session_id="preview", source="file",
                                             sequence=sequence, **fields))
            window.floating.pin_question()
            window.floating.set_status("Preview · no capture")
            window.floating.show()
            try:
                screen = window.floating.window.get_screen()
                monitor = screen.get_monitor_geometry(screen.get_primary_monitor())
                width, _height = floating_panel_size(False)
                window.floating.window.move(monitor.x + max(0, monitor.width - width - 24), monitor.y + 60)
            except Exception:
                pass
            return False
        GLib.idle_add(show_preview)
    Gtk.main()


if __name__ == "__main__":
    main()
