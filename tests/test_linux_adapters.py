"""Mocked Linux adapter tests; no GUI or live accessibility session."""
import importlib.util
import ast
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ACCESS = load("access", "scripts/linux-accessibility.py")
GTK = load("gtk_shell", "apps/socket_stream_desktop/ui/linux-gtk.py")
ATSPI = SimpleNamespace(StateType=SimpleNamespace(ACTIVE="ACTIVE", FOCUSED="FOCUSED", PROTECTED="PROTECTED"))


class LiveHandoffTests(unittest.TestCase):
    def test_real_delivery_closure_bounds_callbacks_and_stops_on_saturation(self):
        tree = ast.parse((ROOT / "apps/socket_stream_desktop/ui/linux-gtk.py").read_text())
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "deliver")
        queued, delivered, stopped = [], [], []
        slots = threading.BoundedSemaphore(32)
        scope = dict(pending_ui=slots,
                     proc=SimpleNamespace(terminate=lambda: stopped.append(True)),
                     GLib=SimpleNamespace(idle_add=lambda callback: queued.append(callback)))
        exec(compile(ast.Module(body=[function], type_ignores=[]), "live-handoff", "exec"), scope)
        send = scope["deliver"]
        for index in range(32):
            self.assertTrue(send(delivered.append, index))
        self.assertFalse(send(delivered.append, "overflow"))
        self.assertEqual(len(queued), 32)
        self.assertEqual(stopped, [True])
        self.assertFalse(queued.pop(0)())
        self.assertEqual(delivered, [0])
        self.assertTrue(send(delivered.append, "reused"))
        self.assertEqual(len(queued), 32)


class Node:
    def __init__(self, role="application", text=None, states=(), kids=(), name="", pid=None):
        self.role, self.text, self.states, self.kids = role, text, set(states), list(kids)
        self.name, self.pid = name, pid

    def get_state_set(self):
        return SimpleNamespace(contains=lambda state: state in self.states)

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_process_id(self):
        return self.pid

    def get_text_iface(self):
        if self.text is None:
            return None
        return SimpleNamespace(get_character_count=lambda: len(self.text),
                               get_text=lambda start, end: self.text[start:end])

    def get_child_count(self):
        return len(self.kids)

    def get_child_at_index(self, index):
        return self.kids[index]


class LinuxAdapterTests(unittest.TestCase):
    def test_active_app_only(self):
        other = Node(kids=[Node("text", "other app")])
        active = Node(states=["ACTIVE"], kids=[Node("text", "foreground")])
        self.assertEqual(ACCESS.extract(Node(kids=[other, active]), ATSPI), "foreground")

    def test_focused_descendant_fallback(self):
        other = Node(kids=[Node("text", "other")])
        focus = Node(kids=[Node("text", "focused", states=["FOCUSED"])])
        self.assertEqual(ACCESS.extract(Node(kids=[other, focus]), ATSPI), "focused")

    def test_no_foreground_returns_empty(self):
        self.assertEqual(ACCESS.extract(Node(kids=[Node(kids=[Node("text", "secret")])]), ATSPI), "")

    def test_own_gui_is_not_treated_as_question(self):
        active = Node(states=["ACTIVE"], name="SocketStream", kids=[Node("text", "generated answer")])
        with self.assertRaises(ACCESS.OwnWindowError):
            ACCESS.extract(Node(kids=[active]), ATSPI)

    def test_password_and_protected_descendants_excluded(self):
        password = Node("password entry", "secret", kids=[Node("text", "nested")])
        protected = Node("entry", "pin", states=["PROTECTED"])
        active = Node(states=["ACTIVE"], kids=[password, protected, Node("text", "safe")])
        self.assertEqual(ACCESS.extract(Node(kids=[active]), ATSPI), "safe")

    def test_deduplicates_bounds_and_handles_cycle(self):
        active = Node(states=["ACTIVE"])
        active.kids = [Node("text", "same"), Node("text", "same"), Node("text", "x" * 100000), active]
        result = ACCESS.extract(Node(kids=[active]), ATSPI)
        self.assertEqual(result.count("same"), 1)
        self.assertLessEqual(sum(map(len, result.split("\n"))), ACCESS.MAX_CHARS)

    def test_gtk_import_is_headless(self):
        self.assertTrue(callable(GTK.main))

    def test_observe_args_require_supported_provider(self):
        for provider in ("mock", "codex", "openai-compatible", "ollama"):
            args = GTK.observe_args(provider)
            self.assertIn("--authorized", args)
            self.assertEqual(args[-1], provider)
        with self.assertRaises(ValueError):
            GTK.observe_args("unknown")

    def test_status_tracks_real_process(self):
        running = SimpleNamespace(poll=lambda: None)
        exited = SimpleNamespace(poll=lambda: 1)
        self.assertEqual(GTK.capture_status(None), "Stopped")
        self.assertEqual(GTK.capture_status(running), "Active")
        self.assertEqual(GTK.capture_status(running, True), "Stopping")
        self.assertEqual(GTK.capture_status(exited), "Stopped")

    def test_floating_panel_compact_mode_keeps_normal_visible_window_sizes(self):
        self.assertEqual(GTK.floating_panel_size(False), (520, 370))
        self.assertEqual(GTK.floating_panel_size(True), (360, 250))

    def test_minimize_iconifies_main_window_without_touching_panel(self):
        class Window:
            iconified = False

            def iconify(self):
                self.iconified = True

        window = Window()
        GTK.minimize_app_window(window)
        self.assertTrue(window.iconified)

    def test_observation_interval_validation(self):
        self.assertEqual(GTK.observe_args("mock", 7)[3], "7")
        for interval in (0, 1, 3601):
            with self.assertRaises(ValueError):
                GTK.observe_args("mock", interval)

    def test_stop_only_terminates_owned_process_group(self):
        from unittest import mock
        proc = SimpleNamespace(pid=42699, poll=lambda: None)
        with mock.patch.object(GTK.os, "killpg") as killpg:
            self.assertTrue(GTK.terminate_tree(proc))
            killpg.assert_called_once_with(42699, GTK.signal.SIGTERM)
        proc_finished = SimpleNamespace(pid=42700, poll=lambda: 0)
        with mock.patch.object(GTK.os, "killpg") as killpg:
            self.assertFalse(GTK.terminate_tree(proc_finished))
            killpg.assert_not_called()

    def test_linux_portable_launcher_files_are_present(self):
        for path in (
            ROOT / "scripts/linux-portable-launch.sh",
            ROOT / "scripts/build-linux-release.sh",
            ROOT / "scripts/linux-check-dependencies.sh",
            ROOT / "docs/LINUX-TESTING.md",
        ):
            self.assertTrue(path.is_file(), str(path))

if __name__ == "__main__":
    unittest.main()
