"""Floating answer panel for the Linux live-practice workflow.

The panel renders the question and the model answer produced by
``socket_stream observe-live`` (or ``audio-live``). It applies the best-effort
exclusion plan from :mod:`overlay_guard`. Focus changes and screen-share
detection never hide it; only an explicit user hide/close action does.

The module imports cleanly with no GTK installed so it can be unit tested
headless; GTK is imported lazily inside :class:`FloatingOverlay`.

Standalone use::

    python3 floating_overlay.py --provider ollama
    python3 floating_overlay.py --stdin < events.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from live_state import LiveState  # noqa: E402  (path set above)
from answer_format import answer_parts, markdown_runs, question_title  # noqa: E402
from ui_theme import install_theme, style  # noqa: E402
from capture_rules import PANEL_APP_ID, PANEL_WM_CLASS  # noqa: E402  (path set above)
from overlay_guard import (  # noqa: E402  (path set above)
    SESSION_WAYLAND,
    ScreenShareWatcher,
    backend_is_x11,
    build_plan,
    detect_session,
    display_is_x11,
    ensure_panel_backend,
    manual_drag_position,
    needs_manual_drag,
    should_hide,
)

WINDOW_TITLE = "SocketStream — Practice Answer"
ANSWER_LIMIT = 64_000
DEFAULT_PANEL_OPACITY = 0.90
PANEL_SIZE = (720, 620)
COMPACT_PANEL_SIZE = (560, 420)


def parse_opacity(value):
    """Background opacity; glyphs remain opaque for legible glass panels."""
    try:
        opacity = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("opacity must be a number from 0.55 to 1.0") from exc
    if not 0.55 <= opacity <= 1.0:
        raise argparse.ArgumentTypeError("opacity must be from 0.55 to 1.0")
    return opacity


def repo_root() -> Path:
    return _HERE.parents[2]


def default_cli_path() -> str:
    """Locate the Rust CLI the same way the GTK shell does."""
    override = os.environ.get("SOCKET_STREAM_CLI")
    if override:
        return override
    root = repo_root()
    for candidate in (root / "bin" / "socket_stream", root / "target" / "debug" / "socket_stream"):
        if candidate.is_file():
            return str(candidate)
    return str(root / "target" / "debug" / "socket_stream")


def observe_command(cli, provider, interval=4, allow_ocr=False, portal=False):
    """Build the ``observe-live`` command line consumed by the overlay."""
    command = [
        str(cli), "observe-live", "--authorized",
        "--interval", str(int(interval)), "--provider", str(provider),
    ]
    if allow_ocr:
        command.append("--allow-ocr")
    if portal:
        command.append("--portal")
    return command


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="floating_overlay.py",
        description=(
            "Floating SocketStream answer panel. Applies best-effort capture-exclusion "
            "hints and stays visible across focus changes and screen shares. Linux "
            "cannot guarantee exclusion from a full-screen capture."
        ),
    )
    parser.add_argument("--stdin", action="store_true",
                        help="read JSONL events (schema 1) from stdin")
    parser.add_argument("--provider", default=None,
                        help="provider for observe-live (default: ollama)")
    parser.add_argument("--cli", default=None, help="path to the socket_stream binary")
    parser.add_argument("--interval", type=int, default=4, help="observation interval seconds")
    parser.add_argument("--allow-ocr", action="store_true",
                        help="allow the local OCR fallback")
    parser.add_argument("--portal", action="store_true",
                        help="use the user-approved ScreenCast chooser")
    parser.add_argument("--stealth", dest="stealth", action="store_true", default=True,
                        help="apply best-effort capture-exclusion hints (default)")
    parser.add_argument("--no-stealth", dest="stealth", action="store_false",
                        help="disable best-effort capture-exclusion hints")
    parser.add_argument("--compact", action="store_true", help="start in the compact layout")
    parser.add_argument(
        "--opacity", type=parse_opacity, default=DEFAULT_PANEL_OPACITY,
        help="background opacity from 0.55 (transparent) to 1.0 (opaque; default: 0.90)",
    )
    return parser.parse_args(argv)


class OverlayModel:
    """Pure JSONL-event to view-state mapping. No GTK, fully testable."""

    def __init__(self, state=None):
        self.state = state if state is not None else LiveState()

    def feed(self, event) -> bool:
        return bool(self.state.apply(event))

    def snapshot(self) -> dict:
        return self.state.snapshot()

    def status_line(self, provider="") -> str:
        view = self.snapshot()
        if view["error"]:
            return f"Error: {view['error']}"
        parts = [view["status"].title(), view["source"], provider]
        return " · ".join(part for part in parts if part)


class FloatingOverlay:
    """A GTK3 answer panel that tries to stay out of screen captures."""

    def _create_window(self):
        """Create the panel window before it advertises the panel identity."""
        window = self._Gtk.Window(title=WINDOW_TITLE)
        try:
            window.set_wmclass(PANEL_WM_CLASS, PANEL_WM_CLASS)
        except Exception:  # noqa: BLE001 - X11 only; harmless on Wayland
            pass
        return window

    def _show_window(self):
        """Map the panel while it advertises its own compositor identity.

        GTK3 Wayland sends ``xdg_toplevel.set_app_id()`` from
        ``g_get_prgname()`` when the toplevel is first created. On GTK 3.24
        that happens at map time, not at realize time (verified on 3.24.52),
        so the identity has to be active around the first ``show_all()``.
        """
        try:
            self._with_panel_identity(self.window.show_all)
        except Exception:  # noqa: BLE001 - retry without the identity swap
            self.window.show_all()
        try:
            self._drag_debug(
                f"shown at position={self.window.get_position()} "
                f"size={self.window.get_size()}"
            )
        except Exception:  # noqa: BLE001 - diagnostics only
            pass

    def _with_panel_identity(self, action):
        """Run ``action`` while the process advertises the panel identity."""
        previous_name = previous_class = None
        try:
            previous_name = self._GLib.get_prgname()
        except Exception:  # noqa: BLE001
            pass
        try:
            previous_class = self._Gdk.get_program_class()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._GLib.set_prgname(PANEL_APP_ID)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._Gdk.set_program_class(PANEL_WM_CLASS)
        except Exception:  # noqa: BLE001
            pass
        try:
            return action()
        finally:
            if previous_name:
                try:
                    self._GLib.set_prgname(previous_name)
                except Exception:  # noqa: BLE001
                    pass
            if previous_class:
                try:
                    self._Gdk.set_program_class(previous_class)
                except Exception:  # noqa: BLE001
                    pass

    def __init__(self, plan=None, session=None, *, stealth=True, compact=False,
                 opacity=DEFAULT_PANEL_OPACITY, on_stop=None, on_pause=None,
                 on_copy=None, on_minimize=None, on_settings=None, state=None,
                 on_selection_changed=None, font_size=16, reduced_motion=False):
        import gi

        gi.require_version("Gdk", "3.0")
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gdk, GLib, Gtk, Pango

        self._Gtk = Gtk
        self._Gdk = Gdk
        self._GLib = GLib
        self._Pango = Pango

        self.session = session if session is not None else detect_session()
        try:
            self.display_name = Gdk.Display.get_default().get_name() or ""
        except Exception:  # noqa: BLE001 - a headless test must not fail here
            self.display_name = ""
        self.xwayland_dock = False
        if plan is None:
            plan = build_plan(self.session)
        if (
            self.session.session_type == SESSION_WAYLAND
            and plan.strategy == "wayland-toplevel"
            and display_is_x11(self.display_name)
        ):
            # GNOME Wayland cannot keep a Wayland toplevel above a fullscreen
            # window. On XWayland the dock layer can, and mutter never focuses
            # a dock window.
            plan = build_plan(self.session, xwayland_dock=True)
            self.xwayland_dock = True
        self.plan = plan
        self.stealth = bool(stealth)
        self.compact = bool(compact)
        self.opacity = parse_opacity(opacity)
        self.font_size = max(13, min(22, int(font_size)))
        self.reduced_motion = bool(reduced_motion)
        self._on_stop = on_stop
        self._on_pause = on_pause
        self._on_copy = on_copy
        self._on_minimize = on_minimize
        self._on_settings = on_settings
        self._on_selection_changed = on_selection_changed
        self._model = OverlayModel(state)
        self._watcher = ScreenShareWatcher()
        self._share_active = False
        self._user_hidden = False
        self._ever_shown = False
        self._paused = False
        self._watch_timeout = None
        self._child = None
        self._applied = []
        self._drag_manual = False
        self._drag_origin = (0, 0)
        self._drag_grab = (0, 0)
        self._resizing = False
        self._render_timeout = None
        self._fade_tick = None
        self._raw_answer = ""
        self._rendered_runs = []
        self._display_request = ""
        self._history_signature = None
        self._syncing_pin = False
        self._provider = ""

        self.window = self._with_panel_identity(self._create_window)
        self.window.set_default_size(*(COMPACT_PANEL_SIZE if self.compact else PANEL_SIZE))
        self.window.set_size_request(480, 340)
        self.window.set_name("socket-stream-answer-panel")
        style(self.window, "ss-overlay")
        install_theme(self.window.get_screen())
        self.window.set_decorated(False)
        self.window.set_focus_on_map(False)
        self.window.set_accept_focus(False)
        visual = self.window.get_screen().get_rgba_visual()
        self._has_alpha = visual is not None and self.window.get_screen().is_composited()
        if visual is not None:
            self.window.set_visual(visual)
        self.window.set_app_paintable(True)
        self.window.connect("draw", self._clear_background)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.window.add(outer)
        command_row = Gtk.Box(spacing=8)
        command_row.set_halign(Gtk.Align.CENTER)
        outer.pack_start(command_row, False, False, 0)
        toolbar = style(Gtk.Box(spacing=6), "ss-command-bar")
        command_row.pack_start(toolbar, False, False, 0)
        layout = style(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), "ss-panel")
        outer.pack_start(layout, True, True, 0)
        self.drag_handle = Gtk.EventBox()
        self.drag_handle.set_visible_window(False)
        self.drag_handle.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON1_MOTION_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
        )
        drag_content = Gtk.Box(spacing=10)
        drag_content.pack_start(style(Gtk.Label(label="S"), "ss-brand"), False, False, 2)
        drag_content.pack_start(Gtk.Label(label="SocketStream"), False, False, 0)
        self.spinner = Gtk.Spinner()
        self.spinner.set_no_show_all(True)
        drag_content.pack_start(self.spinner, False, False, 0)
        self.status = style(Gtk.Label(label="Ready"), "ss-muted")
        self.status.set_xalign(0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.set_max_width_chars(16)
        drag_content.pack_start(self.status, True, True, 0)
        self.drag_handle.add(drag_content)
        self.drag_handle.set_tooltip_text("Drag to move the answer panel")
        self.drag_handle.connect("button-press-event", self._on_drag_start)
        self.drag_handle.connect("motion-notify-event", self._on_drag_motion)
        self.drag_handle.connect("button-release-event", self._on_drag_release)
        toolbar.pack_start(self.drag_handle, True, True, 0)
        # Diagnostics: trace every press that reaches the panel at all, so a
        # silent no-drag (stacking, geometry, focus) is visible in the log.
        self.window.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.window.connect("button-press-event", self._on_any_press)

        self.pause_button = Gtk.Button(label="Pause")
        self.pause_button.set_sensitive(False)
        self.pause_button.connect("clicked", lambda *_: self._pause())
        toolbar.pack_start(self.pause_button, False, False, 0)
        self.stop_button = style(Gtk.Button(label="Stop"), "ss-quiet", "ss-danger")
        self.stop_button.connect("clicked", lambda *_: self._stop())
        toolbar.pack_start(self.stop_button, False, False, 0)
        if on_settings is not None:
            toolbar.pack_start(self._icon_button("preferences-system-symbolic", "Panel settings",
                                                lambda *_: on_settings()), False, False, 0)
        self.minimize_button = None
        if on_minimize is not None:
            self.minimize_button = self._icon_button("window-minimize-symbolic", "Minimize app",
                                                     lambda *_: on_minimize())
            toolbar.pack_start(self.minimize_button, False, False, 0)
        self.hide_button = self._icon_button("window-close-symbolic", "Close panel", lambda *_: self.hide())
        style(self.hide_button, "ss-close")
        self.hide_button.set_valign(Gtk.Align.CENTER)
        command_row.pack_start(self.hide_button, False, False, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.set_border_width(18)
        layout.pack_start(body, True, True, 0)
        meta = Gtk.Box(spacing=8)
        self.source_label = style(Gtk.Label(label="Ready"), "ss-chip")
        meta.pack_start(self.source_label, False, False, 0)
        self.pin_button = style(Gtk.ToggleButton(label="Pin question"), "ss-pin")
        self.pin_button.set_tooltip_text("Keep this question and its answer on screen as new questions arrive")
        self.pin_button.connect("toggled", self._on_pin)
        meta.pack_end(self.pin_button, False, False, 0)
        body.pack_start(meta, False, False, 0)
        self.question = style(Gtk.Label(label="Ready for your next question"), "ss-question")
        self.question.set_xalign(1)
        self.question.set_justify(Gtk.Justification.RIGHT)
        self.question.set_line_wrap(True)
        self.question.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.question.set_lines(3)
        self.question.set_ellipsize(Pango.EllipsizeMode.END)
        self.question.set_width_chars(1)
        self.question.set_hexpand(True)
        question_button = style(Gtk.Button(), "ss-question-bubble")
        question_button.set_margin_start(36)
        question_button.add(self.question)
        question_button.set_tooltip_text("Show the full recognized question")
        question_button.connect("clicked", lambda *_: self.question_revealer.set_reveal_child(
            not self.question_revealer.get_reveal_child()))
        body.pack_start(question_button, False, False, 0)
        self.question_revealer = Gtk.Revealer()
        self.question_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        question_scroll = Gtk.ScrolledWindow()
        question_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        question_scroll.set_min_content_height(100)
        self.full_question = style(Gtk.Label(), "ss-muted")
        self.full_question.set_xalign(0)
        self.full_question.set_line_wrap(True)
        self.full_question.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.full_question.set_selectable(True)
        self.full_question.set_max_width_chars(60)
        question_scroll.add(self.full_question)
        self.question_revealer.add(question_scroll)
        body.pack_start(self.question_revealer, False, False, 0)

        answer_meta = Gtk.Box(spacing=8)
        self.answer_label = style(Gtk.Label(label="ANSWER"), "ss-eyebrow")
        answer_meta.pack_start(self.answer_label, False, False, 0)
        self.provider_label = style(Gtk.Label(), "ss-muted")
        self.provider_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.provider_label.set_max_width_chars(32)
        answer_meta.pack_end(self.provider_label, False, False, 0)
        body.pack_start(answer_meta, False, False, 0)
        self.answer_stack = Gtk.Stack()
        self.answer_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.answer_stack.set_vexpand(True)
        self.answer_stack.set_hhomogeneous(False)
        self.answer_stack.set_vhomogeneous(False)
        self.answer_scroll = Gtk.ScrolledWindow()
        self.answer_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.answer_scroll.set_hexpand(True)
        self.answer_scroll.set_vexpand(True)
        self.answer = style(Gtk.TextView(), "ss-answer")
        self.answer.set_editable(False)
        self.answer.set_cursor_visible(False)
        self.answer.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.answer.set_pixels_below_lines(5)
        self.answer.set_left_margin(2)
        self.answer.set_right_margin(8)
        self.answer_scroll.add(self.answer)
        self.answer_stack.add_named(self.answer_scroll, "answer")
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        empty.set_valign(Gtk.Align.CENTER)
        self.empty_title = Gtk.Label(label="A little space to think.")
        self.empty_detail = style(Gtk.Label(label="Start reading your screen or listening to audio.\nYour answer will appear here."), "ss-muted")
        self.empty_detail.set_line_wrap(True)
        self.empty_detail.set_max_width_chars(50)
        self.empty_detail.set_justify(Gtk.Justification.CENTER)
        empty.pack_start(self.empty_title, False, False, 0)
        empty.pack_start(self.empty_detail, False, False, 0)
        self.answer_stack.add_named(empty, "empty")
        self.answer_stack.set_visible_child_name("empty")
        body.pack_start(self.answer_stack, True, True, 0)
        self.notice = style(Gtk.Label(), "ss-muted")
        self.notice.set_xalign(0)
        self.notice.set_line_wrap(True)
        self.notice.set_max_width_chars(65)
        self.notice.set_no_show_all(True)
        body.pack_start(self.notice, False, False, 0)
        self._create_answer_tags()

        self.history_revealer = Gtk.Revealer()
        self.history_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        history_body = style(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8), "ss-history")
        history_body.set_border_width(12)
        self.history_stack = Gtk.Stack()
        self.history_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.history_stack.set_vhomogeneous(True)
        history_switcher = Gtk.StackSwitcher(stack=self.history_stack)
        history_body.pack_start(history_switcher, False, False, 0)
        history_scroll = Gtk.ScrolledWindow()
        history_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        history_scroll.set_min_content_height(130)
        self.history_list = Gtk.ListBox()
        self.history_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.history_list.connect("row-activated", lambda _list, row: self.pin_question(row.request_id))
        history_scroll.add(self.history_list)
        self.history_stack.add_titled(history_scroll, "questions", "Questions")
        transcript_scroll = Gtk.ScrolledWindow()
        transcript_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        transcript_scroll.set_min_content_height(130)
        self.transcript = style(Gtk.TextView(), "ss-transcript")
        self.transcript.set_editable(False)
        self.transcript.set_cursor_visible(False)
        self.transcript.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        transcript_scroll.add(self.transcript)
        self.history_stack.add_titled(transcript_scroll, "transcript", "Heard transcript")
        history_body.pack_start(self.history_stack, True, True, 0)
        history_hint = style(Gtk.Label(label="Select to pin · Recent 24 questions · This session only"), "ss-muted")
        history_hint.set_ellipsize(Pango.EllipsizeMode.END)
        history_body.pack_start(history_hint, False, False, 0)
        self.history_revealer.add(history_body)
        layout.pack_start(self.history_revealer, False, False, 0)

        self.controls = style(Gtk.Box(spacing=6), "ss-footer")
        layout.pack_start(self.controls, False, False, 0)
        self.history_button = style(Gtk.ToggleButton(label="Questions  0"), "ss-quiet")
        self.history_button.connect("toggled", lambda button: self.history_revealer.set_reveal_child(button.get_active()))
        self.controls.pack_start(self.history_button, False, False, 0)
        self.latest_button = style(Gtk.Button(label="Follow latest"), "ss-quiet")
        self.latest_button.set_no_show_all(True)
        self.latest_button.connect("clicked", lambda *_: self.follow_latest())
        self.controls.pack_start(self.latest_button, False, False, 0)
        spacer = Gtk.Box()
        self.controls.pack_start(spacer, True, True, 0)
        self.copy_button = self._icon_button("edit-copy-symbolic", "Copy answer", lambda *_: self.copy_answer())
        self.controls.pack_start(self.copy_button, False, False, 0)
        self.compact_button = style(Gtk.ToggleButton(), "ss-quiet", "ss-icon")
        self.compact_button.add(Gtk.Image.new_from_icon_name("view-restore-symbolic", Gtk.IconSize.MENU))
        self.compact_button.set_tooltip_text("Toggle compact layout")
        self.compact_button.get_accessible().set_name("Compact layout")
        self.compact_button.set_active(self.compact)
        self.compact_button.connect("toggled", self._on_compact)
        self.controls.pack_start(self.compact_button, False, False, 0)
        self.resize_handle = Gtk.EventBox()
        self.resize_handle.set_visible_window(False)
        self.resize_handle.add(style(Gtk.Label(label="◢"), "ss-muted"))
        self.resize_handle.set_tooltip_text("Drag to resize")
        self.resize_handle.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON1_MOTION_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK)
        self.resize_handle.connect("button-press-event", self._on_resize_start)
        self.resize_handle.connect("motion-notify-event", self._on_resize_motion)
        self.resize_handle.connect("button-release-event", self._on_resize_release)
        self.controls.pack_end(self.resize_handle, False, False, 0)

        self._surface_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(self.window.get_screen(), self._surface_css,
                                                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.set_appearance(self.opacity, self.font_size, self.reduced_motion)
        self.render_state()

        self.window.connect("delete-event", self._on_delete)

        # GTK3 on Wayland derives ``xdg_toplevel.app_id`` from
        # ``g_get_prgname()``. Keep the panel identity active for realize and
        # for the first map (``_show_window``); the main window keeps its own.
        try:
            self._with_panel_identity(self.window.realize)
        except Exception:  # noqa: BLE001 - show_all() will realize it later
            pass

        if self.stealth:
            self._apply_plan()

    def _icon_button(self, icon, title, callback):
        button = style(self._Gtk.Button(), "ss-quiet", "ss-icon")
        button.add(self._Gtk.Image.new_from_icon_name(icon, self._Gtk.IconSize.MENU))
        button.set_tooltip_text(title)
        button.get_accessible().set_name(title)
        button.connect("clicked", callback)
        return button

    def _clear_background(self, _window, cr):
        import cairo

        cr.save()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.restore()
        return False

    def set_appearance(self, opacity, font_size=16, reduced_motion=False):
        self.opacity = parse_opacity(opacity)
        self.font_size = max(13, min(22, int(font_size)))
        self.reduced_motion = bool(reduced_motion)
        alpha = self.opacity if self._has_alpha else 1.0
        self._surface_css.load_from_data((
            f"#socket-stream-answer-panel .ss-panel, #socket-stream-answer-panel .ss-command-bar {{ background-color: rgba(23,23,28,{alpha}); }}\n"
            f"#socket-stream-answer-panel .ss-answer {{ font-size: {self.font_size}px; }}\n"
        ).encode())
        settings = self._Gtk.Settings.get_default()
        animations = settings is None or settings.get_property("gtk-enable-animations")
        duration = 160 if animations and not self.reduced_motion else 0
        context = self.window.get_style_context()
        if duration:
            context.remove_class("ss-reduced-motion")
        else:
            context.add_class("ss-reduced-motion")
            self._cancel_fade()
            self.spinner.stop()
        for widget in (self.question_revealer, self.history_revealer, self.answer_stack, self.history_stack):
            widget.set_transition_duration(duration)
        self._motion_enabled = duration > 0
        self._update_code_size()

    def _create_answer_tags(self):
        buffer = self.answer.get_buffer()
        buffer.create_tag("body")
        buffer.create_tag("strong", weight=self._Pango.Weight.BOLD, foreground="#f5f7fa")
        buffer.create_tag("emphasis", style=self._Pango.Style.ITALIC)
        buffer.create_tag("heading", weight=self._Pango.Weight.BOLD, scale=1.08,
                          pixels_above_lines=10, pixels_below_lines=7, foreground="#cbe3ff")
        buffer.create_tag("code", family="monospace", foreground="#d0e6ff", background="#263348")
        buffer.create_tag("code_block", family="monospace", foreground="#dbe8fa",
                          paragraph_background="#171d25", left_margin=12, right_margin=12,
                          pixels_below_lines=1, wrap_mode=self._Gtk.WrapMode.NONE)
        buffer.create_tag("code_language", family="monospace", foreground="#95adbe",
                          paragraph_background="#171d25", left_margin=12, pixels_above_lines=8,
                          pixels_below_lines=8)
        buffer.create_tag("muted", foreground="#788798")

    def _update_code_size(self):
        table = self.answer.get_buffer().get_tag_table()
        for name in ("code", "code_block", "code_language"):
            table.lookup(name).set_property("scale", (self.font_size - 1) / self.font_size)

    def _on_resize_start(self, _widget, event):
        if getattr(event, "button", None) != 1:
            return False
        if display_is_x11(self.display_name):
            self._resizing = True
            self._resize_size = tuple(self.window.get_size())
            self._resize_pointer = (event.x_root, event.y_root)
            self.resize_handle.grab_add()
        else:
            self.window.begin_resize_drag(self._Gdk.WindowEdge.SOUTH_EAST, 1,
                                          int(event.x_root), int(event.y_root), event.time)
        return True

    def _on_resize_motion(self, _widget, event):
        if not self._resizing:
            return False
        width = max(480, int(self._resize_size[0] + event.x_root - self._resize_pointer[0]))
        height = max(340, int(self._resize_size[1] + event.y_root - self._resize_pointer[1]))
        self.window.resize(width, height)
        return True

    def _on_resize_release(self, _widget, _event):
        if not getattr(self, "_resizing", False):
            return False
        self._resizing = False
        self.resize_handle.grab_remove()
        return True

    def _window_is_unmanaged(self):
        """True when the window manager will not move this window for us.

        Override-redirect windows are unmanaged by definition, and the dock
        (plus popup-menu) type hints ask the WM to treat the window as a
        panel — mutter refuses ``begin_move_drag`` for both.
        """
        is_override = False
        try:
            gdk_window = self._gdk_window()
            if gdk_window is not None:
                is_override = bool(gdk_window.get_override_redirect())
        except Exception:  # noqa: BLE001 - backend without the attribute
            is_override = False
        hint_is_dock = False
        try:
            hint = self.window.get_type_hint()
            dock = getattr(self._Gdk.WindowTypeHint, "DOCK", None)
            popup = getattr(self._Gdk.WindowTypeHint, "POPUP_MENU", None)
            hint_is_dock = dock is not None and hint in (dock, popup)
        except Exception:  # noqa: BLE001
            hint_is_dock = False
        self._drag_debug(
            f"unmanaged check: override={is_override} hint_dock={hint_is_dock}"
        )
        return needs_manual_drag(is_override, hint_is_dock)

    def _drag_debug(self, message):
        """Stderr trace for drag decisions; enabled with SOCKET_STREAM_DEBUG=1."""
        if os.environ.get("SOCKET_STREAM_DEBUG"):
            print(f"[drag] {message}", file=sys.stderr, flush=True)

    def _grab_pointer(self, event):
        """Grab the pointer globally so motion outside the panel still tracks.

        The implicit button grab usually covers this, but an explicit seat
        grab with ``owner_events=True`` keeps motion events flowing to the
        drag handle when the pointer leaves the (small) panel — important
        for override-redirect windows that some compositors treat oddly.
        """
        try:
            display = self._Gdk.Display.get_default()
            seat = display.get_default_seat()
            if seat is None:
                return False
            status = seat.grab(
                self.drag_handle.get_window(),
                self._Gdk.SeatCapabilities.ALL_POINTING,
                True,  # owner_events: keep receiving events anywhere
                None,  # default cursor
                event,
                None,
                None,
            )
            ok = status == getattr(self._Gdk.GrabStatus, "SUCCESS", status)
            self._drag_debug(f"seat grab status={status}")
            return ok
        except Exception as exc:  # noqa: BLE001 - grabbing is best effort
            self._drag_debug(f"seat grab failed: {exc}")
            return False

    def _ungrab_pointer(self):
        try:
            display = self._Gdk.Display.get_default()
            seat = display.get_default_seat()
            if seat is not None:
                seat.ungrab()
        except Exception:  # noqa: BLE001
            pass

    def _on_any_press(self, _widget, event):
        """Log every press that reaches the panel (diagnostics only)."""
        try:
            alloc = self.drag_handle.get_allocation()
            self._drag_debug(
                f"press on panel at ({int(event.x_root)},{int(event.y_root)}) "
                f"handle alloc={alloc.x},{alloc.y} {alloc.width}x{alloc.height}"
            )
        except Exception:  # noqa: BLE001 - diagnostics only
            self._drag_debug("press on panel (allocation unavailable)")
        return False

    def _on_drag_start(self, _widget, event):
        if getattr(event, "button", None) != 1:
            return False
        if self._window_is_unmanaged() and display_is_x11(self.display_name):
            # Unmanaged windows get no window-manager move support, so the
            # panel moves itself while the button is held: keep the
            # pointer-to-origin offset from the press constant. Wayland
            # clients cannot self-move; there the WM path stays the honest
            # fallback (managed windows keep using it everywhere).
            try:
                self._drag_origin = tuple(int(v) for v in self.window.get_position())
                self._drag_grab = (int(event.x_root), int(event.y_root))
                self._drag_manual = True
                grabbed = self._grab_pointer(event)
                self.drag_handle.grab_add()
                self._drag_debug(
                    f"manual drag start origin={self._drag_origin} "
                    f"grab={self._drag_grab} seat_grab={grabbed}"
                )
                return True
            except Exception as exc:  # noqa: BLE001 - fall through to the WM path
                self._drag_debug(f"manual drag setup failed: {exc}")
                self._drag_manual = False
        try:
            self._drag_debug("WM drag (begin_move_drag)")
            self.window.begin_move_drag(1, int(event.x_root), int(event.y_root), event.time)
            return True
        except Exception:
            return False

    def _on_drag_motion(self, _widget, event):
        if not self._drag_manual:
            return False
        try:
            new_x, new_y = manual_drag_position(
                self._drag_origin[0],
                self._drag_origin[1],
                self._drag_grab[0],
                self._drag_grab[1],
                int(event.x_root),
                int(event.y_root),
            )
            self.window.move(new_x, new_y)
        except Exception:  # noqa: BLE001 - keep the drag alive on odd events
            pass
        return True

    def _on_drag_release(self, _widget, _event):
        if not getattr(self, "_drag_manual", False):
            return False
        self._drag_manual = False
        self.drag_handle.grab_remove()
        self._ungrab_pointer()
        self._drag_debug("manual drag end")
        return True

    # ---- exclusion plan -------------------------------------------------

    def _apply_plan(self):
        plan = self.plan
        applied = []
        if plan.keep_above:
            try:
                self.window.set_keep_above(True)
                applied.append("keep-above")
            except Exception:  # noqa: BLE001 - best effort
                pass
        if plan.skip_taskbar:
            for setter in ("set_skip_taskbar_hint", "set_skip_pager_hint"):
                try:
                    getattr(self.window, setter)(True)
                except Exception:  # noqa: BLE001
                    pass
            applied.append("skip-taskbar + skip-pager")
        if plan.window_type:
            hint_name = plan.window_type.rsplit("_", 1)[-1]
            hint = getattr(self._Gdk.WindowTypeHint, hint_name, None)
            if hint is not None:
                try:
                    self.window.set_type_hint(hint)
                    applied.append(f"type-hint={hint_name.lower()}")
                    if hint_name == "DOCK":
                        # The dock layer is above fullscreen windows and is
                        # never focused; no titlebar should be drawn for it.
                        self.window.set_decorated(False)
                        self._center_window()
                except Exception:  # noqa: BLE001
                    pass
        if plan.override_redirect:
            if self._try_override_redirect():
                applied.append("override-redirect (unmanaged)")
            else:
                applied.append("override-redirect unavailable")
        self._applied = applied
        self._drag_debug(f"apply_plan: {applied}")
        return applied

    def _clear_plan(self):
        """Best-effort removal of hints when the user disables stealth."""
        for setter, value in (
            ("set_keep_above", False),
            ("set_skip_taskbar_hint", False),
            ("set_skip_pager_hint", False),
        ):
            try:
                getattr(self.window, setter)(value)
            except Exception:  # noqa: BLE001 - window managers vary
                pass
        normal_hint = getattr(self._Gdk.WindowTypeHint, "NORMAL", None)
        if normal_hint is not None:
            try:
                self.window.set_type_hint(normal_hint)
            except Exception:  # noqa: BLE001
                pass
        for target in (self._gdk_window(), self.window):
            setter = getattr(target, "set_override_redirect", None) if target is not None else None
            if callable(setter):
                try:
                    setter(False)
                except Exception:  # noqa: BLE001
                    pass
        try:
            self.window.set_decorated(True)
        except Exception:  # noqa: BLE001
            pass
        self._applied = []

    def _gdk_window(self):
        try:
            return self.window.get_window()
        except Exception:  # noqa: BLE001
            return None

    def _center_window(self):
        """Place the panel at the centre of the primary monitor."""
        try:
            screen = self.window.get_screen()
            primary = screen.get_primary_monitor()
            monitor = screen.get_monitor_geometry(primary)
            width, height = self.window.get_size()
            if width <= 1 or height <= 1:
                width, height = self.window.get_default_size()
            x = monitor.x + max(0, (monitor.width - width) // 2)
            y = monitor.y + max(0, (monitor.height - height) // 2)
            self.window.move(x, y)
            self._drag_debug(
                f"center: primary={primary} monitor={monitor.x},{monitor.y} "
                f"{monitor.width}x{monitor.height} size={width}x{height} "
                f"-> move({x}, {y})"
            )
        except Exception as exc:  # noqa: BLE001 - placement is best effort
            self._drag_debug(f"center failed: {exc}")

    def _try_override_redirect(self):
        """Make the X11 toplevel unmanaged (override-redirect).

        GTK3 does not expose this on ``Gtk.Window``, but the introspectable
        ``Gdk.Window.set_override_redirect`` works before the first map.
        Unmanaged windows have no taskbar/pager entry and stay above managed
        windows regardless of the window manager. On an already-mapped window
        the X11 attribute cannot change, so this honestly reports failure.
        """
        try:
            if self.window.get_mapped():
                return False
        except Exception:  # noqa: BLE001
            pass
        for target in (self._gdk_window(), self.window):
            setter = getattr(target, "set_override_redirect", None) if target is not None else None
            if not callable(setter):
                continue
            try:
                setter(True)
            except Exception:  # noqa: BLE001 - try the next mechanism
                continue
            try:
                self.window.set_decorated(False)
            except Exception:  # noqa: BLE001
                pass
            self._center_window()
            return True
        # GTK3 has no ``POPUP`` hint; ``POPUP_MENU`` is the closest popup
        # type. It does not set override-redirect, but is usually off-taskbar.
        try:
            hint = getattr(self._Gdk.WindowTypeHint, "POPUP_MENU", None)
            if hint is None:
                return False
            self.window.set_type_hint(hint)
            self.window.set_decorated(False)
            return True
        except Exception:  # noqa: BLE001
            return False

    def applied_hints(self):
        return list(self._applied)

    # ---- visibility -----------------------------------------------------

    def is_visible(self):
        try:
            return bool(self.window.get_visible())
        except Exception:  # noqa: BLE001
            return False

    def _should_hide(self):
        # Visibility is user-controlled. Never auto-hide on focus or share state.
        return should_hide(self.plan, self._share_active, self._user_hidden)

    def _sync_visibility(self):
        if self._should_hide():
            if self.is_visible():
                self.window.hide()
        elif self._ever_shown and not self.is_visible():
            self._show_window()

    def show(self):
        was_visible = self.is_visible()
        self._user_hidden = False
        self._ever_shown = True
        if not self._should_hide():
            self._show_window()
            if not was_visible and getattr(self, "_motion_enabled", False):
                self._fade_in()
        return self.is_visible()

    def hide(self):
        self._user_hidden = True
        self._on_drag_release(None, None)
        self._on_resize_release(None, None)
        self._cancel_fade()
        self.window.hide()
        return False

    def _cancel_fade(self):
        if getattr(self, "_fade_tick", None) is not None:
            self.window.remove_tick_callback(self._fade_tick)
            self._fade_tick = None
            self.window.set_opacity(1.0)

    def _fade_in(self):
        self._cancel_fade()
        start = self._GLib.get_monotonic_time()
        self.window.set_opacity(0.45)

        def tick(_widget, _clock):
            progress = min(1.0, (self._GLib.get_monotonic_time() - start) / 140000)
            eased = 1 - (1 - progress) ** 3
            self.window.set_opacity(0.45 + 0.55 * eased)
            if progress >= 1:
                self._fade_tick = None
                return False
            return True

        self._fade_tick = self.window.add_tick_callback(tick)

    def toggle(self):
        # Keep legacy toggle callers from unexpectedly closing the panel. The
        # explicit Close button and window close control call hide() directly.
        return self.show()

    def apply_share_state(self, active):
        """Record the share state without changing panel visibility."""
        self._share_active = bool(active)
        self._sync_visibility()
        return self.is_visible()

    def set_stealth(self, enabled):
        self.stealth = bool(enabled)
        if self.stealth:
            self._apply_plan()
        else:
            self._clear_plan()
        self._sync_visibility()
        return self.stealth

    # ---- content --------------------------------------------------------

    def set_question(self, text):
        text = (text or "").strip()[:12000]
        self.question.set_text(question_title(text) or "Ready for your next question")
        self.full_question.set_text(text)

    def set_answer(self, text):
        text = (text or "")[:ANSWER_LIMIT]
        if self._raw_answer == text:
            return
        self._raw_answer = text
        self._schedule_render()

    def append_answer(self, text):
        if not text:
            return
        self.set_answer((self._raw_answer + text)[:ANSWER_LIMIT])

    def answer_text(self):
        return answer_parts(self._raw_answer)[1]

    def _schedule_render(self):
        # Coalesce fast token streams; only the changed run tail touches GTK.
        if self._render_timeout is None:
            self._render_timeout = self._GLib.timeout_add(80, self._flush_answer)

    def _flush_answer(self):
        self._render_timeout = None
        content = self.answer_text()
        runs = markdown_runs(content)
        adjustment = self.answer_scroll.get_vadjustment()
        scroll_value = adjustment.get_value()
        at_bottom = adjustment.get_value() + adjustment.get_page_size() >= adjustment.get_upper() - 36
        previous = self._rendered_runs
        common = 0
        while common < min(len(previous), len(runs)) and previous[common] == runs[common]:
            common += 1
        if common < len(previous) or common < len(runs):
            buffer = self.answer.get_buffer()
            offset = sum(len(text) for text, _tag in previous[:common])
            buffer.delete(buffer.get_iter_at_offset(offset), buffer.get_end_iter())
            for text, tag in runs[common:]:
                buffer.insert_with_tags_by_name(buffer.get_end_iter(), text, tag)
            self._rendered_runs = runs
        self.answer_stack.set_visible_child_name("answer" if content.strip() else "empty")
        self.copy_button.set_sensitive(bool(content.strip()))
        key = self._display_request
        top = getattr(self, "_scroll_top", False)
        self._scroll_top = False

        def restore_scroll():
            if not getattr(self, "_destroyed", False) and self._display_request == key:
                if top:
                    adjustment.set_value(0)
                elif at_bottom:
                    adjustment.set_value(max(0, adjustment.get_upper() - adjustment.get_page_size()))
                else:
                    adjustment.set_value(min(scroll_value, max(0, adjustment.get_upper() - adjustment.get_page_size())))
            return False

        self._GLib.idle_add(restore_scroll)
        return False

    def bind_state(self, state):
        self._model.state = state
        self.render_state()

    def render_state(self, provider=None):
        """Render a selected pair, keeping session activity and transcript separate."""
        if provider is not None:
            self._provider = provider
        state = self._model.state.display_snapshot()
        key = (state["session_id"], state["request_id"])
        if key != self._display_request:
            self._display_request = key
            self._scroll_top = True
            self.question_revealer.set_reveal_child(False)
            self._raw_answer = ""
            self._rendered_runs = []
            self.answer.get_buffer().set_text("")
            self.answer_stack.set_visible_child_name("empty")
        question = state["question_display"] or state["question"]
        self.set_question(question)
        self.question.set_text(question_title(question, state["answer"]) or "Ready for your next question")
        self.set_answer(state["answer"])
        sources = {"screen": "Screen", "accessibility": "Accessibility", "ocr": "Local OCR",
                   "microphone": "Microphone", "playback": "System audio", "file": "Imported"}
        self.source_label.set_text(sources.get(state["source"], "Ready"))
        self.provider_label.set_text(self._provider)
        self.provider_label.set_tooltip_text(self._provider)
        session_status = state["session_status"]
        is_audio = self._model.snapshot()["source"] in ("microphone", "playback")
        status = {"idle": "Ready", "observing": "Listening" if is_audio else "Reading screen",
                  "thinking": "Answering", "ready": "Listening" if is_audio else "Ready",
                  "paused": "Paused", "error": "Needs attention", "stopped": "Stopped"}.get(session_status, "Ready")
        self.set_status(status)
        busy = session_status == "thinking"
        self.spinner.set_visible(busy)
        if busy and self._motion_enabled:
            self.spinner.start()
        else:
            self.spinner.stop()
        self.answer_label.set_text("PINNED ANSWER" if state["pinned"] else "ANSWER")
        self._syncing_pin = True
        self.pin_button.set_active(state["pinned"])
        self.pin_button.set_label("Pinned" if state["pinned"] else "Pin question")
        self.pin_button.set_sensitive(bool(state["request_id"]))
        self._syncing_pin = False
        self.latest_button.set_visible(state["pinned"])
        count = state["newer_count"]
        self.latest_button.set_label(f"{count} new · Latest" if count else "Follow latest")
        notice = state["error"]
        if not notice and state["live_error"]:
            notice = "Live session: " + state["live_error"]
        if not notice and state["status"] == "interrupted":
            notice = "This answer was interrupted. Newer answers are in Questions."
        self.notice.set_text(notice)
        self.notice.set_visible(bool(notice))
        if state["error"]:
            self.empty_title.set_text("The answer couldn't be completed.")
            self.empty_detail.set_text(state["error"])
        elif state["request_id"]:
            title = "Thinking through your question…" if state["status"] == "thinking" else "No answer text yet."
            self.empty_title.set_text(title)
            self.empty_detail.set_text("You can pin this question while its answer arrives." if busy
                                       else "Start or resume a session to continue.")
        else:
            self.empty_title.set_text("A little space to think.")
            self.empty_detail.set_text("Start reading your screen or listening to audio.\nYour answer will appear here.")
        transcript = self._model.snapshot()["transcript"]
        if transcript != getattr(self, "_transcript_text", None):
            self._transcript_text = transcript
            self.transcript.get_buffer().set_text(transcript or "Recognized speech will appear here when audio listening starts.")
        self._render_history(state)
        self.copy_button.set_sensitive(bool(self.answer_text().strip()))

    def _render_history(self, view):
        history = self._model.state.history()
        signature = (view["request_id"], view["pinned"], tuple(
            (item["request_id"], item["question_display"], item["status"]) for item in history))
        self.history_button.set_label(f"Questions  {len(history)}")
        if signature == self._history_signature:
            return
        self._history_signature = signature
        for row in self.history_list.get_children():
            self.history_list.remove(row)
            row.destroy()
        for item in reversed(history):
            row = self._Gtk.ListBoxRow()
            row.request_id = item["request_id"]
            content = self._Gtk.Box(spacing=10)
            title = self._Gtk.Label(label=question_title(item["question_display"], limit=120))
            title.set_xalign(0)
            title.set_ellipsize(self._Pango.EllipsizeMode.END)
            title.set_max_width_chars(62)
            content.pack_start(title, True, True, 0)
            label = "Pinned" if view["pinned"] and row.request_id == view["request_id"] else {
                "thinking": "Answering", "ready": "Answered", "interrupted": "Partial",
                "paused": "Paused", "error": "Error"}.get(item["status"], "")
            content.pack_end(style(self._Gtk.Label(label=label), "ss-muted"), False, False, 0)
            row.add(content)
            self.history_list.add(row)
            if row.request_id == view["request_id"]:
                self.history_list.select_row(row)
        self.history_list.show_all()

    def _selection_changed(self):
        self.render_state()
        if self._on_selection_changed is not None:
            self._on_selection_changed()

    def pin_question(self, request_id=None):
        if self._model.state.pin(request_id):
            self._selection_changed()

    def toggle_pin(self):
        if self._model.state.toggle_pin():
            self._selection_changed()

    def follow_latest(self):
        if self._model.state.follow_latest():
            self._selection_changed()

    def step_question(self, direction):
        if self._model.state.step_question(direction):
            self._selection_changed()

    def _on_pin(self, _button):
        if not self._syncing_pin:
            self.toggle_pin()

    def set_status(self, text):
        self.status.set_text(text or "")
        self.status.set_tooltip_text(text or "")

    def copy_answer(self):
        if self._on_copy is not None:
            self._on_copy(self.answer_text())
            return
        try:
            clipboard = self._Gtk.Clipboard.get(self._Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text(self.answer_text(), -1)
            self.set_status("Answer copied to clipboard")
        except Exception:  # noqa: BLE001
            self.set_status("Could not access the clipboard")

    # ---- events ---------------------------------------------------------

    def feed_event(self, event):
        if not self._model.feed(event):
            return False
        self.render_state()
        state = self._model.snapshot()
        self.pause_button.set_label("Resume" if state["status"] == "paused" else "Pause")
        self.pause_button.set_sensitive(self._child is not None and state["status"] not in ("idle", "stopped"))
        return True

    # ---- controls -------------------------------------------------------

    def _on_compact(self, button):
        self.compact = button.get_active()
        if self.compact:
            self.history_button.set_active(False)
            self.question_revealer.set_reveal_child(False)
        self.window.resize(*(COMPACT_PANEL_SIZE if self.compact else PANEL_SIZE))

    def _on_delete(self, *_args):
        self.hide()
        return True

    def _pause(self):
        if self._on_pause is not None:
            self._on_pause()
            return
        if self._child is None or self._child.stdin is None:
            return
        self._paused = not self._paused
        try:
            self._child.stdin.write("pause\n" if self._paused else "resume\n")
            self._child.stdin.flush()
            self.pause_button.set_label("Resume" if self._paused else "Pause")
        except OSError:
            self.set_status("Could not send pause/resume to the observation process")

    def _stop(self):
        if self._on_stop is not None:
            self._on_stop()
            return
        self._model.state.stop()
        self._terminate_child()
        self.render_state()
        self.pause_button.set_sensitive(False)

    def _terminate_child(self):
        child = self._child
        if child is None or child.poll() is not None:
            return
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                child.terminate()
            except OSError:
                pass

    # ---- lifecycle ------------------------------------------------------

    def _start_watch_timer(self):
        if self._watch_timeout is None:
            self._watch_timeout = self._GLib.timeout_add_seconds(2, self._tick)

    def _tick(self):
        self.apply_share_state(self._watcher.poll())
        return True

    def attach_child(self, child):
        """Track the observe-live process so Stop can terminate it."""
        self._child = child

    def run(self):
        self._start_watch_timer()
        self._Gtk.main()
        return 0

    def destroy(self):
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        self._model.state.stop()
        self._terminate_child()
        self._on_drag_release(None, None)
        self._on_resize_release(None, None)
        self._cancel_fade()
        if self._render_timeout is not None:
            self._GLib.source_remove(self._render_timeout)
            self._render_timeout = None
        if self._watch_timeout is not None:
            try:
                self._GLib.source_remove(self._watch_timeout)
            except Exception:  # noqa: BLE001
                pass
            self._watch_timeout = None
        self._Gtk.StyleContext.remove_provider_for_screen(self.window.get_screen(), self._surface_css)
        try:
            self.window.destroy()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._Gtk.main_level():
                self._Gtk.main_quit()
        except Exception:  # noqa: BLE001
            pass


def _stream_lines(stream, deliver):
    try:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            deliver(event)
    except Exception:  # noqa: BLE001 - reader thread must not crash the UI
        pass


def main(argv=None):
    args = parse_args(argv)
    session = detect_session()
    backend = ensure_panel_backend(session)
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib
    except (ImportError, ValueError) as exc:
        print(f"GTK3 unavailable: {exc}. Install python3-gi and gir1.2-gtk-3.0.", file=sys.stderr)
        return 2

    plan = build_plan(session, xwayland_dock=backend_is_x11(backend))
    overlay = FloatingOverlay(
        plan=plan, session=session, stealth=args.stealth, compact=args.compact,
        opacity=args.opacity,
    )

    def deliver(event):
        overlay.feed_event(event)
        return False

    def idle_deliver(event):
        GLib.idle_add(deliver, event)
        return False

    if args.stdin:
        threading.Thread(
            target=_stream_lines, args=(sys.stdin, idle_deliver), daemon=True,
        ).start()
    else:
        provider = args.provider or "ollama"
        cli = args.cli or default_cli_path()
        if not Path(cli).is_file():
            print(
                f"socket_stream not found at {cli}. Build it with "
                "`cargo build -p socket_stream` or pass --cli PATH.",
                file=sys.stderr,
            )
            return 2
        command = observe_command(cli, provider, args.interval, args.allow_ocr, args.portal)
        try:
            child = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=None, text=True, bufsize=1, errors="replace",
                start_new_session=True,
            )
        except OSError as exc:
            print(f"Could not start observation: {exc}", file=sys.stderr)
            return 2
        overlay.attach_child(child)
        overlay.set_status(f"Observing · {provider}")
        threading.Thread(
            target=_stream_lines, args=(child.stdout, idle_deliver), daemon=True,
        ).start()

    overlay.show()
    return overlay.run()


if __name__ == "__main__":
    raise SystemExit(main())
