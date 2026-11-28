"""Actual GTK controller methods with deferred, headless chooser callbacks.

No desktop, screen text, process launch, model request or user settings.
The fake dialog models nested GTK callbacks during chooser/consent runs.
"""
import ast
import json
import os
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "apps/socket_stream_desktop/ui/linux-gtk.py"
TREE = ast.parse(SOURCE.read_text())
WINDOW_A = {"id": '[":1.55","/window/a"]', "title": "Public practice A", "application": "Chrome"}
WINDOW_B = {"id": '[":1.55","/window/b"]', "title": "Public practice B", "application": "Chrome"}


class CaptureWindowFlow(unittest.TestCase):
    def setUp(self):
        self.work, self.idle, self.dialogs, self.reads = [], [], [], []
        self.launches, self.confirmations, self.messages = [], [], []
        self.windows = [dict(WINDOW_A), dict(WINDOW_B)]
        self.dialog_action = lambda dialog: self.select(dialog, "0")
        self.consent_action = lambda: True
        fixture = self

        class Box:
            def __init__(self):
                self.children = []

            def pack_start(self, value, *_args):
                self.children.append(value)

        class Combo:
            def __init__(self):
                self.items, self.active, self.changed = [], None, None

            def append(self, identity, title):
                self.items.append((identity, title))

            def set_active(self, index):
                self.active = self.items[index][0] if index >= 0 else None

            def set_active_id(self, identity):
                self.active = identity
                if self.changed:
                    self.changed(self)

            def get_active_id(self):
                return self.active

            def connect(self, signal, callback):
                fixture.assertEqual(signal, "changed")
                self.changed = callback

        class Dialog:
            def __init__(self, **_kwargs):
                self.box, self.sensitive, self.destroyed = Box(), {}, False
                fixture.dialogs.append(self)

            def set_modal(self, _value):
                pass

            def add_buttons(self, *_values):
                pass

            def get_content_area(self):
                return self.box

            def set_response_sensitive(self, response, value):
                self.sensitive[response] = value

            def show_all(self):
                pass

            def run(self):
                return fixture.dialog_action(self)

            def destroy(self):
                self.destroyed = True

            @property
            def combo(self):
                return next(child for child in self.box.children if isinstance(child, Combo))

        self.scope = dict(
            ROOT=ROOT, os=os, json=json,
            Gtk=SimpleNamespace(Dialog=Dialog, ComboBoxText=Combo,
                                Label=lambda **_kwargs: SimpleNamespace(set_line_wrap=lambda *_args: None),
                                ResponseType=SimpleNamespace(OK=1, CANCEL=0)),
            GLib=SimpleNamespace(idle_add=lambda callback: self.idle.append(callback)),
            threading=SimpleNamespace(Thread=lambda target, **_kwargs: SimpleNamespace(start=lambda: self.work.append(target))),
            subprocess=SimpleNamespace(run=self.native_inventory, TimeoutExpired=TimeoutError),
            CLI=SimpleNamespace(is_file=lambda: False),
            live_start_args=lambda *_args: ["observe-live", "--authorized"],
        )
        self.app = SimpleNamespace(
            proc=None, _starting=False, _exiting=False, _stdin_live=False, _live_mode=False,
            _process_generation=7, _capture_request=None, _question_window_request=None,
            question_window_target=None, question_window_title="", session_message=object(),
            question_window_hint=SimpleNamespace(set_text=lambda value: self.messages.append(value)),
            feedback=lambda _widget, text, **_kwargs: self.messages.append(text),
            write=lambda text: self.messages.append(text), set_status=lambda text: self.messages.append(text),
            selected_provider=lambda: "mock", provider_display=lambda: "mock", fallback_profile="",
            audio_check=SimpleNamespace(get_active=lambda: False), screen_check=SimpleNamespace(get_active=lambda: True),
            interval=SimpleNamespace(get_value_as_int=lambda: 5), force_ocr=SimpleNamespace(get_active=lambda: False),
            portal=SimpleNamespace(get_active=lambda: False), audio_source="system", audio_device="default",
            speaker_role="interviewer", show_answer_panel=lambda: None,
        )
        for name in ("manual_capture", "choose_question_window", "stop", "start"):
            setattr(self.app, name, MethodType(self.method(name), self.app))
        self.app.run_cli = lambda args: self.launches.append((list(args), self.app.question_window_target)) or True
        self.app.confirmation = self.confirm

    def method(self, name):
        node = next(value for value in ast.walk(TREE) if isinstance(value, ast.FunctionDef) and value.name == name)
        scope = dict(self.scope)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), scope)
        return scope[name]

    def native_inventory(self, args, **kwargs):
        self.assertEqual(args, ["/usr/bin/python3", str(ROOT / "scripts/linux-accessibility.py"), "--mode", "windows"])
        self.assertEqual(kwargs["timeout"], 6)
        self.reads.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.windows))

    def confirm(self, title, explanation):
        self.confirmations.append((title, explanation, self.app.question_window_target))
        return self.consent_action()

    def select(self, dialog, identity):
        dialog.combo.set_active_id(identity)
        return 1

    def finish_listing(self):
        self.assertEqual(len(self.work), 1)
        self.work.pop()()
        self.assertEqual(len(self.idle), 1)
        self.assertFalse(self.idle.pop()())

    def assert_no_capture(self):
        self.assertEqual(self.launches, [])
        self.assertIsNone(self.app._capture_request)
        self.assertIsNone(self.app._question_window_request)

    def test_positive_fixed_choice_then_consent_launches_only_that_window(self):
        self.app.manual_capture()
        self.assertEqual(self.reads, [])
        self.assertEqual(self.confirmations, [])
        self.assertEqual(self.launches, [])
        self.finish_listing()
        self.assertEqual(len(self.confirmations), 1)
        self.assertEqual(self.confirmations[0][2], WINDOW_A["id"])
        self.assertIn(WINDOW_A["title"], self.confirmations[0][1])
        self.assertEqual(self.launches, [(["ask", "--read-screen", "--authorized", "--provider", "mock", "--no-vision"], WINDOW_A["id"])])
        self.assertNotIn("automatic", [identity for identity, _title in self.dialogs[0].combo.items])
        self.assertTrue(self.dialogs[0].destroyed)
        self.assertIsNone(self.app._capture_request)
        self.assertIsNone(self.app._question_window_request)

    def test_cancel_no_selection_and_automatic_never_resume_capture(self):
        for result, selection in ((0, None), (1, None), (1, "automatic"), (1, "invalid"), (1, "99")):
            with self.subTest(result=result, selection=selection):
                self.setUp()

                def choose(dialog):
                    self.assertFalse(dialog.sensitive[1])
                    self.assertIsNone(dialog.combo.get_active_id())
                    dialog.combo.set_active_id(selection)
                    return result

                self.dialog_action = choose
                self.app.manual_capture()
                self.finish_listing()
                self.assertEqual(self.confirmations, [])
                self.assertIsNone(self.app.question_window_target)
                self.assert_no_capture()

    def test_two_capture_clicks_share_one_pending_chooser(self):
        self.app.manual_capture()
        self.app.manual_capture()
        self.assertEqual(len(self.work), 1)
        self.finish_listing()
        self.assertEqual(len(self.dialogs), 1)
        self.assertEqual(len(self.confirmations), 1)
        self.assertEqual(len(self.launches), 1)

    def test_stop_while_stopped_invalidates_pending_worker(self):
        self.app.manual_capture()
        self.app.stop()
        self.finish_listing()
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.confirmations, [])
        self.assert_no_capture()

    def test_stop_inside_modal_chooser_refuses_late_positive_result(self):
        def choose(dialog):
            self.app.stop()
            return self.select(dialog, "0")

        self.dialog_action = choose
        self.app.manual_capture()
        self.finish_listing()
        self.assertEqual(self.confirmations, [])
        self.assertIsNone(self.app.question_window_target)
        self.assert_no_capture()

    def test_new_session_generation_cannot_be_reused_after_it_ends(self):
        self.app.manual_capture()
        self.app._process_generation += 1
        self.app.proc = None
        self.finish_listing()
        self.assertEqual(self.dialogs, [])
        self.assertIsNone(self.app.question_window_target)
        self.assert_no_capture()

    def test_close_during_chooser_listing_cannot_select_or_launch(self):
        self.app.manual_capture()
        self.app._exiting = True
        self.finish_listing()
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.confirmations, [])
        self.assert_no_capture()

    def test_target_change_or_stop_inside_accepted_consent_refuses_launch(self):
        for change in (lambda: setattr(self.app, "question_window_target", WINDOW_B["id"]), self.app.stop):
            self.app.question_window_target = WINDOW_A["id"]
            self.app.question_window_title = WINDOW_A["title"]
            self.consent_action = lambda: (change(), True)[1]
            self.app.manual_capture()
            self.assert_no_capture()

    def test_declined_consent_preserves_explicit_selection_without_launch(self):
        self.consent_action = lambda: False
        self.app.manual_capture()
        self.finish_listing()
        self.assertEqual(self.app.question_window_target, WINDOW_A["id"])
        self.assertEqual(len(self.confirmations), 1)
        self.assert_no_capture()

    def test_regular_window_picker_still_allows_automatic_without_capture(self):
        self.app.question_window_target = WINDOW_A["id"]
        self.dialog_action = lambda dialog: self.select(dialog, "automatic")
        self.app.choose_question_window()
        self.finish_listing()
        self.assertIsNone(self.app.question_window_target)
        self.assertEqual(self.confirmations, [])
        self.assert_no_capture()

    def test_missing_or_malformed_inventory_clears_intent_and_allows_retry(self):
        for windows in ([], {}, [{"id": 5, "title": "invalid"}]):
            with self.subTest(windows=windows):
                self.windows = windows
                self.app.manual_capture()
                self.finish_listing()
                self.assertEqual(self.dialogs, [])
                self.assert_no_capture()

    def test_new_start_cancels_old_capture_even_if_start_consent_is_declined(self):
        self.app.manual_capture()
        self.consent_action = lambda: False
        self.app.start()
        self.finish_listing()
        self.assertEqual(self.dialogs, [])
        self.assertEqual(len(self.confirmations), 1)
        self.assertEqual(self.confirmations[0][0], "Start selected sources?")
        self.assert_no_capture()

    def test_new_launch_attempt_invalidates_old_chooser_before_cli_availability_check(self):
        self.app.manual_capture()
        self.assertFalse(self.method("run_cli")(self.app, ["ask"]))
        self.finish_listing()
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.confirmations, [])
        self.assert_no_capture()

    def test_live_capture_keeps_existing_stdin_protocol_and_opens_no_chooser(self):
        lines = []
        self.app.proc = SimpleNamespace(poll=lambda: None, stdin=SimpleNamespace(write=lines.append, flush=lambda: None))
        self.app._stdin_live = True
        self.app.manual_capture()
        self.assertEqual(lines, ["read-screen\n"])
        self.assertEqual(self.work, [])
        self.assertEqual(self.confirmations, [])
        self.assertEqual(self.launches, [])


if __name__ == "__main__":
    unittest.main()
