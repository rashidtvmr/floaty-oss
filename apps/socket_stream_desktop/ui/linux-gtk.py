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
    enabled_from_config_file,
    from_config_file,
    platform_note,
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
    return (360, 250) if compact else (520, 370)


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
        mask |= getattr(types, "META_MASK", 1 << 28)
    return mask


def terminate_tree(proc):
    """Terminate only child processes started in our private process group."""
    if proc is None or proc.poll() is not None:
        return False
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def main():
    from live_state import LiveState
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
        from gi.repository import Gtk, Gdk, GLib
    except (ImportError, ValueError) as exc:
        raise SystemExit(f"GTK3 unavailable: {exc}. Install python3-gi and gir1.2-gtk-3.0.")

    capture_exclusion = CaptureExclusion(session=session)
    initial_capture_status = capture_exclusion.status()
    plan = build_plan(
        session, compositor_skip_capture=initial_capture_status.configured,
        xwayland_dock=panel_x11,
    )

    class SocketStream(Gtk.Window):
        def __init__(self):
            super().__init__(title="SocketStream — Stopped")
            self.set_default_size(870, 630)
            self.set_border_width(14)
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
            self.share_watcher = ScreenShareWatcher(interval=2.0)
            self.floating = FloatingOverlay(
                plan=plan, session=session, stealth=True,
                on_stop=self.stop, on_pause=self.toggle_pause,
                on_minimize=self.minimize_app,
            )
            self._install_pause_controls()
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

            main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            self.add(main_box)
            hint = Gtk.Label(label="Authorized practice only. No screen or audio capture runs until you start it.")
            hint.set_xalign(0)
            main_box.pack_start(hint, False, False, 0)
            self.status = Gtk.Label(label="Capture status: Stopped")
            self.status.set_xalign(0)
            main_box.pack_start(self.status, False, False, 0)
            self.share_status = Gtk.Label(label="Screen share: checking...")
            self.share_status.set_xalign(0)
            main_box.pack_start(self.share_status, False, False, 0)

            capture_row = Gtk.Box(spacing=8)
            main_box.pack_start(capture_row, False, False, 0)
            self.capture_label = Gtk.Label(label="Capture exclusion: checking...")
            self.capture_label.set_xalign(0)
            self.capture_label.set_line_wrap(True)
            capture_row.pack_start(self.capture_label, True, True, 0)
            self.capture_setup_button = self.add_button(
                capture_row, "Set up exclusion", self.setup_capture_exclusion)
            self.capture_remove_button = self.add_button(
                capture_row, "Remove rule", self.remove_capture_exclusion)
            self.add_button(capture_row, "Details", self.show_capture_details)

            choices = Gtk.Box(spacing=8)
            main_box.pack_start(choices, False, False, 0)
            choices.pack_start(Gtk.Label(label="Provider:"), False, False, 0)
            self.provider = Gtk.ComboBoxText()
            for name in ("mock", "ollama", "codex", "openai-compatible"):
                self.provider.append_text(name)
            self.provider.set_active(1)
            choices.pack_start(self.provider, False, False, 0)
            choices.pack_start(Gtk.Label(label="Poll every (s):"), False, False, 0)
            self.interval = Gtk.SpinButton.new_with_range(2, 3600, 1)
            self.interval.set_value(5)
            choices.pack_start(self.interval, False, False, 0)
            self.force_ocr = Gtk.CheckButton(label="Enable local OCR fallback")
            choices.pack_start(self.force_ocr, False, False, 0)
            self.portal = Gtk.CheckButton(label="Use portal")
            self.portal.set_tooltip_text("Use the user-approved ScreenCast chooser on Wayland")
            choices.pack_start(self.portal, False, False, 0)
            self.stealth = Gtk.CheckButton(label="Stealth overlay")
            self.stealth.set_active(True)
            self.stealth.set_tooltip_text(
                "Best-effort window hints only. The answer panel stays visible during "
                "focus changes and screen shares, and may appear in full-screen captures."
            )
            self.stealth.connect("toggled", self.on_stealth_toggled)
            choices.pack_start(self.stealth, False, False, 0)
            settings_actions = Gtk.Box(spacing=8)
            main_box.pack_start(settings_actions, False, False, 0)
            self.add_button(settings_actions, "Settings", self.settings)
            self.add_button(settings_actions, "Shortcuts", self.show_shortcuts)
            self.add_button(settings_actions, "Codex sign-in", self.codex_login)
            self.add_button(settings_actions, "Audio devices", self.audio_devices)
            self.add_button(settings_actions, "Doctor", self.doctor)

            controls = Gtk.Box(spacing=8)
            main_box.pack_start(controls, False, False, 0)
            self.add_button(controls, "Show answer panel", self.show_answer_panel)
            self.add_button(controls, "Quit SocketStream", self.on_exit)
            for name, handler in (
                ("Start observation", self.start),
                ("Listen to audio", self.listen_audio),
                ("Stop", self.stop),
                ("Minimize app", self.minimize_app),
            ):
                self.add_button(controls, name, handler)

            advanced = Gtk.Expander(label="Advanced tools and diagnostics")
            main_box.pack_start(advanced, True, True, 0)
            advanced_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            advanced.add(advanced_body)
            imports = Gtk.Box(spacing=8)
            advanced_body.pack_start(imports, False, False, 0)
            for name, handler in (
                ("Analyze text", self.choose_file),
                ("Analyze image", self.choose_image),
                ("Analyze audio", self.analyze_audio),
                ("Audio practice", self.audio_practice),
                ("Replay demo", self.replay_demo),
            ):
                self.add_button(imports, name, handler)

            model_buttons = Gtk.Box(spacing=8)
            advanced_body.pack_start(model_buttons, False, False, 0)
            self.add_button(model_buttons, "Start local AI", self.start_local_ai)
            self.add_button(model_buttons, "Stop local AI", self.stop_local_ai)
            note = Gtk.Label(label="ScreenCast permission is requested by your desktop. Stop ends the capture session.")
            note.set_xalign(0)
            model_buttons.pack_start(note, False, False, 0)

            provider_checks = Gtk.Box(spacing=8)
            advanced_body.pack_start(provider_checks, False, False, 0)
            self.add_button(provider_checks, "List provider models", self.provider_models)
            self.add_button(provider_checks, "Test AI response", self.provider_test)
            self.add_button(provider_checks, "Codex status", self.codex_status)
            self.add_button(provider_checks, "Codex sign-out", self.codex_logout)
            provider_checks.pack_start(Gtk.Label(label="Model listing sends no captured content."), False, False, 0)

            scroll = Gtk.ScrolledWindow()
            scroll.set_hexpand(True)
            scroll.set_vexpand(True)
            advanced_body.pack_start(scroll, True, True, 0)
            self.output = Gtk.TextView()
            self.output.set_editable(False)
            self.output.set_cursor_visible(False)
            self.output.set_monospace(True)
            self.output.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            scroll.add(self.output)
            self.write("SocketStream ready. Select a provider and test an imported file.\n")
            self.report_capabilities()
            self.share_timeout = GLib.timeout_add_seconds(2, self.poll_share_state)
            self.poll_share_state()
            self._install_shortcuts()
            self.connect("delete-event", self.close_to_background)
            self.connect("destroy", self.on_exit)

        def add_button(self, row, text, handler):
            button = Gtk.Button(label=text)
            button.connect("clicked", handler)
            row.pack_start(button, False, False, 0)
            return button

        def _install_pause_controls(self):
            """Wire the Pause/Resume control to toggle_pause.

            The overlay normally owns this button; when it exposes only a
            controls container, add the button there so the behaviour is kept.
            """
            button = getattr(self.floating, "pause_button", None)
            if button is None:
                controls = getattr(self.floating, "controls", None)
                if controls is None:
                    return
                button = Gtk.Button(label="Pause")
                button.connect("clicked", self.toggle_pause)
                controls.pack_start(button, False, False, 0)
                try:
                    self.floating.pause_button = button
                except AttributeError:
                    pass
            button.set_sensitive(False)

        def _set_pause_controls(self, label=None, sensitive=None):
            """Keep the overlay's Pause/Resume control in sync with live state."""
            button = getattr(self.floating, "pause_button", None)
            if button is None:
                return
            if label is not None:
                button.set_label(label)
            if sensitive is not None:
                button.set_sensitive(sensitive)

        # ---- keyboard shortcuts ------------------------------------------

        def _install_shortcuts(self):
            """Build the shortcut map and register every action.

            In-app accelerators are installed on the main window with
            Gtk.AccelGroup. Out-of-focus global shortcuts are NOT grabbed: they
            need the desktop's own hotkey tool (see docs/SHORTCUTS.md). The app
            never installs a grab silently and never bypasses OS permission.
            """
            self.shortcuts = from_config_file()
            self.shortcuts_enabled = enabled_from_config_file()
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
                "open_settings": lambda: self.settings(None),
                "refresh_question": self._refresh_question,
                "quit": lambda: self.on_exit(),
            }
            for action in self.shortcuts.actions():
                self.shortcuts.register(action, handlers[action])
            if not self.shortcuts_enabled:
                self.write("Shortcuts are disabled in the config file (shortcuts.enabled = false).\n")
                return
            group = Gtk.AccelGroup()
            self.add_accel_group(group)
            for action in self.shortcuts.actions():
                accelerator = self.shortcuts.accelerator_for(action)
                keyval = Gdk.keyval_from_name(gtk_key_name(accelerator.key))
                if not keyval:
                    self.write(f"Shortcut for {action} could not be registered.\n")
                    continue
                group.connect(
                    keyval, gtk_modifier_mask(Gdk, accelerator),
                    Gtk.AccelFlags.VISIBLE,
                    lambda *_args, name=action: self._run_shortcut(name),
                )

        def _run_shortcut(self, action):
            """Dispatch an action from the AccelGroup; never raise into GTK."""
            try:
                self.shortcuts.dispatch(self.shortcuts.accelerator_for(action))
            except Exception as exc:  # noqa: BLE001 - a shortcut must not crash the UI
                self.write(f"Shortcut '{action}' failed: {exc}\n")
            return True

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
            """List every action, its accelerator and the platform note."""
            dialog = Gtk.Dialog(title="Keyboard shortcuts", transient_for=self, flags=0)
            dialog.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
            area = dialog.get_content_area()
            area.set_spacing(8)
            area.set_border_width(12)
            heading = Gtk.Label(
                label="Every shortcut is also reachable from the buttons in the main window."
            )
            heading.set_xalign(0)
            area.pack_start(heading, False, False, 0)
            grid = Gtk.Grid(column_spacing=18, row_spacing=6)
            for row, action in enumerate(self.shortcuts.actions()):
                accelerator = self.shortcuts.accelerator_for(action)
                grid.attach(Gtk.Label(label=action), 0, row, 1, 1)
                value = Gtk.Label(label=accelerator.display() if accelerator else "unbound")
                value.set_xalign(0)
                grid.attach(value, 1, row, 1, 1)
            area.pack_start(grid, False, False, 0)
            if not self.shortcuts_enabled:
                disabled = Gtk.Label(
                    label="Shortcuts are disabled in the config file (shortcuts.enabled = false)."
                )
                disabled.set_xalign(0)
                area.pack_start(disabled, False, False, 0)
            for text in (
                platform_note(),
                "SocketStream never installs a global hotkey grab silently and never "
                "bypasses OS permission.",
            ):
                label = Gtk.Label(label=text)
                label.set_xalign(0)
                label.set_line_wrap(True)
                label.set_max_width_chars(64)
                area.pack_start(label, False, False, 0)
            dialog.show_all()
            dialog.run()
            dialog.destroy()

        def on_stealth_toggled(self, button):
            self.floating.set_stealth(button.get_active())

        def report_capabilities(self):
            status = self.refresh_capture_status()
            report = capability_report(
                detect_session(), compositor_skip_capture=status.configured,
                capture_rule_supported=status.supported,
                xwayland_dock=panel_x11,
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
                "runtime-verified=no\n")
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
            if status.configured:
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
            return status

        def _apply_capture_plan(self):
            """Recompute the plan after a rule is installed or removed."""
            status = self.capture_exclusion.status()
            self.floating.plan = build_plan(
                detect_session(), compositor_skip_capture=status.configured,
                xwayland_dock=panel_x11,
            )

        def setup_capture_exclusion(self, _button=None):
            try:
                status = self.capture_exclusion.install()
            except CaptureRuleError as exc:
                self.write(f"Capture exclusion was not installed: {exc}\n")
                self.refresh_capture_status()
                return
            note = self.capture_exclusion.note()
            self.write(f"Capture exclusion: {status.detail}\n")
            if note:
                self.write(f"  {note}\n")
            if status.path:
                self.write(f"  rule file: {status.path}\n")
            self.write(
                "  The panel stays visible. Verify with a real whole-screen share; "
                "runtime exclusion is not asserted.\n")
            self._apply_capture_plan()
            self.report_capabilities()

        def remove_capture_exclusion(self, _button=None):
            try:
                status = self.capture_exclusion.remove()
            except CaptureRuleError as exc:
                self.write(f"Capture exclusion was not removed: {exc}\n")
                self.refresh_capture_status()
                return
            note = self.capture_exclusion.note()
            self.write("Capture exclusion rule removed.\n")
            if note:
                self.write(f"  {note}\n")
            self.write(f"  {status.detail}\n")
            self._apply_capture_plan()
            self.report_capabilities()

        def show_capture_details(self, _button=None):
            status = self.refresh_capture_status()
            lines = [
                f"Backend: {status.backend}",
                f"Supported: {'yes' if status.supported else 'no'}",
                f"Configured: {'yes' if status.configured else 'no'}",
                f"Config validated: {'yes' if status.verified else 'no'}",
                "Runtime verified: no (a real screen share is required)",
                f"Detail: {status.detail}",
            ]
            if status.path:
                lines.append(f"Rule file: {status.path}")
            if status.guidance:
                lines.append(f"Guidance: {status.guidance}")
            self.write("\n".join(lines) + "\n")

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
            state = capture_status(self.proc, self.stopping)
            self.set_title(f"SocketStream — {state}")
            self.status.set_text(f"Capture status: {state}" + (f" — {detail}" if detail else ""))

        def write(self, message):
            buffer = self.output.get_buffer()
            buffer.insert(buffer.get_end_iter(), message)
            excess = buffer.get_char_count() - 90000
            if excess > 0:
                buffer.delete(buffer.get_start_iter(), buffer.get_iter_at_offset(excess))
            mark = buffer.create_mark(None, buffer.get_end_iter(), False)
            self.output.scroll_to_mark(mark, 0, False, 0, 0)
            buffer.delete_mark(mark)
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
            previous = self.live_state.snapshot()
            if not self.live_state.apply(event):
                return False
            if event.get("type") == "session_started":
                self.pause_supported = True
            elif event.get("type") == "session_stopped":
                self.pause_supported = False
            state = self.live_state.snapshot()
            if state["question"] != previous["question"] or state["transcript"] != previous["transcript"]:
                shown = state.get("question_display") or state["question"]
                if state["transcript"]:
                    shown = "Heard: " + state["transcript"][-450:] + "\nAnswering: " + shown[:180]
                self.floating.set_question(shown[:650] or "Waiting for a question...")
            if event.get("type") == "answer_delta" and state["answer"].startswith(previous["answer"]):
                # Append only the new text; avoid O(n²) full-buffer redraws.
                addition = state["answer"][len(previous["answer"]):]
                if addition:
                    self.floating.append_answer(addition)
            elif state["answer"] != previous["answer"]:
                self.floating.set_answer(state["answer"])
            label = state["status"].title()
            self.floating.set_status(
                "Error: " + state["error"] if state["error"]
                else f"{label} · {state['source']} · {self.active_provider}"
            )
            self._set_pause_controls(
                label="Resume" if state["status"] == "paused" else "Pause",
                sensitive=self.pause_supported and state["status"] != "stopped",
            )
            self.status.set_text(f"Practice: {label}")
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
            live_mode = arguments[0] in ("observe-live", "audio-live", "replay-events")
            if live_mode:
                self.live_state = LiveState()
                if arguments[0] in ("observe-live", "audio-live"):
                    self.active_provider = self.provider_display()
                    self.floating.set_status(f"Checking provider · {self.active_provider}")
            self.pause_supported = False
            self._set_pause_controls(label="Pause", sensitive=False)
            try:
                proc = subprocess.Popen(
                    [str(CLI), *arguments], cwd=str(ROOT), env=env,
                    stdin=subprocess.PIPE if arguments[0] in ("observe-live", "audio-live") else None,
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
            self.stopping = False
            self.set_status("Authorized practice" if live_mode else "Processing")

            # Bound reader-to-main-loop ownership, not just the OS pipe. If
            # GTK stalls, stop draining and terminate this owned CLI session.
            pending_ui = threading.BoundedSemaphore(32)

            def deliver(action, *values):
                if not pending_ui.acquire(timeout=1):
                    proc.terminate()
                    return False

                def consume():
                    try:
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
                    GLib.idle_add(self.write, f"Output reader error: {exc}\n")

                def finished():
                    if self.proc is proc:
                        self.proc = None
                        self.stopping = False
                        self.set_status("Command finished")
                        self.pause_supported = False
                        if live_mode:
                            state = self.live_state.snapshot()
                            if state["status"] == "error":
                                self.floating.set_status(
                                    f"Error ({state['source']}): {state['error']}"
                                )
                            elif state["status"] == "idle":
                                self.floating.set_status("Not started · check diagnostics")
                            elif state["status"] != "stopped":
                                self.floating.set_status("Stopped · capture and inference ended")
                        self._set_pause_controls(sensitive=False)
                    self.write(f"\nCommand exited with status {returncode}.\n")
                    return False

                GLib.idle_add(finished)

            threading.Thread(target=reader, daemon=True).start()
            return True

        def toggle_pause(self, _button=None):
            proc = self.proc
            if not self.pause_supported or proc is None or proc.poll() is not None or proc.stdin is None:
                return
            status = self.live_state.snapshot()["status"]
            command = "resume" if status == "paused" else "pause"
            try:
                proc.stdin.write(command + "\n")
                proc.stdin.flush()
                self._set_pause_controls(sensitive=False)
                self.floating.set_status(
                    "Resume requested" if command == "resume" else "Pause requested"
                )
            except (BrokenPipeError, OSError):
                self.write("Could not send pause/resume to the observation process.\n")

        def confirmation(self, title, explanation):
            dialog = Gtk.MessageDialog(
                transient_for=self, flags=0,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.OK_CANCEL, text=title)
            dialog.format_secondary_text(explanation)
            accepted = dialog.run() == Gtk.ResponseType.OK
            dialog.destroy()
            return accepted

        def start(self, _button):
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
            if self.proc is None or self.proc.poll() is not None:
                self.set_status("No active operation")
                return
            self.stopping = True
            self._set_pause_controls(sensitive=False)
            if self.live_state.snapshot()["status"] not in ("idle", "stopped"):
                self.floating.set_status("Stopping · ending capture and inference")
            if terminate_tree(self.proc):
                self.write("Stop requested; terminating authorized capture and subprocesses.\n")
                self.set_status("Stopping")
                proc = self.proc

                def force_stop():
                    if proc.poll() is None:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                            self.write("Forced stuck operation to stop.\n")
                        except ProcessLookupError:
                            pass
                    return False

                GLib.timeout_add_seconds(4, force_stop)

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
                title="AI provider settings (kept in memory only)",
                transient_for=self, flags=0)
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
            dialog.get_content_area().add(grid)
            dialog.show_all()
            if dialog.run() == Gtk.ResponseType.OK:
                for key, control in entries.items():
                    self.env[key] = control.get_text().strip()
                self.write("Provider settings updated in memory; key was not logged.\n")
            dialog.destroy()

        def listen_audio(self, _button):
            """Built-in Moonshine streaming speech in the normal answer panel."""
            if self.proc is not None:
                self.write("Stop the active operation before enabling audio listening.\n")
                return
            dialog = Gtk.Dialog(title="Audio listening settings", transient_for=self, flags=0)
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
            for row, (label, widget) in enumerate((
                ("Audio source", source),
                ("Linux input/monitor device", device),
                ("Speech engine", Gtk.Label(label="Built-in Moonshine · English · offline")),
            )):
                grid.attach(Gtk.Label(label=label), 0, row, 1, 1)
                grid.attach(widget, 1, row, 1, 1)
            dialog.get_content_area().add(grid)
            dialog.show_all()
            confirmed = dialog.run() == Gtk.ResponseType.OK
            selection = (source.get_active_text(), device.get_text().strip())
            dialog.destroy()
            if not confirmed or not self.confirmation("Authorize audio practice listening?",
                    "Stream the selected source through local Moonshine speech recognition. "
                    "Finalized speech is sent to your selected AI provider for answers. "
                    "Pause/Stop end capture immediately. No recording is saved."):
                return
            audio_source, device_name = selection
            args = ["audio-live", "--authorized", "--source", audio_source,
                    "--stt", "moonshine",
                    "--provider", self.selected_provider()]
            if device_name:
                args.extend(["--device", device_name])
            if self.run_cli(args):
                self.show_answer_panel()

        def audio_practice(self, _button):
            if self.proc is not None:
                self.write("Stop the current operation before starting audio practice.\n")
                return
            dialog = Gtk.Dialog(title="Audio practice", transient_for=self, flags=0)
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
            self.show_all()
            self.present()
            return False

        def on_exit(self, *_args):
            self.stop()
            self.stop_local_ai()
            if self.share_timeout is not None:
                GLib.source_remove(self.share_timeout)
                self.share_timeout = None
            self.floating.destroy()
            Gtk.main_quit()

    window = SocketStream()
    signal.signal(signal.SIGUSR1, lambda *_: GLib.idle_add(window.restore_from_background))
    window.show_all()
    if os.environ.get("SOCKET_STREAM_PREVIEW_DEMO") == "1":
        def show_preview():
            window.floating.set_stealth(False)
            window.floating.window.set_keep_above(True)
            window.floating.window.set_skip_taskbar_hint(True)
            window.floating.window.set_skip_pager_hint(True)
            window.floating.window.set_decorated(True)
            window.floating.window.set_accept_focus(True)
            window.floating.set_question("How would you improve Two Sum from O(n^2) to O(n)?")
            window.floating.set_answer(
                "Brute force: try all pairs, O(n^2) time, O(1) space.\n\n"
                "Optimal: use a hash map of numbers already seen. For each "
                "number, look up target minus number. O(n) average time "
                "and O(n) space.\n\n"
                "This is local sample content. Observation is not active."
            )
            window.floating.set_status("Preview: answer panel, no capture active")
            window.floating.show()
            try:
                screen = window.floating.window.get_screen()
                monitor = screen.get_monitor_geometry(screen.get_primary_monitor())
                window.floating.window.move(monitor.x + max(0, monitor.width - 550), monitor.y + 35)
            except Exception:
                pass
            return False
        GLib.idle_add(show_preview)
    Gtk.main()


if __name__ == "__main__":
    main()
