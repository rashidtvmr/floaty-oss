"""Contract tests for the accessibility helper scripts.

These tests are headless: they never import ``gi``, never touch X11/D-Bus and
never capture pixels. They exercise the pure pieces of
``scripts/linux-accessibility.py`` through duck-typed node objects, and check
the macOS helper's source contract (and typecheck it when ``swiftc`` exists).
"""
import importlib.util
import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import types
import unittest
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
        DEFUNCT = "DEFUNCT"

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
            {"schema", "source", "app", "pid", "window_id", "truncated", "node_count", "nodes"},
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
                {"id", "stable_id", "parent", "role", "name", "text", "focused", "bounds", "image", "protected"},
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


def native_node(role, path, bus=":1.42", **kwargs):
    node = FakeNode(role, **kwargs)
    node.app = types.SimpleNamespace(bus_name=bus)
    node.path = path
    return node


class StableIdentityTests(unittest.TestCase):
    def test_keys_survive_new_wrappers_insertions_and_reordering(self):
        first = native_node("frame", "/window/1", children=[
            native_node("label", "/label/a", text="One"),
            native_node("entry", "/entry/b", text="Two"),
        ])
        second = native_node("frame", "/window/1", children=[
            native_node("button", "/button/new", name="New"),
            native_node("entry", "/entry/b", text="Edited"),
            native_node("label", "/label/a", text="One"),
        ])
        old = helper.build_tree(first, FakeAtspi, 20, 5, 1000)
        new = helper.build_tree(second, FakeAtspi, 20, 5, 1000)
        self.assertEqual(old["window_id"], new["window_id"])
        self.assertEqual(json.loads(old["window_id"]), [":1.42", "/window/1"])
        self.assertEqual(old["nodes"][1]["stable_id"], new["nodes"][3]["stable_id"])
        self.assertNotEqual(old["nodes"][1]["id"], new["nodes"][3]["id"])
        self.assertEqual(old["nodes"][2]["stable_id"], new["nodes"][2]["stable_id"])

    def test_identical_paths_from_different_app_connections_do_not_alias(self):
        left = native_node("frame", "/window/1", bus=":1.42")
        right = native_node("frame", "/window/1", bus=":1.43")
        self.assertNotEqual(helper.window_identity(left), helper.window_identity(right))

    def test_missing_identity_is_explicit_and_fallback_retains_wrapper(self):
        node = FakeNode("label", text="Fallback")
        visited = {}
        self.assertTrue(helper.remember_node(node, visited))
        self.assertFalse(helper.remember_node(node, visited))
        self.assertIs(next(iter(visited.values())), node)
        tree = helper.build_tree(node, FakeAtspi, 10, 5, 1000)
        self.assertIsNone(tree["window_id"])
        self.assertIsNone(tree["nodes"][0]["stable_id"])

    def test_native_duplicates_and_proxy_cycles_are_bounded(self):
        left = native_node("label", "/label/a", text="Only once")
        duplicate = native_node("label", "/label/a", text="Only once")
        frame = native_node("frame", "/window/1", states=("ACTIVE",), children=[left, duplicate])
        proxy = native_node("frame", "/window/1", children=[frame])
        frame.children.append(proxy)
        tree = helper.build_tree(frame, FakeAtspi, 100, 20, 1000)
        self.assertEqual(tree["node_count"], 2)
        self.assertFalse(tree["truncated"])
        self.assertIsNone(helper.focused_descendant(frame, FakeAtspi))
        desktop = FakeNode("desktop", children=[FakeNode("application", children=[frame])])
        self.assertEqual(helper.extract(desktop, FakeAtspi), "Only once")

    def test_invalid_or_unavailable_native_identity_never_uses_title(self):
        for bus, path in (("app.name", "/window/1"), (":1.42", "not/a/path"),
                          (None, "/window/1"), (":1.42", None),
                          (":" + "1" * 256, "/x"), (":1.42", "/" + "a" * 2048)):
            self.assertIsNone(helper.window_identity(native_node("frame", path, bus=bus, name="Same title")))

        class BrokenProxy:
            @property
            def app(self):
                raise RuntimeError("defunct native wrapper")

        self.assertIsNone(helper.window_identity(BrokenProxy()))


class WaylandForegroundTests(unittest.TestCase):
    def test_gnome_decoration_mirrors_do_not_shadow_the_application_tree(self):
        for mirror in helper.COMPOSITOR_APPLICATIONS:
            with self.subTest(mirror=mirror):
                decoration = FakeNode("application", name=mirror, children=[
                    FakeNode("frame", name="Browser", states=("ACTIVE",))])
                frame = FakeNode("frame", name="Browser", states=("ACTIVE",),
                                 children=[FakeNode("paragraph", text="Native Wayland question")])
                desktop = FakeNode("desktop", children=[decoration, FakeNode("application", children=[frame])])
                with mock.patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}, clear=True):
                    self.assertIs(helper.select_foreground(desktop, FakeAtspi), frame)
                    self.assertEqual(helper.extract(desktop, FakeAtspi), "Native Wayland question")

    def test_active_mirror_does_not_promote_stale_background_focus(self):
        for mirror_name in helper.COMPOSITOR_APPLICATIONS:
            for active_level in ("application", "frame"):
                for mode in ("text", "tree"):
                    with self.subTest(mirror=mirror_name, active_level=active_level, mode=mode):
                        stale = FakeNode("entry", text="SYNTHETIC_BACKGROUND_PRIVATE", states=("FOCUSED",))
                        stale.get_text_iface = mock.Mock(wraps=stale.get_text_iface)
                        background = FakeNode("frame", name="Background", children=[stale])
                        mirror_frame = FakeNode("frame", states=("ACTIVE",) if active_level == "frame" else ())
                        mirror = FakeNode("application", name=mirror_name, children=[mirror_frame],
                                          states=("ACTIVE",) if active_level == "application" else ())
                        desktop = FakeNode("desktop", children=[
                            mirror, FakeNode("application", name="Background app", children=[background])])
                        with mock.patch.dict(os.environ, {
                            "XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"
                        }, clear=True):
                            with self.assertRaisesRegex(RuntimeError, "Choose question window"):
                                if mode == "text":
                                    helper.extract(desktop, FakeAtspi)
                                else:
                                    helper.extract_tree(desktop, FakeAtspi, helper.parse_args([]))
                        stale.get_text_iface.assert_not_called()

    def test_active_mirror_preserves_independent_active_application(self):
        current = FakeNode("frame", text="Current application content")
        active_app = FakeNode("application", states=("ACTIVE",), children=[current])
        mirror = FakeNode("application", name="gnome-shell", states=("ACTIVE",))
        background = FakeNode("application", children=[
            FakeNode("frame", children=[FakeNode("entry", states=("FOCUSED",), text="Background content")])])
        with mock.patch.dict(os.environ, {}, clear=True):
            desktop = FakeNode("desktop", children=[mirror, background, active_app])
            self.assertIs(helper.select_foreground(desktop, FakeAtspi), current)
            self.assertEqual(helper.extract(desktop, FakeAtspi), "Current application content")

    def test_active_mirror_with_failed_role_lookup_still_blocks_background_focus(self):
        mirror_frame = FakeNode("frame", states=("ACTIVE",))
        mirror_frame.get_role_name = mock.Mock(side_effect=RuntimeError("mirror role unavailable"))
        mirror = FakeNode("application", name="gnome-shell", children=[mirror_frame])
        stale = FakeNode("entry", states=("FOCUSED",), text="SYNTHETIC_BACKGROUND_PRIVATE")
        stale.get_text_iface = mock.Mock(wraps=stale.get_text_iface)
        desktop = FakeNode("desktop", children=[mirror, FakeNode("application", children=[FakeNode("frame", children=[stale])])])
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Choose question window"):
                helper.extract(desktop, FakeAtspi)
        stale.get_text_iface.assert_not_called()

    def test_active_mirror_preserves_explicit_unfocused_target(self):
        target = native_node("frame", "/window/selected", text="Explicitly selected question")
        mirror = FakeNode("application", name="gnome-shell", children=[FakeNode("frame", states=("ACTIVE",))])
        desktop = FakeNode("desktop", children=[mirror, FakeNode("application", children=[target])])
        with mock.patch.dict(os.environ, {"SOCKET_STREAM_ATSPI_TARGET": helper.window_identity(target)}, clear=True):
            self.assertIs(helper.select_foreground(desktop, FakeAtspi), target)
            self.assertEqual(helper.extract(desktop, FakeAtspi), "Explicitly selected question")

    def test_unreadable_active_window_role_blocks_stale_background_focus(self):
        for mode in ("text", "tree"):
            for background_states in ((), ("ACTIVE",)):
                with self.subTest(mode=mode, background_states=background_states):
                    unavailable = FakeNode("frame", name="Unreadable active window", states=("ACTIVE",))
                    unavailable.get_role_name = mock.Mock(side_effect=RuntimeError("native role unavailable"))
                    unavailable.get_name = mock.Mock(wraps=unavailable.get_name)
                    stale = FakeNode("entry", text="SYNTHETIC_BACKGROUND_PRIVATE", states=("FOCUSED",))
                    stale.get_text_iface = mock.Mock(wraps=stale.get_text_iface)
                    background = FakeNode("frame", states=background_states, children=[stale])
                    desktop = FakeNode("desktop", children=[FakeNode("application", children=[unavailable]),
                                                           FakeNode("application", children=[background])])
                    with mock.patch.dict(os.environ, {}, clear=True):
                        with self.assertRaisesRegex(RuntimeError, "Choose question window"):
                            if mode == "text":
                                helper.extract(desktop, FakeAtspi)
                            else:
                                helper.extract_tree(desktop, FakeAtspi, helper.parse_args([]))
                    unavailable.get_name.assert_not_called()
                    stale.get_text_iface.assert_not_called()

    def test_unreadable_active_window_role_preserves_explicit_other_target(self):
        unavailable = FakeNode("frame", states=("ACTIVE",))
        unavailable.get_role_name = mock.Mock(side_effect=RuntimeError("native role unavailable"))
        target = native_node("frame", "/window/selected", text="Explicit question")
        desktop = FakeNode("desktop", children=[FakeNode("application", children=[unavailable]),
                                               FakeNode("application", children=[target])])
        with mock.patch.dict(os.environ, {"SOCKET_STREAM_ATSPI_TARGET": helper.window_identity(target)}, clear=True):
            self.assertEqual(helper.extract(desktop, FakeAtspi), "Explicit question")

    def test_unreadable_inactive_or_defunct_window_does_not_block_focus_fallback(self):
        for states in ((), ("ACTIVE", "DEFUNCT")):
            with self.subTest(states=states):
                unavailable = FakeNode("frame", states=states)
                unavailable.get_role_name = mock.Mock(side_effect=RuntimeError("native role unavailable"))
                target = FakeNode("frame", children=[FakeNode("entry", states=("FOCUSED",), text="Current question")])
                desktop = FakeNode("desktop", children=[FakeNode("application", children=[unavailable]),
                                                       FakeNode("application", children=[target])])
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(helper.extract(desktop, FakeAtspi), "Current question")

    def test_inactive_or_defunct_mirrors_preserve_normal_focus_fallback(self):
        for app_states, frame_states in (((), ()), (("ACTIVE", "DEFUNCT"), ("ACTIVE",)),
                                         ((), ("ACTIVE", "DEFUNCT"))):
            with self.subTest(app_states=app_states, frame_states=frame_states):
                target = FakeNode("frame", children=[FakeNode("entry", states=("FOCUSED",), text="Current field")])
                mirror = FakeNode("application", name="gnome-shell", states=app_states,
                                  children=[FakeNode("frame", states=frame_states)])
                desktop = FakeNode("desktop", children=[mirror, FakeNode("application", children=[target])])
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertIs(helper.select_foreground(desktop, FakeAtspi), target)
                    self.assertEqual(helper.extract(desktop, FakeAtspi), "Current field")

    def test_defunct_active_frame_and_application_are_ignored(self):
        gone = FakeNode("frame", states=("ACTIVE", "DEFUNCT"), text="stale")
        active = FakeNode("frame", states=("ACTIVE",), text="current")
        gone_app = FakeNode("application", states=("ACTIVE", "DEFUNCT"), children=[gone])
        desktop = FakeNode("desktop", children=[gone_app, FakeNode("application", children=[gone, active])])
        self.assertIs(helper.select_foreground(desktop, FakeAtspi), active)

    def test_wayland_focus_fallback_stays_inside_one_window(self):
        unrelated = FakeNode("frame", text="Background private text")
        focus = FakeNode("entry", text="Current field", states=("FOCUSED",))
        foreground = FakeNode("frame", children=[focus])
        app = FakeNode("application", children=[unrelated, foreground])
        self.assertIs(helper.select_foreground(FakeNode("desktop", children=[app]), FakeAtspi), foreground)

    def test_explicit_target_does_not_match_a_compositor_mirror(self):
        mirror = native_node("frame", "/window/1", states=("ACTIVE",))
        app = FakeNode("application", name="gnome-shell", children=[mirror])
        with mock.patch.dict(os.environ, {"SOCKET_STREAM_ATSPI_TARGET": helper.window_identity(mirror)}):
            with self.assertRaisesRegex(RuntimeError, "Selected question window is unavailable"):
                helper.select_foreground(FakeNode("desktop", children=[app]), FakeAtspi)

    def test_own_window_remains_excluded_when_gnome_mirror_is_active(self):
        own = FakeNode("frame", name="SocketStream — Practice Answer", states=("ACTIVE",))
        mirror = FakeNode("application", name="gnome-shell", children=[FakeNode("frame", states=("ACTIVE",))])
        desktop = FakeNode("desktop", children=[mirror, FakeNode("application", name="python3", children=[own])])
        with self.assertRaises(helper.OwnWindowError):
            helper.select_foreground(desktop, FakeAtspi)

    def test_own_active_window_blocks_stale_active_background_focus(self):
        for identification in ("title", "application", "pid"):
            for active_level in ("application", "frame"):
                for mode in ("text", "tree"):
                    with self.subTest(identification=identification, active_level=active_level, mode=mode):
                        own_frame = FakeNode("frame", name="SocketStream — Practice Answer" if identification == "title" else "Answer",
                                             states=("ACTIVE",) if active_level == "frame" else ())
                        own_app = FakeNode("application", name="SocketStream" if identification == "application" else "python3",
                                           pid=4242 if identification == "pid" else 10, children=[own_frame],
                                           states=("ACTIVE",) if active_level == "application" else ())
                        stale = FakeNode("entry", text="SYNTHETIC_BACKGROUND_PRIVATE", states=("FOCUSED",))
                        stale.get_text_iface = mock.Mock(wraps=stale.get_text_iface)
                        background = FakeNode("frame", states=("ACTIVE",), children=[stale])
                        desktop = FakeNode("desktop", children=[own_app, FakeNode("application", children=[background])])
                        with mock.patch.dict(os.environ, {"SOCKET_STREAM_GUI_PID": "4242"}, clear=True):
                            with self.assertRaises(helper.OwnWindowError):
                                if mode == "text":
                                    helper.extract(desktop, FakeAtspi)
                                else:
                                    helper.extract_tree(desktop, FakeAtspi, helper.parse_args([]))
                        stale.get_text_iface.assert_not_called()

    def test_own_active_window_preserves_explicit_other_target(self):
        own = FakeNode("frame", name="SocketStream — Practice Answer", states=("ACTIVE",))
        target = native_node("frame", "/window/selected", text="Selected question")
        desktop = FakeNode("desktop", children=[FakeNode("application", name="python3", children=[own]),
                                               FakeNode("application", children=[target])])
        with mock.patch.dict(os.environ, {"SOCKET_STREAM_ATSPI_TARGET": helper.window_identity(target)}, clear=True):
            self.assertIs(helper.select_foreground(desktop, FakeAtspi), target)
            self.assertEqual(helper.extract(desktop, FakeAtspi), "Selected question")

    def test_inactive_own_window_does_not_block_active_question(self):
        own = FakeNode("frame", name="SocketStream — Practice Answer")
        target = FakeNode("frame", states=("ACTIVE",), text="Current question")
        desktop = FakeNode("desktop", children=[FakeNode("application", name="python3", children=[own]),
                                               FakeNode("application", children=[target])])
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(helper.extract(desktop, FakeAtspi), "Current question")

    def test_active_application_with_one_window_does_not_require_widget_focus(self):
        frame = FakeNode("frame", text="Application content")
        app = FakeNode("application", states=("ACTIVE",), children=[frame])
        app.get_name = mock.Mock(side_effect=RuntimeError("no application label"))
        app.get_process_id = mock.Mock(side_effect=RuntimeError("no process metadata"))
        self.assertIs(helper.select_foreground(FakeNode("desktop", children=[app]), FakeAtspi), frame)


class WindowInventoryBoundaryTests(unittest.TestCase):
    def test_inventory_rejects_unaddressable_protected_defunct_and_own_windows(self):
        invalid_nodes = [
            FakeNode("frame", name="No native identity"),
            native_node("label", "/label/not_a_window"),
            native_node("frame", "/window/protected", states=("PROTECTED",)),
            native_node("frame", "/window/defunct", states=("DEFUNCT",)),
            native_node("frame", "/window/own", name="SocketStream — Practice"),
        ]
        visible = native_node("frame", "/window/visible")
        app = FakeNode("application", name="Fixture", children=[*invalid_nodes, visible])
        output = helper.list_windows(FakeNode("desktop", children=[app]), FakeAtspi)
        self.assertEqual(output, [{"id": helper.window_identity(visible), "title": "Untitled window", "application": "Fixture"}])
        for name, pid in (("SocketStream", None), ("python3", os.getpid()), ("python3", 54321)):
            with self.subTest(name=name, pid=pid), mock.patch.dict(os.environ, {"SOCKET_STREAM_GUI_PID": "54321"}):
                own = FakeNode("application", name=name, pid=pid, children=[visible])
                self.assertEqual(helper.list_windows(FakeNode("desktop", children=[own]), FakeAtspi), [])


class ProtectedAndUnavailableNodeTests(unittest.TestCase):
    def test_empty_native_role_is_unavailable_but_empty_accessible_name_is_valid(self):
        for role in (None, "", " "):
            with self.subTest(role=role):
                node = FakeNode(role, name="SYNTHETIC_UNREADABLE", text="SYNTHETIC_UNREADABLE")
                node.get_name = mock.Mock(wraps=node.get_name)
                node.get_text_iface = mock.Mock(wraps=node.get_text_iface)
                tree = helper.build_tree(node, FakeAtspi, 20, 5, 1000)
                self.assertTrue(tree["truncated"])
                self.assertEqual(tree["nodes"], [])
                node.get_name.assert_not_called()
                node.get_text_iface.assert_not_called()
        tree = helper.build_tree(FakeNode("paragraph", name="", text="Unlabelled text"), FakeAtspi, 20, 5, 1000)
        self.assertFalse(tree["truncated"])
        self.assertEqual(tree["nodes"][0]["text"], "Unlabelled text")

    def test_failed_role_or_name_omits_subtree_until_metadata_recovers(self):
        for method in ("get_role_name", "get_name"):
            with self.subTest(method=method):
                child = native_node("label", "/inaccessible/child", text="SYNTHETIC_UNREADABLE_CHILD")
                unavailable = native_node("panel", "/inaccessible", name="Retained label", children=[child])
                sibling = native_node("paragraph", "/valid", text="Valid sibling")
                frame = native_node("frame", "/window", name="Selected window", children=[unavailable, sibling])
                original_read = getattr(unavailable, method)
                child.get_text_iface = mock.Mock(wraps=child.get_text_iface)
                unavailable.get_text_iface = mock.Mock(wraps=unavailable.get_text_iface)
                unavailable.get_child_count = mock.Mock(wraps=unavailable.get_child_count)
                setattr(unavailable, method, mock.Mock(side_effect=RuntimeError("native metadata unavailable")))
                partial = helper.build_tree(frame, FakeAtspi, 20, 5, 1000)
                self.assertTrue(partial["truncated"])
                self.assertEqual([node["stable_id"] for node in partial["nodes"]],
                                 [helper.window_identity(frame), helper.window_identity(sibling)])
                self.assertEqual([node["parent"] for node in partial["nodes"]], [None, 1])
                self.assertNotIn("SYNTHETIC_UNREADABLE_CHILD", json.dumps(partial))
                unavailable.get_text_iface.assert_not_called()
                unavailable.get_child_count.assert_not_called()
                child.get_text_iface.assert_not_called()

                setattr(unavailable, method, original_read)
                complete = helper.build_tree(frame, FakeAtspi, 20, 5, 1000)
                self.assertFalse(complete["truncated"])
                self.assertEqual([node["parent"] for node in complete["nodes"]], [None, 1, 2, 1])
                self.assertEqual(complete["nodes"][1]["stable_id"], helper.window_identity(unavailable))
                self.assertEqual(complete["nodes"][1]["name"], "Retained label")
                self.assertEqual(complete["nodes"][2]["text"], "SYNTHETIC_UNREADABLE_CHILD")

    def test_failed_root_metadata_is_not_read_again_for_application_label(self):
        for method in ("get_role_name", "get_name"):
            with self.subTest(method=method):
                root = native_node("frame", "/window", name="SYNTHETIC_UNREADABLE_ROOT",
                                   children=[FakeNode("paragraph", text="SYNTHETIC_UNREADABLE_CHILD")])
                original_read = getattr(root, method)
                failed_read = mock.Mock(side_effect=[RuntimeError("metadata unavailable"), original_read()])
                setattr(root, method, failed_read)
                tree = helper.build_tree(root, FakeAtspi, 20, 5, 1000)
                self.assertTrue(tree["truncated"])
                self.assertEqual(tree["nodes"], [])
                self.assertEqual(tree["app"], "")
                self.assertNotIn("SYNTHETIC_UNREADABLE", json.dumps(tree))
                failed_read.assert_called_once_with()

    def test_failed_protected_role_is_opaque_without_a_protected_state_enum(self):
        native_states = types.SimpleNamespace(FOCUSED="FOCUSED", ACTIVE="ACTIVE", DEFUNCT="DEFUNCT")
        native_atspi = types.SimpleNamespace(StateType=native_states, CoordType=FakeAtspi.CoordType)
        protected = FakeNode("password text", name="SYNTHETIC_SECRET_NAME",
                             text="SYNTHETIC_SECRET_TEXT", children=[FakeNode("entry", text="SYNTHETIC_SECRET_CHILD")])
        protected.get_role_name = mock.Mock(side_effect=RuntimeError("native role unavailable"))
        protected.get_name = mock.Mock(wraps=protected.get_name)
        protected.get_text_iface = mock.Mock(wraps=protected.get_text_iface)
        protected.get_child_count = mock.Mock(wraps=protected.get_child_count)
        tree = helper.build_tree(protected, native_atspi, 20, 5, 1000)
        self.assertTrue(tree["truncated"])
        self.assertEqual(tree["nodes"], [])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(tree))
        self.assertIsNone(helper.focused_descendant(protected, native_atspi))
        protected.get_name.assert_not_called()
        protected.get_text_iface.assert_not_called()
        protected.get_child_count.assert_not_called()

    def test_legacy_metadata_failure_does_not_read_descendants(self):
        for method in ("get_role_name", "get_name"):
            with self.subTest(method=method):
                child = FakeNode("entry", text="SYNTHETIC_UNREADABLE_CHILD")
                unreadable = FakeNode("panel", children=[child])
                setattr(unreadable, method, mock.Mock(side_effect=RuntimeError("native metadata unavailable")))
                unreadable.get_text_iface = mock.Mock(wraps=unreadable.get_text_iface)
                child.get_text_iface = mock.Mock(wraps=child.get_text_iface)
                frame = FakeNode("frame", states=("ACTIVE",), children=[unreadable, FakeNode("label", text="Valid sibling")])
                desktop = FakeNode("desktop", children=[FakeNode("application", children=[frame])])
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(helper.extract(desktop, FakeAtspi), "Valid sibling")
                unreadable.get_text_iface.assert_not_called()
                child.get_text_iface.assert_not_called()

    def test_protected_subtree_is_an_opaque_leaf_in_tree_and_text_modes(self):
        child = FakeNode("entry", text="must never be read", states=("FOCUSED",))
        protected = FakeNode("password text", name="mirrored secret", children=[child])
        protected.get_name = lambda: self.fail("protected name was read")
        protected.get_text_iface = lambda: self.fail("protected text was read")
        protected.get_child_count = lambda: self.fail("protected descendants were read")
        tree = helper.build_tree(protected, FakeAtspi, 20, 5, 1000)
        self.assertEqual(tree["node_count"], 1)
        self.assertEqual((tree["app"], tree["nodes"][0]["name"], tree["nodes"][0]["text"]), ("", "", ""))
        self.assertIsNone(helper.focused_descendant(protected, FakeAtspi))
        frame = FakeNode("frame", states=("ACTIVE",), children=[protected])
        self.assertEqual(helper.extract(FakeNode("desktop", children=[frame]), FakeAtspi), "")

    def test_defunct_subtree_is_not_read(self):
        defunct = FakeNode("panel", states=("DEFUNCT",), children=[FakeNode("entry", text="stale")])
        defunct.get_text_iface = lambda: self.fail("defunct text was read")
        frame = FakeNode("frame", states=("ACTIVE",), children=[defunct])
        self.assertEqual(helper.build_tree(frame, FakeAtspi, 20, 5, 1000)["node_count"], 1)
        self.assertIsNone(helper.focused_descendant(defunct, FakeAtspi))

    def test_gi_state_error_does_not_abort_other_accessible_widgets(self):
        node = FakeNode("label", text="Readable")
        node.get_state_set = mock.Mock(side_effect=Exception("org.a11y.Error.Defunct"))
        self.assertFalse(helper.has_state(node, "ACTIVE", FakeAtspi))
        frame = FakeNode("frame", states=("ACTIVE",), children=[node])
        self.assertEqual(helper.build_tree(frame, FakeAtspi, 20, 5, 1000)["nodes"][1]["text"], "Readable")

    def test_transient_child_failure_preserves_other_children_and_marks_partial(self):
        child = FakeNode("label", text="Surviving child")
        frame = FakeNode("frame", children=[None, child, None])
        frame.get_child_at_index = mock.Mock(side_effect=[RuntimeError("closed child"), child, None])
        tree = helper.build_tree(frame, FakeAtspi, 20, 5, 1000)
        self.assertTrue(tree["truncated"])
        self.assertEqual(tree["nodes"][1]["text"], "Surviving child")
        frame.get_child_count = mock.Mock(side_effect=RuntimeError("disconnected application"))
        self.assertTrue(helper.build_tree(frame, FakeAtspi, 20, 5, 1000)["truncated"])
        self.assertEqual(list(helper.children(frame)), [])

    def test_children_tolerate_missing_broken_and_null_proxies(self):
        child = FakeNode("label", name="Visible")
        parent = FakeNode("panel", children=[None, None, child])
        parent.get_child_at_index = mock.Mock(side_effect=[RuntimeError("gone"), None, child])
        self.assertEqual(list(helper.children(parent)), [child])
        self.assertEqual(helper.build_tree(None, FakeAtspi, 10, 5, 1000)["nodes"], [])

    def test_read_errors_mark_partial_instead_of_claiming_complete_text(self):
        node = FakeNode("entry", text="Unavailable")
        for operation in ("get_text_iface",):
            with self.subTest(operation=operation):
                setattr(node, operation, mock.Mock(side_effect=RuntimeError("disconnected")))
                state = {"truncated": False}
                self.assertEqual(helper.safe_text(node, FakeAtspi, 100, state), "")
                self.assertTrue(state["truncated"])
        node.get_text_iface = lambda: types.SimpleNamespace(get_character_count=lambda: 20,
                                                           get_text=mock.Mock(side_effect=RuntimeError("closed")))
        state = {"truncated": False}
        self.assertEqual(helper.safe_text(node, FakeAtspi, 100, state), "")
        self.assertTrue(state["truncated"])
        self.assertEqual(helper.safe_text(node, FakeAtspi), "")
        node.get_text_iface = mock.Mock(side_effect=RuntimeError("disconnected"))
        self.assertEqual(helper.safe_text(node, FakeAtspi), "")

    def test_accessor_failures_return_bounded_empty_metadata(self):
        node = FakeNode("entry")
        for method in ("get_role_name", "get_name", "get_process_id", "get_extents"):
            setattr(node, method, mock.Mock(side_effect=RuntimeError("closed")))
        self.assertEqual(helper.safe_role(node), "")
        self.assertEqual(helper.safe_name(node), "")
        self.assertIsNone(helper.safe_pid(node))
        self.assertIsNone(helper.safe_extents(node, FakeAtspi))
        self.assertIsNone(helper.normalize_bounds((1,)))
        self.assertIsNone(helper.normalize_bounds((-2 ** 31, -2 ** 31, 300, 200)))

    def test_budget_and_focus_limits_are_enforced_on_nested_tree(self):
        leaf = FakeNode("entry", states=("FOCUSED",))
        branch = FakeNode("panel", children=[leaf])
        self.assertIsNone(helper.focused_descendant(branch, FakeAtspi, max_depth=0))
        self.assertIsNone(helper.focused_descendant(branch, FakeAtspi, max_nodes=0))
        self.assertIsNone(helper.focused_descendant(None, FakeAtspi))
        self.assertTrue(helper.build_tree(branch, FakeAtspi, 0, 5, 1000)["truncated"])

    def test_combined_name_and_text_budget_preserves_preorder_prefix(self):
        node = FakeNode("entry", name="Label", text="0123456789")
        tree = helper.build_tree(node, FakeAtspi, 10, 5, 8)
        self.assertEqual((tree["nodes"][0]["name"], tree["nodes"][0]["text"]), ("Label", "012"))
        self.assertTrue(tree["truncated"])

    def test_legacy_read_continues_after_disconnected_node_and_honours_budget(self):
        broken = FakeNode("entry", name=None)
        broken.get_text_iface = mock.Mock(side_effect=RuntimeError("disconnected"))
        empty = FakeNode("entry", text="")
        valid = FakeNode("paragraph", text="1234567890")
        frame = FakeNode("frame", states=("ACTIVE",), children=[broken, empty, valid, valid])
        desktop = FakeNode("desktop", children=[FakeNode("application", children=[frame])])
        with mock.patch.object(helper, "MAX_CHARS", 5):
            self.assertEqual(helper.extract(desktop, FakeAtspi), "12345")

    def test_focus_ancestry_tolerates_an_omitted_parent(self):
        nodes = [{"id": 2, "parent": 99, "focused": True}]
        helper.mark_focused_ancestors(nodes)
        self.assertTrue(nodes[0]["focused"])


class GiInterfaceDispatchTests(unittest.TestCase):
    def test_text_methods_dispatch_through_the_text_interface(self):
        iface = types.SimpleNamespace(get_text=lambda: self.fail("Accessible.get_text collision"),
                                      get_character_count=lambda: self.fail("wrong character-count dispatch"))
        node = FakeNode("entry")
        node.get_text_iface = lambda: iface
        atspi = types.SimpleNamespace(Text=types.SimpleNamespace(
            get_text=mock.Mock(return_value="Native browser text"),
            get_character_count=mock.Mock(return_value=19)))
        self.assertEqual(helper.safe_text(node, atspi), "Native browser text")
        atspi.Text.get_text.assert_called_once_with(iface, 0, 19)

    def test_component_methods_dispatch_through_component_interface(self):
        iface = types.SimpleNamespace(get_extents=lambda *_: self.fail("wrong Component dispatch"))
        node = FakeNode("image")
        node.get_component_iface = lambda: iface
        atspi = types.SimpleNamespace(CoordType=FakeAtspi.CoordType, Component=types.SimpleNamespace(
            get_extents=mock.Mock(return_value=(10, 20, 30, 40))))
        self.assertEqual(helper.safe_extents(node, atspi), (10, 20, 30, 40))
        atspi.Component.get_extents.assert_called_once_with(iface, FakeAtspi.CoordType.SCREEN)
        node.get_component_iface = lambda: None
        self.assertIsNone(helper.safe_extents(node, atspi))


class RuntimeInitializationTests(unittest.TestCase):
    def run_helper(self, argv, desktop=None, init=0, read_error=None):
        gi = types.ModuleType("gi")
        gi.require_version = mock.Mock()
        repository = types.ModuleType("gi.repository")
        atspi = types.SimpleNamespace(
            StateType=FakeAtspi.StateType, CoordType=FakeAtspi.CoordType,
            init=mock.Mock(return_value=init), set_timeout=mock.Mock(),
            get_desktop=mock.Mock(return_value=desktop, side_effect=read_error))
        repository.Atspi = atspi
        gi.repository = repository
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}), \
                mock.patch.object(helper.signal, "signal", return_value="old-handler") as signal_handler, \
                mock.patch.object(helper.signal, "setitimer") as timer, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = helper.main(argv)
        self.assertEqual(timer.call_args_list[-1], mock.call(helper.signal.ITIMER_REAL, 0))
        self.assertEqual(signal_handler.call_args_list[-1], mock.call(helper.signal.SIGALRM, "old-handler"))
        return code, stdout.getvalue(), stderr.getvalue(), atspi, signal_handler

    def test_own_active_role_failure_keeps_exit_four_without_reading_node_name(self):
        for identification in ("pid", "application"):
            for mode in ("text", "tree"):
                with self.subTest(identification=identification, mode=mode):
                    own = FakeNode("frame", name="SYNTHETIC_UNREADABLE_OWN_NAME", states=("ACTIVE",))
                    own.get_role_name = mock.Mock(side_effect=RuntimeError("native role unavailable"))
                    own.get_name = mock.Mock(wraps=own.get_name)
                    own.get_text_iface = mock.Mock(wraps=own.get_text_iface)
                    app = FakeNode("application", name="SocketStream" if identification == "application" else "python3",
                                   pid=4242 if identification == "pid" else 10, children=[own])
                    stale = FakeNode("entry", text="SYNTHETIC_BACKGROUND_PRIVATE", states=("FOCUSED",))
                    stale.get_text_iface = mock.Mock(wraps=stale.get_text_iface)
                    desktop = FakeNode("desktop", children=[app, FakeNode("application", children=[
                        FakeNode("frame", states=("ACTIVE",), children=[stale])])])
                    with mock.patch.dict(os.environ, {"SOCKET_STREAM_GUI_PID": "4242"}, clear=True):
                        code, output, error, _, _ = self.run_helper(["--mode", mode], desktop)
                    self.assertEqual((code, output, error), (4, "", ""))
                    own.get_name.assert_not_called()
                    own.get_text_iface.assert_not_called()
                    stale.get_text_iface.assert_not_called()

    def test_known_own_active_keeps_exit_four_before_unrelated_role_failure(self):
        own = FakeNode("frame", name="SocketStream — Practice Answer", states=("ACTIVE",))
        unavailable = FakeNode("frame", states=("ACTIVE",))
        unavailable.get_role_name = mock.Mock(side_effect=RuntimeError("native role unavailable"))
        unavailable.get_name = mock.Mock(wraps=unavailable.get_name)
        desktop = FakeNode("desktop", children=[FakeNode("application", children=[own]),
                                               FakeNode("application", children=[unavailable])])
        with mock.patch.dict(os.environ, {}, clear=True):
            code, output, error, _, _ = self.run_helper([], desktop)
        self.assertEqual((code, output, error), (4, "", ""))
        unavailable.get_name.assert_not_called()

    def test_unreadable_own_active_role_preserves_explicit_different_target(self):
        own = FakeNode("frame", states=("ACTIVE",))
        own.get_role_name = mock.Mock(side_effect=RuntimeError("native role unavailable"))
        own.get_name = mock.Mock(wraps=own.get_name)
        target = native_node("frame", "/window/selected", children=[FakeNode("paragraph", text="Pinned question")])
        desktop = FakeNode("desktop", children=[FakeNode("application", name="python3", pid=4242, children=[own]),
                                               FakeNode("application", children=[target])])
        with mock.patch.dict(os.environ, {"SOCKET_STREAM_GUI_PID": "4242",
                                         "SOCKET_STREAM_ATSPI_TARGET": helper.window_identity(target)}, clear=True):
            code, output, error, _, _ = self.run_helper(["--mode", "text"], desktop)
        self.assertEqual((code, output, error), (0, "Pinned question\n", ""))
        own.get_name.assert_not_called()

    def test_init_success_and_already_initialized_are_accepted(self):
        for status in (0, 1):
            code, output, error, atspi, _ = self.run_helper([], FakeNode("desktop"), init=status)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(json.loads(output)["nodes"], [])
            atspi.set_timeout.assert_called_once_with(800, 1500)

    def test_failed_bus_initialization_never_attempts_a_desktop_read(self):
        code, output, error, atspi, _ = self.run_helper([], init=2)
        self.assertEqual((code, output), (3, ""))
        self.assertIn("D-Bus session", error)
        atspi.get_desktop.assert_not_called()

    def test_missing_desktop_is_a_backend_error_not_a_successful_empty_read(self):
        code, output, error, _, _ = self.run_helper([])
        self.assertEqual((code, output), (3, ""))
        self.assertIn("AT-SPI desktop is unavailable", error)

    def test_deadline_cannot_be_swallowed_by_best_effort_node_read(self):
        node = FakeNode("entry")
        node.get_text_iface = mock.Mock(side_effect=helper.ReadDeadline("read timed out"))
        with self.assertRaises(helper.ReadDeadline):
            helper.safe_text(node, FakeAtspi)
        code, output, error, _, handler = self.run_helper([], read_error=helper.ReadDeadline("read timed out"))
        self.assertEqual((code, output), (3, ""))
        self.assertIn("timed out", error)
        installed = handler.call_args_list[0].args[1]
        with self.assertRaises(helper.ReadDeadline):
            installed(None, None)

    def test_text_and_inventory_modes_keep_their_wire_contracts(self):
        frame = native_node("frame", "/window/1", name="Native fixture", states=("ACTIVE",),
                            children=[FakeNode("paragraph", text="Visible text")])
        desktop = FakeNode("desktop", children=[FakeNode("application", children=[frame])])
        code, output, _, _, _ = self.run_helper(["--mode", "text"], desktop)
        self.assertEqual((code, output), (0, "Visible text\n"))
        code, output, _, _, _ = self.run_helper(["--mode", "windows"], desktop)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)[0]["id"], helper.window_identity(frame))


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
