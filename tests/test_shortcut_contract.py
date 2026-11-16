"""Source-tree parity checks; portable releases ship the runtime model tests.

Rust/Swift source is intentionally absent from a Linux application archive.
The packaged CLI/Python round trip is covered by acceptance tests instead.
"""
import re
import unittest
from pathlib import Path
from tests.test_shortcuts import SHORTCUTS

ROOT = Path(__file__).resolve().parents[1]


def _extract_rust_bindings(path):
    text = path.read_text(encoding="utf-8")
    start = text.index("pub const DEFAULT_BINDINGS")
    block = text[start:text.index("];", start)]
    return dict(re.findall(r'\("([a-z_]+)", "([a-z0-9+]+)"\)', block))


def _extract_swift_bindings(path):
    text = path.read_text(encoding="utf-8")
    start = text.index("let defaultShortcutBindings")
    start = text.index("= [", start)
    block = text[start:text.index("]", start)]
    return dict(re.findall(r'"([a-z_]+)": "([a-z0-9+]+)"', block))


class DefaultsParityTests(unittest.TestCase):
    def test_python_and_rust_share_all_seventeen_defaults_in_order(self):
        rust_config = _extract_rust_bindings(ROOT / "crates/socket_stream_core/src/config.rs")
        rust_shortcuts = _extract_rust_bindings(ROOT / "crates/socket_stream_core/src/shortcuts.rs")
        self.assertEqual(len(SHORTCUTS.DEFAULT_BINDINGS), 17)
        self.assertEqual(len(rust_config), 17)
        self.assertEqual(rust_config, dict(SHORTCUTS.DEFAULT_BINDINGS))
        self.assertEqual(rust_shortcuts, rust_config)
        self.assertEqual(list(SHORTCUTS.DEFAULT_BINDINGS), list(SHORTCUTS.ACTIONS))

    def test_swift_is_a_twelve_action_subset_without_arrows(self):
        swift = _extract_swift_bindings(ROOT / "apps/socket_stream_desktop/ui/mac-app.swift")
        self.assertEqual(len(swift), 12)
        self.assertTrue(set(swift).issubset(set(SHORTCUTS.DEFAULT_BINDINGS)))
        for action, accel in swift.items():
            self.assertEqual(accel, SHORTCUTS.DEFAULT_BINDINGS[action], action)
        linux_only = set(SHORTCUTS.DEFAULT_BINDINGS) - set(swift)
        self.assertEqual(linux_only, {"listen_audio", "pin_question",
                                      "previous_question", "next_question", "follow_latest"})
        self.assertNotIn("previous_question", swift)
        self.assertNotIn("next_question", swift)
        self.assertNotIn("ctrl+alt+left", swift.values())
        self.assertNotIn("ctrl+alt+right", swift.values())


if __name__ == "__main__":
    unittest.main()
