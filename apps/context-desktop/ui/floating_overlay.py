"""Floating, capture-aware answer panel for the Linux live-practice workflow.

The panel renders the question and the model answer produced by
``context-cli observe-live`` (or ``audio-live``). It applies the best-effort
exclusion plan from :mod:`overlay_guard` and hides itself while a screen share
is detected.

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
from overlay_guard import (  # noqa: E402  (path set above)
    ScreenShareWatcher,
    build_plan,
    detect_session,
    should_hide,
)

WINDOW_TITLE = "ContextLens — Practice Answer"
ANSWER_LIMIT = 64_000


def repo_root() -> Path:
    return _HERE.parents[2]


def default_cli_path() -> str:
    """Locate the Rust CLI the same way the GTK shell does."""
    override = os.environ.get("CONTEXT_LENS_CLI")
    if override:
        return override
    root = repo_root()
    for candidate in (root / "bin" / "context-cli", root / "target" / "debug" / "context-cli"):
        if candidate.is_file():
            return str(candidate)
    return str(root / "target" / "debug" / "context-cli")


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
            "Floating ContextLens answer panel. Applies best-effort capture-exclusion "
            "hints and hides itself while a screen share is detected. Linux cannot "
            "guarantee exclusion from a full-screen capture."
        ),
    )
    parser.add_argument("--stdin", action="store_true",
                        help="read JSONL events (schema 1) from stdin")
    parser.add_argument("--provider", default=None,
                        help="provider for observe-live (default: ollama)")
    parser.add_argument("--cli", default=None, help="path to the context-cli binary")
    parser.add_argument("--interval", type=int, default=4, help="observation interval seconds")
    parser.add_argument("--allow-ocr", action="store_true",
                        help="allow the local OCR fallback")
    parser.add_argument("--portal", action="store_true",
                        help="use the user-approved ScreenCast chooser")
    parser.add_argument("--stealth", dest="stealth", action="store_true", default=True,
                        help="apply exclusion hints and the behavioural guard (default)")
    parser.add_argument("--no-stealth", dest="stealth", action="store_false",
                        help="disable exclusion hints and the behavioural guard")
    parser.add_argument("--compact", action="store_true", help="start in the compact layout")
    return parser.parse_args(argv)


class OverlayModel:
    """Pure JSONL-event to view-state mapping. No GTK, fully testable."""

    def __init__(self):
        self.state = LiveState()

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

    def __init__(self, plan=None, session=None, *, stealth=True, compact=False,
                 on_stop=None, on_pause=None, on_copy=None):
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gdk, GLib, Gtk

        self._Gtk = Gtk
        self._Gdk = Gdk
        self._GLib = GLib

        self.session = session if session is not None else detect_session()
        self.plan = plan if plan is not None else build_plan(self.session)
        self.stealth = bool(stealth)
        self.compact = bool(compact)
        self._on_stop = on_stop
        self._on_pause = on_pause
        self._on_copy = on_copy
        self._model = OverlayModel()
        self._watcher = ScreenShareWatcher()
        self._share_active = False
        self._user_hidden = False
        self._ever_shown = False
        self._paused = False
        self._watch_timeout = None
        self._child = None
        self._applied = []

        self.window = Gtk.Window(title=WINDOW_TITLE)
        self.window.set_default_size(*(360, 250) if self.compact else (520, 370))
        self.window.set_border_width(12)
        self.window.set_focus_on_map(False)
        self.window.set_accept_focus(False)
        self._apply_plan()

        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.window.add(layout)

        self.status = Gtk.Label(label="Waiting for a practice question")
        self.status.set_xalign(0)
        layout.pack_start(self.status, False, False, 0)

        self.question = Gtk.Label(label="No question detected yet")
        self.question.set_xalign(0)
        self.question.set_line_wrap(True)
        self.question.set_max_width_chars(70)
        layout.pack_start(self.question, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        self.answer = Gtk.TextView()
        self.answer.set_editable(False)
        self.answer.set_cursor_visible(False)
        self.answer.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroll.add(self.answer)
        layout.pack_start(scroll, True, True, 0)

        self.controls = Gtk.Box(spacing=8)
        layout.pack_start(self.controls, False, False, 0)

        self.compact_button = Gtk.ToggleButton(label="Compact")
        self.compact_button.set_active(self.compact)
        self.compact_button.connect("toggled", self._on_compact)
        self.controls.pack_start(self.compact_button, False, False, 0)

        self.pause_button = Gtk.Button(label="Pause")
        self.pause_button.set_sensitive(False)
        self.pause_button.connect("clicked", lambda *_: self._pause())
        self.controls.pack_start(self.pause_button, False, False, 0)

        self.copy_button = Gtk.Button(label="Copy answer")
        self.copy_button.connect("clicked", lambda *_: self.copy_answer())
        self.controls.pack_start(self.copy_button, False, False, 0)

        self.hide_button = Gtk.Button(label="Hide")
        self.hide_button.connect("clicked", lambda *_: self.hide())
        self.controls.pack_start(self.hide_button, False, False, 0)

        self.stop_button = Gtk.Button(label="Stop practice")
        self.stop_button.connect("clicked", lambda *_: self._stop())
        self.controls.pack_end(self.stop_button, False, False, 0)

        self.window.connect("delete-event", self._on_delete)

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
                except Exception:  # noqa: BLE001
                    pass
        if plan.override_redirect:
            if self._try_override_redirect():
                applied.append("override-redirect (unmanaged)")
            else:
                applied.append("override-redirect unavailable")
        self._applied = applied
        return applied

    def _try_override_redirect(self):
        setter = getattr(self.window, "set_override_redirect", None)
        if callable(setter):
            try:
                setter(True)
                try:
                    self.window.set_decorated(False)
                except Exception:  # noqa: BLE001
                    pass
                return True
            except Exception:  # noqa: BLE001
                return False
        # GTK3 often lacks set_override_redirect; POPUP is unmanaged too.
        try:
            self.window.set_type_hint(self._Gdk.WindowTypeHint.POPUP)
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
        if self._user_hidden:
            return True
        if not self.stealth:
            return False
        return should_hide(self.plan, self._share_active, False)

    def _sync_visibility(self):
        if self._should_hide():
            if self.is_visible():
                self.window.hide()
        elif self._ever_shown and not self.is_visible():
            self.window.show_all()

    def show(self):
        self._user_hidden = False
        self._ever_shown = True
        if not self._should_hide():
            self.window.show_all()
        return self.is_visible()

    def hide(self):
        self._user_hidden = True
        self.window.hide()
        return False

    def toggle(self):
        return self.hide() if self.is_visible() else self.show()

    def apply_share_state(self, active):
        self._share_active = bool(active)
        self._sync_visibility()
        return self.is_visible()

    def set_stealth(self, enabled):
        self.stealth = bool(enabled)
        self._sync_visibility()
        return self.stealth

    # ---- content --------------------------------------------------------

    def set_question(self, text):
        self.question.set_text((text or "").strip()[:650] or "No question detected yet")

    def set_answer(self, text):
        self.answer.get_buffer().set_text(text or "")

    def append_answer(self, text):
        if not text:
            return
        buffer = self.answer.get_buffer()
        if buffer.get_char_count() + len(text) > ANSWER_LIMIT:
            return
        buffer.insert(buffer.get_end_iter(), text)

    def answer_text(self):
        buffer = self.answer.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def set_status(self, text):
        self.status.set_text(text or "")

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
        previous = self._model.snapshot()
        if not self._model.feed(event):
            return False
        state = self._model.snapshot()
        if state["question"] != previous["question"]:
            self.set_question(state["question"] or "Waiting for a question...")
        if event.get("type") == "answer_delta" and state["answer"].startswith(previous["answer"]):
            addition = state["answer"][len(previous["answer"]):]
            if addition:
                self.append_answer(addition)
        elif state["answer"] != previous["answer"]:
            self.set_answer(state["answer"])
        self.set_status(self._model.status_line())
        return True

    # ---- controls -------------------------------------------------------

    def _on_compact(self, button):
        self.compact = button.get_active()
        self.window.resize(*(360, 250) if self.compact else (520, 370))

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
        self._terminate_child()
        self.destroy()

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
        if self._watch_timeout is not None:
            try:
                self._GLib.source_remove(self._watch_timeout)
            except Exception:  # noqa: BLE001
                pass
            self._watch_timeout = None
        try:
            self.window.destroy()
        except Exception:  # noqa: BLE001
            pass
        try:
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
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib
    except (ImportError, ValueError) as exc:
        print(f"GTK3 unavailable: {exc}. Install python3-gi and gir1.2-gtk-3.0.", file=sys.stderr)
        return 2

    session = detect_session()
    plan = build_plan(session)
    overlay = FloatingOverlay(
        plan=plan, session=session, stealth=args.stealth, compact=args.compact,
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
                f"context-cli not found at {cli}. Build it with "
                "`cargo build -p context-cli` or pass --cli PATH.",
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
