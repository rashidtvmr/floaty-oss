"""GTK-free regressions for slow desktop probes and their real UI adapters.

Only generated statuses and injected workers are used. No compositor, system
service, provider, desktop, preference file or real credential is accessed.
"""
import ast
import copy
import queue
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "apps/socket_stream_desktop/ui"
sys.path.insert(0, str(UI))

from background_refresh import BackgroundRefresh  # noqa: E402
from capture_rules import CaptureRuleError, RuleStatus  # noqa: E402
from overlay_guard import build_plan, detect_session  # noqa: E402


class Dispatch:
    def __init__(self):
        self.queue = queue.Queue()
        self.thread = threading.get_ident()

    def __call__(self, callback, *arguments):
        self.queue.put((callback, arguments))
        return 1

    def finish_one(self):
        assert threading.get_ident() == self.thread
        callback, arguments = self.queue.get(timeout=2)
        return callback(*arguments)


class BackgroundRefreshTests(unittest.TestCase):
    def test_creation_is_lazy_and_one_worker_coalesces_many_requests(self):
        dispatched = Dispatch()
        entered, release = threading.Event(), threading.Event()
        reads, applied = [], []

        def read():
            reads.append(threading.get_ident())
            entered.set()
            self.assertTrue(release.wait(2))
            return len(reads)

        refresh = BackgroundRefresh(read, lambda value, error: applied.append(
            (value, error, threading.get_ident())), dispatched)
        self.addCleanup(release.set)
        self.addCleanup(refresh.close)
        self.assertEqual(reads, [])
        self.assertTrue(refresh.request())
        self.assertTrue(entered.wait(1))
        for _ in range(100):
            self.assertFalse(refresh.request())
        self.assertEqual(len(reads), 1)
        self.assertEqual(applied, [])
        release.set()
        self.assertFalse(dispatched.finish_one())
        self.assertFalse(dispatched.finish_one())
        self.assertEqual([value for value, _, _ in applied], [1, 2])
        self.assertTrue(all(worker != dispatched.thread for worker in reads))
        self.assertTrue(all(thread == dispatched.thread for _, _, thread in applied))
        self.assertTrue(dispatched.queue.empty())

    def test_invalidated_result_is_discarded_before_latest_read_applies(self):
        dispatched, work, applied = Dispatch(), [], []
        refresh = BackgroundRefresh(lambda: "old", lambda *args: applied.append(args),
                                    dispatched, start=work.append)
        refresh.request()
        work.pop()()
        refresh.invalidate()
        refresh._read = lambda: "new"
        self.assertFalse(refresh.request())
        dispatched.finish_one()
        self.assertEqual(applied, [])
        work.pop()()
        dispatched.finish_one()
        self.assertEqual(applied, [("new", None)])

    def test_close_during_blocked_read_returns_and_suppresses_followup(self):
        dispatched = Dispatch()
        entered, release = threading.Event(), threading.Event()
        applied, reads = [], []

        def read():
            reads.append(True)
            entered.set()
            self.assertTrue(release.wait(2))
            return "late"

        refresh = BackgroundRefresh(read, lambda *args: applied.append(args), dispatched)
        self.addCleanup(release.set)
        refresh.request()
        self.assertTrue(entered.wait(1))
        refresh.request()
        refresh.close()
        self.assertFalse(release.is_set())  # close neither joins nor releases the worker
        self.assertFalse(refresh.request())
        release.set()
        dispatched.finish_one()
        self.assertEqual(applied, [])
        self.assertEqual(reads, [True])
        self.assertTrue(dispatched.queue.empty())

    def test_close_before_worker_start_does_not_run_the_probe(self):
        dispatched, work, reads = Dispatch(), [], []
        refresh = BackgroundRefresh(lambda: reads.append(True), self.fail,
                                    dispatched, start=work.append)
        refresh.request()
        refresh.close()
        work.pop()()
        dispatched.finish_one()
        self.assertEqual(reads, [])

    def test_probe_error_is_sanitized_and_next_request_recovers(self):
        dispatched, work, applied = Dispatch(), [], []

        def fail():
            raise RuntimeError("SYNTHETIC-PRIVATE-DETAIL")

        refresh = BackgroundRefresh(fail, lambda *args: applied.append(args),
                                    dispatched, start=work.append)
        refresh.request()
        work.pop()()
        dispatched.finish_one()
        self.assertEqual(applied, [(None, "RuntimeError")])
        refresh._read = lambda: "recovered"
        self.assertTrue(refresh.request())
        work.pop()()
        dispatched.finish_one()
        self.assertEqual(applied[-1], ("recovered", None))

    def test_thread_start_failure_is_delivered_on_main_thread(self):
        dispatched, applied = Dispatch(), []

        def unavailable(_work):
            raise RuntimeError("synthetic thread resource failure")

        refresh = BackgroundRefresh(self.fail, lambda *args: applied.append(args),
                                    dispatched, start=unavailable)
        refresh.request()
        self.assertEqual(applied, [])
        dispatched.finish_one()
        self.assertEqual(applied, [(None, "RuntimeError")])

    def test_dispatch_failure_never_applies_on_worker_and_allows_retry(self):
        applied, work = [], []

        def unavailable(*_args):
            raise RuntimeError("synthetic closed main context")

        refresh = BackgroundRefresh(lambda: "value", lambda *args: applied.append(args),
                                    unavailable, start=work.append)
        refresh.request()
        work.pop()()
        self.assertEqual(applied, [])
        dispatched = Dispatch()
        refresh._dispatch = dispatched
        self.assertTrue(refresh.request())
        work.pop()()
        dispatched.finish_one()
        self.assertEqual(applied, [("value", None)])

    def test_apply_failure_releases_slot_and_keeps_coalesced_request(self):
        dispatched, work = Dispatch(), []

        def fail(*_args):
            raise ValueError("synthetic UI callback failure")

        refresh = BackgroundRefresh(lambda: "value", fail, dispatched, start=work.append)
        refresh.request()
        refresh.request()
        work.pop()()
        with self.assertRaises(ValueError):
            dispatched.finish_one()
        self.assertEqual(len(work), 1)
        refresh.close()
        work.pop()()
        dispatched.finish_one()


class Widget:
    """Record UI changes and reject any worker-thread widget access."""
    def __init__(self, *, owner=None, **_kwargs):
        self.owner = owner or threading.get_ident()
        self.calls = []

    def __call__(self, *_args, **_kwargs):
        self._record("called")

    def _record(self, name, *args):
        assert threading.get_ident() == self.owner, "worker accessed a GTK widget"
        self.calls.append((name, args))

    def __getattr__(self, name):
        if name.startswith(("set_", "show", "hide", "connect", "apply_", "bind_", "render_")):
            return lambda *args, **_kwargs: self._record(name, *args)
        widget = Widget(owner=self.owner)
        setattr(self, name, widget)
        return widget


def controller_class(scope):
    """Compile production constructor/adapters with synthetic widget bindings.

    Keeping the real factory preamble catches any future blocking backend
    construction before the controller exists. Only the GI imports and unrelated
    UI methods are replaced; the constructor and probe/action paths are intact.
    """
    source = ast.parse((UI / "linux-gtk.py").read_text(encoding="utf-8"))
    factory = copy.deepcopy(next(node for node in source.body
                                 if isinstance(node, ast.FunctionDef) and node.name == "create_window"))
    kept = {
        "__init__", "refresh_capture_status", "_read_capture_status", "_capture_status_ready",
        "_render_capture_status", "_apply_capture_plan", "_run_capture_action", "poll_share_state",
        "_read_share_state", "_share_state_ready", "report_capabilities", "_gnome_capture_text",
        "show_capture_details", "on_exit",
        "start", "show_answer_panel",
    }
    factory.body = [node for node in factory.body if not isinstance(node, (ast.Import, ast.ImportFrom, ast.Try))]
    controller = next(node for node in factory.body if isinstance(node, ast.ClassDef))
    controller.body = [node for node in controller.body
                       if isinstance(node, ast.FunctionDef) and node.name in kept]
    helpers = [copy.deepcopy(node) for node in source.body if isinstance(node, ast.FunctionDef)
               and node.name in ("observe_args", "live_start_args", "start_if_consented")]
    exec(compile(ast.fix_missing_locations(ast.Module(body=helpers + [factory], type_ignores=[])),
                 str(UI / "linux-gtk.py"), "exec"), scope)
    return scope["create_window"]


class DesktopProbeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.dispatch = Dispatch()
        self.factory_threads = []
        self.release = threading.Event()
        self.release.set()
        self.entered = threading.Event()
        self.read_count = 0
        self.share_count = 0
        self.session = detect_session({"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "synthetic",
                                       "XDG_CURRENT_DESKTOP": "GNOME"})
        self.status = RuleStatus("gnome", True, False, False, "synthetic ready", can_install=True)
        self.backend = SimpleNamespace(status=self.read_status, note=lambda: "")

        def factory(**_kwargs):
            self.factory_threads.append(threading.get_ident())
            self.assertNotEqual(threading.get_ident(), self.dispatch.thread,
                                "backend construction blocked the GTK thread")
            return self.backend

        self.scope = dict(
            Gtk=SimpleNamespace(Window=Widget, Settings=SimpleNamespace(get_default=lambda: None),
                                main_level=lambda: 0),
            GLib=SimpleNamespace(idle_add=self.dispatch, timeout_add_seconds=lambda *_: 1,
                                 timeout_add=lambda *_: 1, source_remove=lambda *_: None),
            os=SimpleNamespace(environ={}), threading=threading, ROOT=ROOT,
            LiveState=lambda: SimpleNamespace(startup=lambda: None),
            RuleStatus=RuleStatus, CaptureExclusion=factory, CaptureRuleError=CaptureRuleError,
            BackgroundRefresh=BackgroundRefresh, FloatingOverlay=Widget,
            ScreenShareWatcher=lambda **_: SimpleNamespace(poll=self.poll_share,
                status=lambda: {"checked": True, "detail": "synthetic share status"}),
            load_provider_settings=lambda: {},
            load_provider_profiles=lambda: {"fallback_profile": "", "profiles": {}},
            load_appearance=lambda: {}, DEFAULT_APPEARANCE={},
            detect_session=lambda: self.session, ensure_panel_backend=lambda _: "",
            backend_is_x11=lambda _: False, build_plan=build_plan,
            build_workspace=lambda _: None,
            capability_report=lambda *_args, **_kwargs: {"guaranteed": False},
        )
        self.make = controller_class(self.scope)
        self.app = None
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.release.set()
        if self.app:
            self.app._exiting = True
            self.app._capture_refresh.close()
            self.app._share_refresh.close()

    def read_status(self):
        self.read_count += 1
        self.entered.set()
        if not self.release.wait(2):
            raise RuntimeError("synthetic read not released")
        return self.status

    def poll_share(self):
        self.share_count += 1
        self.assertNotEqual(threading.get_ident(), self.dispatch.thread)
        return False

    def create(self):
        self.app = self.make()
        return self.app

    def drain_until(self, predicate):
        for _ in range(12):
            if predicate():
                return
            self.dispatch.finish_one()
        self.assertTrue(predicate(), "synthetic main-loop delivery did not settle")

    def test_first_window_and_share_callbacks_do_not_wait_for_status(self):
        self.release.clear()
        app = self.create()
        self.assertTrue(self.entered.wait(1))
        self.assertFalse(self.release.is_set())
        self.assertTrue(app._capture_status_pending)
        self.assertFalse(any((app.capture_rules.configured, app.capture_rules.runtime_active,
                              app.capture_rules.verified, app.capture_rules.can_install,
                              app.capture_rules.can_remove)))
        app.floating.show()  # still responsive while the actual status worker is held
        self.dispatch.finish_one()  # independent share result can reach the UI
        self.assertEqual(self.share_count, 1)
        self.assertTrue(app.share_status.calls)
        self.assertEqual(self.read_count, 1)
        self.release.set()
        self.drain_until(lambda: not app._capture_status_pending)
        self.assertEqual(app.capture_rules, self.status)
        self.assertEqual(len(self.factory_threads), 1)

    def test_repeated_refreshes_coalesce_and_cache_rendering_never_queries(self):
        self.release.clear()
        app = self.create()
        self.assertTrue(self.entered.wait(1))
        for _ in range(100):
            self.assertIs(app.refresh_capture_status(), app.capture_rules)
            app.report_capabilities()
            app._apply_capture_plan()
        self.assertEqual(self.read_count, 1)
        self.release.set()
        self.drain_until(lambda: self.read_count == 2 and not app._capture_refresh._busy)
        self.assertEqual(self.read_count, 2)

    def test_failed_probe_clears_success_claims_and_later_probe_recovers(self):
        app = self.create()
        self.drain_until(lambda: not app._capture_status_pending)
        app.capture_rules = RuleStatus("gnome", True, True, False, "old", runtime_active=True)

        def fail():
            raise RuntimeError("SYNTHETIC-NOT-FOR-UI")

        self.backend.status = fail
        app.refresh_capture_status()
        self.drain_until(lambda: not app._capture_refresh._busy)
        self.assertFalse(app.capture_rules.configured)
        self.assertFalse(app.capture_rules.runtime_active)
        self.assertFalse(app.capture_rules.can_install)
        self.assertNotIn("SYNTHETIC-NOT-FOR-UI", repr(app.capture_rules))
        self.backend.status = self.read_status
        app.refresh_capture_status()
        self.drain_until(lambda: not app._capture_refresh._busy)
        self.assertEqual(app.capture_rules, self.status)

    def test_mutation_waits_off_ui_thread_and_old_queued_read_cannot_overwrite_it(self):
        self.release.clear()
        app = self.create()
        self.assertTrue(self.entered.wait(1))
        action_started = threading.Event()
        installed = RuleStatus("gnome", True, True, False, "synthetic installed", can_remove=True)

        def install():
            self.assertNotEqual(threading.get_ident(), self.dispatch.thread)
            self.assertTrue(self.release.is_set())
            action_started.set()
            return installed

        self.backend.install = install
        app._run_capture_action("install")
        app._run_capture_action("remove")  # a second action cannot pass the busy gate
        self.assertTrue(app.capture_busy)
        self.assertFalse(action_started.is_set())
        app.refresh_capture_status()
        self.release.set()
        self.assertTrue(action_started.wait(1))
        # Apply the mutation first, deliberately delivering the old status last.
        callbacks = [self.dispatch.queue.get(timeout=2) for _ in range(3)]
        mutation = next(item for item in callbacks if item[0].__name__ == "finish")
        mutation[0](*mutation[1])
        self.assertEqual(app.capture_rules, installed)
        for callback, arguments in callbacks:
            if callback is not mutation[0]:
                callback(*arguments)
        self.assertEqual(app.capture_rules, installed)
        self.assertFalse(app.capture_busy)
        self.assertEqual(self.read_count, 1)

    def test_action_failure_is_conservative_and_recovers_asynchronously(self):
        app = self.create()
        self.drain_until(lambda: not app._capture_status_pending)

        def fail():
            raise CaptureRuleError("synthetic action failed")

        self.backend.remove = fail
        app._run_capture_action("remove")
        self.drain_until(lambda: not app.capture_busy)
        self.assertFalse(app.capture_rules.configured)
        self.assertFalse(app.capture_rules.runtime_active)
        self.drain_until(lambda: not app._capture_refresh._busy)
        self.assertEqual(app.capture_rules, self.status)

    def test_exit_during_status_discards_results_without_waiting_for_worker(self):
        self.release.clear()
        app = self.create()
        self.assertTrue(self.entered.wait(1))
        initial = app.capture_rules
        app.proc = None
        app.on_exit()
        self.assertTrue(app._exiting)
        self.assertFalse(self.release.is_set())
        self.release.set()
        self.dispatch.finish_one()
        self.dispatch.finish_one()
        self.assertIs(app.capture_rules, initial)
        self.assertFalse(app.poll_share_state())

    def test_exit_before_queued_mutation_skips_the_mutation(self):
        self.release.clear()
        app = self.create()
        self.assertTrue(self.entered.wait(1))
        mutations = []
        self.backend.install = lambda: mutations.append(True)
        app._run_capture_action("install")
        app.proc = None
        app.on_exit()
        self.release.set()
        self.drain_until(lambda: not app.capture_busy)
        self.assertEqual(mutations, [])

    def test_share_probe_failure_stays_unverified_without_hiding_panel(self):
        app = self.create()
        self.drain_until(lambda: not app._capture_status_pending)
        app._share_state_ready(None, "RuntimeError")
        self.assertIn(("apply_share_state", (False,)), app.floating.calls)
        self.assertFalse(any(name == "hide" for name, _ in app.floating.calls))
        self.assertIn("probe unavailable", app.share_status.calls[-1][1][0])
        app._exiting = True
        previous = list(app.share_status.calls)
        app._share_state_ready((True, {"checked": True}), None)
        self.assertEqual(app.share_status.calls, previous)

    def prepare_start(self, app, *, consent=True):
        launched = []
        app.audio_check = SimpleNamespace(get_active=lambda: False)
        app.screen_check = SimpleNamespace(get_active=lambda: True)
        app.force_ocr = app.portal = SimpleNamespace(get_active=lambda: False)
        app.interval = SimpleNamespace(get_value_as_int=lambda: 5)
        app.selected_provider = lambda: "mock"
        app.provider_display = lambda: "Generated-only mock"
        app.question_window_target = "synthetic-only-window"
        app.question_window_title = "Generated fixture"
        app.confirmation = lambda *_: consent
        app.run_cli = lambda args: launched.append(args) or True
        return launched

    def test_source_start_and_panel_are_responsive_during_slow_compositor_read(self):
        self.release.clear()
        app = self.create()
        self.assertTrue(self.entered.wait(1))
        launched = self.prepare_start(app)
        checked = threading.Event()

        def preflight(*_args):
            self.assertNotEqual(threading.get_ident(), self.dispatch.thread)
            checked.set()
            return [], ["synthetic readiness passed"]

        with patch.dict(sys.modules, source_preflight=SimpleNamespace(check_sources=preflight)):
            app.start()
            self.assertTrue(checked.wait(1))
            self.assertTrue(app._starting)
            self.assertIn(("show", ()), app.floating.calls)
            self.drain_until(lambda: not app._starting)
        self.assertFalse(self.release.is_set())
        self.assertEqual(launched, [["observe-live", "--authorized", "--interval", "5",
                                    "--max-captures", "1000", "--provider", "mock"]])

    def test_unexpected_source_readiness_error_completes_without_launch(self):
        app = self.create()
        self.drain_until(lambda: not app._capture_status_pending)
        launched = self.prepare_start(app)

        def fail(*_args):
            raise RuntimeError("SYNTHETIC-PRIVATE-PREFLIGHT-ERROR")

        with patch.dict(sys.modules, source_preflight=SimpleNamespace(check_sources=fail)):
            app.start()
            self.drain_until(lambda: not app._starting)
        self.assertEqual(launched, [])
        notice = app.floating.notice.calls
        self.assertTrue(any("could not complete" in str(arguments) for _, arguments in notice))
        self.assertNotIn("SYNTHETIC-PRIVATE", repr(notice))

    def test_declined_start_neither_probes_sources_nor_launches(self):
        app = self.create()
        self.drain_until(lambda: not app._capture_status_pending)
        launched = self.prepare_start(app, consent=False)
        with patch.dict(sys.modules, source_preflight=SimpleNamespace(check_sources=self.fail)):
            app.start()
        self.assertFalse(app._starting)
        self.assertEqual(launched, [])

    def test_readiness_completion_after_exit_cannot_launch(self):
        app = self.create()
        self.drain_until(lambda: not app._capture_status_pending)
        launched = self.prepare_start(app)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def preflight(*_args):
            entered.set()
            self.assertTrue(release.wait(2))
            return [], []

        with patch.dict(sys.modules, source_preflight=SimpleNamespace(check_sources=preflight)):
            app.start()
            self.assertTrue(entered.wait(1))
            app.on_exit()
            release.set()
            while True:
                callback, arguments = self.dispatch.queue.get(timeout=2)
                callback(*arguments)
                if callback.__name__ == "ready":
                    break
        self.assertEqual(launched, [])


if __name__ == "__main__":
    unittest.main()
