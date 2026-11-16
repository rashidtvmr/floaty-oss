"""Contract tests for the accessibility helper scripts.

These tests are headless: they never import ``gi``, never touch X11/D-Bus and
never capture pixels. They exercise the pure pieces of
``scripts/linux-accessibility.py`` through duck-typed node objects, and check
the macOS helper's source contract (and typecheck it when ``swiftc`` exists).
"""
import importlib.util
import contextlib
import io
import pathlib
import shutil
import subprocess
import sys
import types
import unittest
import weakref
import gc
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
LINUX_HELPER = ROOT / "scripts" / "linux-accessibility.py"
MACOS_HELPER = ROOT / "scripts" / "macos-accessibility.swift"


def load_helper():
    spec = importlib.util.spec_from_file_location("linux_accessibility", LINUX_HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = load_helper()


class FakeAtspi:
    class StateType:
        FOCUSED = "FOCUSED"
        ACTIVE = "ACTIVE"
        PROTECTED = "PROTECTED"

    class CoordType:
        SCREEN = 0


class FakeStateSet:
    def __init__(self, states):
        self.states = set(states)

    def contains(self, state):
        return state in self.states


class FakeText:
    def __init__(self, text):
        self.text = text

    def get_character_count(self):
        return len(self.text)

    def get_text(self, start, end):
        return self.text[start:end]


class FakeNode:
    def __init__(self, role, name="", text=None, states=(), children=(), bounds=None, pid=None):
        self.role = role
        self.name = name
        self.text = text
        self.states = states
        self.children = list(children)
        self.bounds = bounds
        self.pid = pid

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_text_iface(self):
        return FakeText(self.text) if self.text is not None else None

    def get_state_set(self):
        return FakeStateSet(self.states)

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_extents(self, coord_type):
        return self.bounds

    def get_process_id(self):
        return self.pid


class FakeBounds:
    def __init__(self, x, y, width, height):
        self.x = x
        self.y = y
        self.width = width
        self.height = height


def sample_desktop():
    paragraph = FakeNode("paragraph", text="Find two numbers that sum to a target")
    entry = FakeNode("entry", name="Answer", text="", states=("FOCUSED",))
    image = FakeNode("image", name="Chart", bounds=FakeBounds(10, 20, 300, 200))
    password = FakeNode("password text", name="Secret", text="hunter2")
    app = FakeNode(
        "frame",
        name="Firefox",
        pid=4321,
        states=("ACTIVE",),
        children=[paragraph, entry, image, password],
    )
    return FakeNode("desktop", children=[app]), app


class EphemeralWrapperTests(unittest.TestCase):
    def root(self):
        testcase = self

        class EphemeralChildren(FakeNode):
            def __init__(self):
                super().__init__('frame')
                self.references = []

            def get_child_count(self):
                return 12

            def get_child_at_index(self, index):
                testcase.assertTrue(all(ref() is not None for ref in self.references),
                                    'visited GI wrappers must remain alive until traversal ends')
                node = FakeNode('paragraph', text=f'Question sibling {index}',
                                states=('FOCUSED',) if index == 11 else ())
                self.references.append(weakref.ref(node))
                return node

        return EphemeralChildren()

    def test_tree_retains_ephemeral_wrappers_across_siblings(self):
        root = self.root()
        tree = helper.build_tree(root, FakeAtspi, 100, 20, 32000)
        self.assertEqual(len(tree['nodes']), 13)
        self.assertIn('Question sibling 11', tree['nodes'][-1]['text'])
        gc.collect()
        self.assertTrue(all(ref() is None for ref in root.references), 'no retained capture after the read')

    def test_focus_search_retains_ephemeral_wrappers(self):
        self.assertIsNotNone(helper.focused_descendant(self.root(), FakeAtspi))

    def test_legacy_text_retains_ephemeral_wrappers(self):
        root = self.root()
        with mock.patch.object(helper, 'select_foreground', return_value=root):
            self.assertIn('Question sibling 11', helper.extract(None, FakeAtspi))


class ArgumentParsingTests(unittest.TestCase):
    def test_module_import_does_not_require_gi(self):
        real_import = __import__

        def reject_gi(name, *args, **kwargs):
            if name == "gi" or name.startswith("gi."):
                raise AssertionError("importing the helper must not load gi")
            return real_import(name, *args, **kwargs)

        spec = importlib.util.spec_from_file_location("linux_accessibility_no_gi", LINUX_HELPER)
        module = importlib.util.module_from_spec(spec)
        with mock.patch("builtins.__import__", reject_gi):
            spec.loader.exec_module(module)

    def test_defaults_are_tree_mode(self):
        args = helper.parse_args([])
        self.assertEqual(args.mode, "tree")
        self.assertEqual(args.max_nodes, 800)
        self.assertEqual(args.max_depth, 20)
        self.assertEqual(args.max_chars, 32000)

    def test_flags_are_honoured(self):
        args = helper.parse_args(
            ["--mode", "text", "--max-nodes", "5", "--max-depth", "2", "--max-chars", "99"]
        )
        self.assertEqual(args.mode, "text")
        self.assertEqual(args.max_nodes, 5)
        self.assertEqual(args.max_depth, 2)
        self.assertEqual(args.max_chars, 99)

    def test_negative_bounds_are_clamped(self):
        args = helper.parse_args(["--max-nodes", "-1", "--max-depth", "-3", "--max-chars", "-9"])
        self.assertEqual((args.max_nodes, args.max_depth, args.max_chars), (0, 0, 0))

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(SystemExit):
            helper.parse_args(["--mode", "bogus"])


class ClassificationTests(unittest.TestCase):
    def test_image_roles(self):
        for role in ("image", "canvas", "graphic", "icon", "chart", "drawing", "figure", "image button"):
            self.assertTrue(helper.role_is_image(role), role)
        for role in ("push button", "entry", "label", ""):
            self.assertFalse(helper.role_is_image(role), role)

    def test_protected_roles(self):
        self.assertTrue(helper.role_is_protected("password text"))
        self.assertTrue(helper.role_is_protected("secure entry"))
        self.assertTrue(helper.role_is_protected("entry", True))
        self.assertFalse(helper.role_is_protected("entry"))
        self.assertFalse(helper.role_is_protected("label", False))

    def test_bounds_normalisation(self):
        self.assertEqual(helper.normalize_bounds((1, 2, 3, 4)), [1, 2, 3, 4])
        self.assertEqual(helper.normalize_bounds(FakeBounds(5, 6, 7, 8)), [5, 6, 7, 8])
        self.assertIsNone(helper.normalize_bounds(None))
        self.assertIsNone(helper.normalize_bounds((0, 0, 0, 4)))
        self.assertIsNone(helper.normalize_bounds((0, 0, -3, 4)))
        self.assertIsNone(helper.normalize_bounds(("x", 0, 1, 1)))
        self.assertIsNone(helper.normalize_bounds((0, 0, float("inf"), 1)))


class TreeShapeTests(unittest.TestCase):
    def test_json_shape_and_parent_linkage(self):
        desktop, _ = sample_desktop()
        payload = helper.extract_tree(desktop, FakeAtspi, helper.parse_args([]))
        self.assertEqual(
            set(payload),
            {"schema", "source", "app", "pid", "truncated", "node_count", "nodes"},
        )
        self.assertEqual(payload["schema"], 1)
        self.assertEqual(payload["source"], "atspi")
        self.assertEqual(payload["app"], "Firefox")
        self.assertEqual(payload["pid"], 4321)
        self.assertEqual(payload["node_count"], len(payload["nodes"]))
        self.assertEqual(payload["node_count"], 5)

        nodes = payload["nodes"]
        self.assertEqual([node["id"] for node in nodes], [1, 2, 3, 4, 5])
        self.assertIsNone(nodes[0]["parent"])
        for node in nodes[1:]:
            self.assertEqual(node["parent"], 1)
        for node in nodes:
            self.assertEqual(
                set(node),
                {"id", "parent", "role", "name", "text", "focused", "bounds", "image", "protected"},
            )

    def test_image_flag_and_bounds(self):
        desktop, _ = sample_desktop()
        payload = helper.extract_tree(desktop, FakeAtspi, helper.parse_args([]))
        image = next(node for node in payload["nodes"] if node["role"] == "image")
        self.assertTrue(image["image"])
        self.assertEqual(image["bounds"], [10, 20, 300, 200])
        paragraph = next(node for node in payload["nodes"] if node["role"] == "paragraph")
        self.assertFalse(paragraph["image"])
        self.assertIsNone(paragraph["bounds"])

    def test_protected_nodes_carry_no_text(self):
        desktop, _ = sample_desktop()
        payload = helper.extract_tree(desktop, FakeAtspi, helper.parse_args([]))
        password = next(node for node in payload["nodes"] if node["role"] == "password text")
        self.assertTrue(password["protected"])
        self.assertEqual(password["text"], "")

    def test_protected_state_suppresses_text_on_regular_role(self):
        field = FakeNode("entry", text="sensitive value", states=("PROTECTED",))
        app = FakeNode("frame", states=("ACTIVE",), children=[field])
        payload = helper.extract_tree(
            FakeNode("desktop", children=[app]), FakeAtspi, helper.parse_args([])
        )
        protected = payload["nodes"][1]
        self.assertTrue(protected["protected"])
        self.assertEqual(protected["text"], "")

    def test_focused_node_and_ancestors_are_marked(self):
        desktop, _ = sample_desktop()
        payload = helper.extract_tree(desktop, FakeAtspi, helper.parse_args([]))
        by_id = {node["id"]: node for node in payload["nodes"]}
        focused = [node for node in payload["nodes"] if node["focused"]]
        self.assertEqual({node["role"] for node in focused}, {"frame", "entry"})
        self.assertTrue(by_id[1]["focused"])

    def test_max_nodes_truncates(self):
        desktop, _ = sample_desktop()
        payload = helper.extract_tree(
            desktop, FakeAtspi, helper.parse_args(["--max-nodes", "2"])
        )
        self.assertEqual(payload["node_count"], 2)
        self.assertTrue(payload["truncated"])

    def test_max_chars_truncates_text(self):
        desktop, app = sample_desktop()
        # Keep the app label from consuming the content budget in this test.
        app.name = ""
        payload = helper.extract_tree(
            desktop, FakeAtspi, helper.parse_args(["--max-chars", "5"])
        )
        paragraph = next(node for node in payload["nodes"] if node["role"] == "paragraph")
        self.assertEqual(paragraph["text"], "Find ")
        self.assertLessEqual(
            sum(len(node["name"]) + len(node["text"]) for node in payload["nodes"]), 5
        )
        self.assertTrue(payload["truncated"])

    def test_max_chars_also_bounds_node_names(self):
        app = FakeNode("frame", name="Long application name", states=("ACTIVE",))
        desktop = FakeNode("desktop", children=[app])
        payload = helper.extract_tree(
            desktop, FakeAtspi, helper.parse_args(["--max-chars", "3"])
        )
        self.assertEqual(payload["app"], "Lon")
        self.assertEqual(payload["nodes"][0]["name"], "Lon")
        self.assertTrue(payload["truncated"])

    def test_preorder_and_parent_ids_for_nested_nodes(self):
        leaf = FakeNode("label", name="leaf")
        branch = FakeNode("group", name="branch", children=[leaf])
        app = FakeNode("frame", states=("ACTIVE",), children=[branch])
        desktop = FakeNode("desktop", children=[app])
        nodes = helper.extract_tree(desktop, FakeAtspi, helper.parse_args([]))["nodes"]
        self.assertEqual([node["role"] for node in nodes], ["frame", "group", "label"])
        self.assertEqual([node["parent"] for node in nodes], [None, 1, 2])

    def test_multiple_focused_flags_resolve_to_one_path(self):
        first = FakeNode("entry", states=("FOCUSED",))
        second = FakeNode("entry", states=("FOCUSED",))
        app = FakeNode("frame", states=("ACTIVE",), children=[first, second])
        nodes = helper.extract_tree(
            FakeNode("desktop", children=[app]), FakeAtspi, helper.parse_args([])
        )["nodes"]
        self.assertEqual([node["focused"] for node in nodes], [True, True, False])

    def test_deepest_focus_wins_over_ancestor_focus_flags(self):
        leaf = FakeNode("entry", states=("FOCUSED",))
        first_branch = FakeNode("group", states=("FOCUSED",))
        second_branch = FakeNode("group", children=[leaf])
        app = FakeNode(
            "frame", states=("ACTIVE", "FOCUSED"), children=[first_branch, second_branch]
        )
        nodes = helper.extract_tree(
            FakeNode("desktop", children=[app]), FakeAtspi, helper.parse_args([])
        )["nodes"]
        self.assertEqual([node["focused"] for node in nodes], [True, False, True, True])

    def test_child_limit_marks_tree_truncated(self):
        app = FakeNode(
            "frame",
            states=("ACTIVE",),
            children=[FakeNode("label", name=str(index)) for index in range(helper.MAX_CHILDREN + 1)],
        )
        payload = helper.extract_tree(
            FakeNode("desktop", children=[app]), FakeAtspi, helper.parse_args([])
        )
        self.assertEqual(payload["node_count"], helper.MAX_CHILDREN + 1)
        self.assertTrue(payload["truncated"])

    def test_max_depth_truncates(self):
        desktop, _ = sample_desktop()
        payload = helper.extract_tree(
            desktop, FakeAtspi, helper.parse_args(["--max-depth", "0"])
        )
        self.assertEqual(payload["node_count"], 1)
        self.assertTrue(payload["truncated"])

    def test_no_foreground_application_yields_empty_tree(self):
        empty_desktop = FakeNode("desktop", children=[])
        payload = helper.extract_tree(empty_desktop, FakeAtspi, helper.parse_args([]))
        self.assertEqual(payload["node_count"], 0)
        self.assertEqual(payload["nodes"], [])
        self.assertEqual(payload["app"], "")


class OwnWindowTests(unittest.TestCase):
    def test_own_window_raises(self):
        app = FakeNode("frame", name="SocketStream", pid=99, states=("ACTIVE",))
        desktop = FakeNode("desktop", children=[app])
        with self.assertRaises(helper.OwnWindowError):
            helper.extract_tree(desktop, FakeAtspi, helper.parse_args([]))

    def test_own_window_by_pid(self):
        import os

        app = FakeNode("frame", name="Firefox", pid=4242, states=("ACTIVE",))
        desktop = FakeNode("desktop", children=[app])
        previous = os.environ.get("SOCKET_STREAM_GUI_PID")
        os.environ["SOCKET_STREAM_GUI_PID"] = "4242"
        try:
            with self.assertRaises(helper.OwnWindowError):
                helper.extract_tree(desktop, FakeAtspi, helper.parse_args([]))
        finally:
            if previous is None:
                os.environ.pop("SOCKET_STREAM_GUI_PID", None)
            else:
                os.environ["SOCKET_STREAM_GUI_PID"] = previous


class MainExitCodeTests(unittest.TestCase):
    @staticmethod
    def run_with_atspi(desktop):
        gi = types.ModuleType("gi")
        gi.require_version = lambda *_args: None
        repository = types.ModuleType("gi.repository")

        def get_desktop(_index):
            if isinstance(desktop, BaseException):
                raise desktop
            return desktop

        repository.Atspi = types.SimpleNamespace(
            StateType=FakeAtspi.StateType,
            CoordType=FakeAtspi.CoordType,
            get_desktop=get_desktop,
        )
        gi.repository = repository
        with mock.patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = helper.main([])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_success_exit_code_zero(self):
        code, stdout, _stderr = self.run_with_atspi(FakeNode("desktop"))
        self.assertEqual(code, 0)
        self.assertEqual(__import__("json").loads(stdout)["nodes"], [])

    def test_backend_unavailable_exit_code_two(self):
        with mock.patch.dict(sys.modules, {"gi": None}):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(helper.main([]), 2)

    def test_read_failure_exit_code_three(self):
        code, _stdout, stderr = self.run_with_atspi(RuntimeError("broken backend"))
        self.assertEqual(code, 3)
        self.assertIn("AT-SPI read failed", stderr)

    def test_own_window_exit_code_four(self):
        app = FakeNode("frame", name="SocketStream", states=("ACTIVE",))
        code, _stdout, _stderr = self.run_with_atspi(FakeNode("desktop", children=[app]))
        self.assertEqual(code, 4)


class LegacyTextModeTests(unittest.TestCase):
    def test_text_mode_is_newline_joined(self):
        paragraph = FakeNode("paragraph", text="Find two numbers that sum to a target")
        label = FakeNode("label", name="Label text")
        app = FakeNode("frame", name="Firefox", pid=1, states=("ACTIVE",), children=[paragraph, label])
        desktop = FakeNode("desktop", children=[app])
        text = helper.extract(desktop, FakeAtspi)
        self.assertEqual(text, "Find two numbers that sum to a target\nLabel text")

    def test_text_mode_skips_protected(self):
        password = FakeNode("password text", name="Secret", text="hunter2")
        app = FakeNode("frame", name="Firefox", pid=1, states=("ACTIVE",), children=[password])
        desktop = FakeNode("desktop", children=[app])
        self.assertEqual(helper.extract(desktop, FakeAtspi), "")

    def test_text_mode_without_foreground_is_empty(self):
        desktop = FakeNode("desktop", children=[])
        self.assertEqual(helper.extract(desktop, FakeAtspi), "")


@unittest.skipUnless(MACOS_HELPER.is_file(), "macOS helper is not part of this package")
class MacOsHelperSourceTests(unittest.TestCase):
    def test_source_declares_the_same_flags_and_source_tag(self):
        source = MACOS_HELPER.read_text(encoding="utf-8")
        for flag in ("--mode", "--max-nodes", "--max-depth", "--max-chars"):
            self.assertIn(flag, source)
        self.assertIn('"source": "ax"', source)
        self.assertIn('"schema": 1', source)
        self.assertIn("AXProtectedContent", source)
        self.assertIn("roleIsProtected", source)

    def test_helper_exit_codes_remain_compatible(self):
        source = MACOS_HELPER.read_text(encoding="utf-8")
        for code in (0, 2, 3, 4):
            self.assertIn(f"return {code}", source)
        self.assertIn("exit(runMacOSAccessibility(CommandLine.arguments))", source)

    @unittest.skipUnless(shutil.which("swiftc"), "swiftc is not installed")
    def test_typechecks(self):
        result = subprocess.run(
            ["swiftc", "-typecheck", str(MACOS_HELPER)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
