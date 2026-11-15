"""Tests for compositor-managed capture exclusion.

The module must never touch a real compositor during tests: every test injects
a temporary home, a fake command runner, and a fake ``which``.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "apps" / "socket_stream_desktop" / "ui"
if str(UI) not in sys.path:
    sys.path.insert(0, str(UI))

import capture_rules as rules  # noqa: E402
import overlay_guard as guard  # noqa: E402


class FakeRunner:
    """Records argv and returns scripted results."""

    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = list(overrides or [])

    def __call__(self, argv, timeout=15, env=None):
        command = [str(part) for part in argv]
        self.calls.append((command, timeout, env))
        joined = " ".join(command)
        for needle, code, out, err in self.overrides:
            if needle in joined:
                return subprocess.CompletedProcess(command, code, out, err)
        return subprocess.CompletedProcess(command, 0, "", "")

    def called(self, needle):
        return any(needle in " ".join(call[0]) for call in self.calls)


def fake_which(*names):
    available = {f"/usr/bin/{name}" for name in names}
    return lambda name: f"/usr/bin/{name}" if f"/usr/bin/{name}" in available else None


def session(compositor, session_type=guard.SESSION_WAYLAND):
    return guard.SessionInfo(
        session_type,
        compositor,
        compositor,
        session_type == guard.SESSION_X11,
        session_type == guard.SESSION_WAYLAND,
    )


class Harness(unittest.TestCase):
    def make(self, compositor, config_home, *, runner=None, available=(),
             session_type=guard.SESSION_WAYLAND, runtime=None, proc_root=None, uid=0):
        runner = runner or FakeRunner()
        environ = {"XDG_CONFIG_HOME": str(config_home)}
        # Never read the real /opt manifest or the host process table in tests.
        environ["SOCKET_STREAM_MUTTER_RUNTIME"] = str(
            runtime if runtime is not None else Path(config_home) / "no-runtime.json")
        return rules.CaptureExclusion(
            session(compositor, session_type),
            environ=environ,
            home=config_home,
            runner=runner,
            which=fake_which(*available),
            proc_root=(
                Path(proc_root) if proc_root is not None
                else Path(config_home) / "proc"
            ),
            uid=uid,
        )


class NiriRules(Harness):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home / "niri" / "config.kdl"
        self.config.parent.mkdir(parents=True)
        self.original = (
            "// keep this comment\n"
            "input {\n"
            "    keyboard {\n"
            '        layout "us"\n'
            "    }\n"
            "}\n"
        )
        self.config.write_text(self.original, encoding="utf-8")

    def test_install_is_managed_and_idempotent(self):
        runner = FakeRunner()
        exclusion = self.make("niri", self.home, runner=runner, available=("niri",))
        before = exclusion.status()
        self.assertTrue(before.supported)
        self.assertFalse(before.configured)
        self.assertTrue(before.can_install)
        self.assertFalse(before.can_remove)

        after = exclusion.install()
        self.assertTrue(after.configured)
        self.assertTrue(after.config_validated)
        self.assertFalse(after.verified)
        self.assertTrue(after.can_remove)
        text = self.config.read_text(encoding="utf-8")
        self.assertIn(rules.MANAGED_BEGIN, text)
        self.assertIn(rules.MANAGED_END, text)
        self.assertIn('match app-id=r#"^socket_stream_overlay$"#', text)
        self.assertIn('block-out-from "screen-capture"', text)
        self.assertIn("// keep this comment", text)
        self.assertTrue(runner.called("niri validate"))
        self.assertTrue((self.config.parent / "config.kdl.socket_stream.bak").is_file())

        exclusion.install()
        self.assertEqual(self.config.read_text(encoding="utf-8").count(rules.MANAGED_BEGIN), 1)

        removed = exclusion.remove()
        self.assertFalse(removed.configured)
        text = self.config.read_text(encoding="utf-8")
        self.assertNotIn(rules.MANAGED_BEGIN, text)
        self.assertIn("// keep this comment", text)
        self.assertIn("input {", text)

    def test_validation_failure_restores_the_original_bytes(self):
        runner = FakeRunner([("niri validate", 1, "", "error: unexpected token")])
        exclusion = self.make("niri", self.home, runner=runner, available=("niri",))
        with self.assertRaises(rules.CaptureRuleError) as caught:
            exclusion.install()
        self.assertIn("rejected", str(caught.exception))
        self.assertEqual(self.config.read_text(encoding="utf-8"), self.original)
        self.assertTrue((self.config.parent / "config.kdl.socket_stream.bak").is_file())

    def test_missing_config_is_not_created(self):
        self.config.unlink()
        exclusion = self.make("niri", self.home, available=("niri",))
        status = exclusion.status()
        self.assertTrue(status.supported)
        self.assertFalse(status.can_install)
        self.assertIn("not found", status.detail)
        with self.assertRaises(rules.CaptureRuleError):
            exclusion.install()
        self.assertFalse(self.config.exists())

    def test_incomplete_managed_block_is_refused(self):
        self.config.write_text(
            self.original + f"\n// {rules.MANAGED_BEGIN}\nwindow-rule {{ }}\n",
            encoding="utf-8",
        )
        exclusion = self.make("niri", self.home, available=("niri",))
        status = exclusion.status()
        self.assertIn("incomplete", status.detail)
        self.assertFalse(status.can_install)
        with self.assertRaises(rules.CaptureRuleError):
            exclusion.install()

    def test_unreadable_config_does_not_crash_status(self):
        self.config.unlink()
        self.config.mkdir()
        exclusion = self.make("niri", self.home, available=("niri",))
        status = exclusion.status()
        self.assertIn("cannot read", status.detail)
        self.assertFalse(status.configured)


class KdeRules(Harness):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home / "kwinrulesrc"
        self.original = "[Other]\nfoo=bar\n"
        self.config.write_text(self.original, encoding="utf-8")

    def runner_for(self, version="6.7.0"):
        return FakeRunner([
            ("kwin_wayland --version", 0, f"kwin {version}\n", ""),
        ])

    def test_kwin_67_writes_and_registers_a_complete_rule(self):
        runner = self.runner_for()
        exclusion = self.make("kde", self.home, runner=runner, available=("kwin_wayland", "qdbus6"))
        before = exclusion.status()
        self.assertTrue(before.supported)
        self.assertTrue(before.can_install)

        after = exclusion.install()
        self.assertTrue(after.configured)
        self.assertTrue(after.can_remove)
        text = self.config.read_text(encoding="utf-8")
        self.assertIn("[socket_stream_capture]", text)
        self.assertIn("Description=SocketStream capture exclusion", text)
        self.assertIn("wmclass=socket_stream_overlay", text)
        self.assertIn("wmclassmatch=1", text)
        self.assertIn("excludefromcapture=true", text)
        self.assertIn("excludefromcapturerule=2", text)
        self.assertIn("rules=socket_stream_capture", text)
        self.assertIn("count=1", text)
        self.assertIn("[Other]", text)
        self.assertIn("foo=bar", text)
        self.assertTrue(runner.called("reconfigure"))
        self.assertTrue((self.home / "kwinrulesrc.socket_stream.bak").is_file())

        exclusion.install()
        self.assertEqual(
            self.config.read_text(encoding="utf-8").count("[socket_stream_capture]"), 1,
        )

        removed = exclusion.remove()
        self.assertFalse(removed.configured)
        text = self.config.read_text(encoding="utf-8")
        self.assertNotIn("[socket_stream_capture]", text)
        self.assertIn("[Other]", text)
        self.assertIn("foo=bar", text)

    def test_kwin_66_has_no_persistent_capture_rule(self):
        runner = self.runner_for("6.6.6")
        exclusion = self.make("kde", self.home, runner=runner, available=("kwin_wayland",))
        status = exclusion.status()
        self.assertFalse(status.supported)
        self.assertFalse(status.can_install)
        self.assertIn("6.7", status.guidance)
        with self.assertRaises(rules.CaptureRuleError):
            exclusion.install()

    def test_unknown_plasma_version_is_unsupported(self):
        runner = FakeRunner()
        exclusion = self.make("kde", self.home, runner=runner, available=("kwin_wayland",))
        status = exclusion.status()
        self.assertFalse(status.supported)
        self.assertIn("6.7", status.guidance)

    def test_rule_can_still_be_removed_after_a_downgrade(self):
        exclusion = self.make(
            "kde", self.home, runner=self.runner_for("6.7.0"),
            available=("kwin_wayland", "qdbus6"),
        )
        exclusion.install()
        downgraded = self.make(
            "kde", self.home, runner=self.runner_for("6.5.4"),
            available=("kwin_wayland",),
        )
        status = downgraded.status()
        self.assertFalse(status.supported)
        self.assertTrue(status.configured)
        self.assertTrue(status.can_remove)
        downgraded.remove()
        self.assertNotIn("[socket_stream_capture]", self.config.read_text(encoding="utf-8"))

    def test_rule_list_round_trip_preserves_order_and_escaped_ids(self):
        original = (
            "[General]\n# keep\nrules=alpha,with\\\\,comma,omega\ncount=3\nother=yes\n"
            "[alpha]\nDescription=User rule\n[with,comma]\nfoo=bar\n[omega]\nx=y\n"
        )
        self.config.write_text(original, encoding="utf-8")
        exclusion = self.make("kde", self.home, runner=self.runner_for(), available=("kwin_wayland",))
        exclusion.install()
        self.assertEqual(rules._kwin_rule_ids(self.config.read_text()),
                          [rules.KDE_GROUP, "alpha", "with,comma", "omega"])
        exclusion.install()
        exclusion.remove()
        text = self.config.read_text()
        self.assertEqual(rules._kwin_rule_ids(text), ["alpha", "with,comma", "omega"])
        self.assertIn("rules=alpha,with\\\\,comma,omega\n", text)
        self.assertIn("# keep", text)
        self.assertIn("other=yes", text)
        self.assertIn("[with,comma]\nfoo=bar", text)

    def test_legacy_numeric_rules_are_preserved(self):
        self.config.write_text("[General]\ncount=2\n[1]\nfoo=bar\n[2]\nx=y\n")
        exclusion = self.make("kde", self.home, runner=self.runner_for(), available=("kwin_wayland",))
        exclusion.install()
        self.assertEqual(rules._kwin_rule_ids(self.config.read_text()), [rules.KDE_GROUP, "1", "2"])
        exclusion.remove()
        self.assertEqual(rules._kwin_rule_ids(self.config.read_text()), ["1", "2"])

    def test_orphaned_old_rule_is_repairable_and_removable(self):
        exclusion = self.make("kde", self.home, runner=self.runner_for(), available=("kwin_wayland",))
        self.config.write_text(exclusion.backend._block())
        self.assertFalse(exclusion.status().configured)
        self.assertTrue(exclusion.status().can_install)
        self.assertTrue(exclusion.status().can_remove)
        exclusion.install()
        self.assertTrue(exclusion.status().configured)
        for old, new in (("wmclassmatch=1", "wmclassmatch=2"),
                         ("excludefromcapturerule=2", "excludefromcapturerule=0"),
                         ("Enabled=true", "Enabled=false")):
            with self.subTest(new=new):
                self.config.write_text(self.config.read_text().replace(old, new))
                self.assertFalse(exclusion.status().configured)
                self.assertTrue(exclusion.status().can_remove)
                exclusion.install()
                self.assertTrue(exclusion.status().configured)

    def test_plasmashell_version_is_not_compositor_capability(self):
        runner = FakeRunner([("plasmashell --version", 0, "plasmashell 6.9.0", "")])
        exclusion = self.make("kde", self.home, runner=runner, available=("plasmashell",))
        self.assertFalse(exclusion.status().supported)

    def test_kde_x11_cannot_install_wayland_rule(self):
        exclusion = self.make("kde", self.home, runner=self.runner_for(),
                              available=("kwin_wayland",), session_type=guard.SESSION_X11)
        self.assertFalse(exclusion.status().supported)
        with self.assertRaises(rules.CaptureRuleError):
            exclusion.install()
        self.assertEqual(self.config.read_text(), self.original)

    def test_invalid_rule_registry_does_not_write(self):
        original = "[General]\ncount=not-a-number\n[1]\nfoo=bar\n"
        self.config.write_text(original)
        exclusion = self.make("kde", self.home, runner=self.runner_for(), available=("kwin_wayland",))
        with self.assertRaises(rules.CaptureRuleError):
            exclusion.install()
        self.assertEqual(self.config.read_text(), original)

    def test_dbus_fallback_waits_for_reply(self):
        runner = self.runner_for()
        exclusion = self.make("kde", self.home, runner=runner,
                              available=("kwin_wayland", "dbus-send"))
        exclusion.install()
        call = next(argv for argv, _, _ in runner.calls if "dbus-send" in argv[0])
        self.assertIn("--print-reply", call)

    def test_removing_absent_rule_preserves_file_and_creates_no_backup(self):
        for original in ("[unrelated]\nx=y\n", "[General]\ncount=1\n[1]\nx=y\n"):
            with self.subTest(original=original):
                self.config.write_text(original)
                runner = self.runner_for()
                exclusion = self.make("kde", self.home, runner=runner,
                                      available=("kwin_wayland", "qdbus6"))
                before = sorted(self.config.parent.iterdir())
                exclusion.remove()
                self.assertEqual(self.config.read_text(), original)
                self.assertEqual(sorted(self.config.parent.iterdir()), before)
                self.assertFalse(any("reconfigure" in argv for argv, _, _ in runner.calls))

    def test_kconfig_extended_headers_are_refused_without_writing(self):
        exclusion = self.make("kde", self.home, runner=self.runner_for(), available=("kwin_wayland",))
        for header in ("[User][$i]", "[User][Nested]"):
            original = "[General]\nrules=socket_stream_capture,User\ncount=2\n" + exclusion.backend._block() + header + "\nkeep=true\n"
            for action in (exclusion.install, exclusion.remove):
                with self.subTest(header=header, action=action):
                    self.config.write_text(original)
                    before = sorted(self.config.parent.iterdir())
                    with self.assertRaises(rules.CaptureRuleError):
                        action()
                    self.assertEqual(self.config.read_text(), original)
                    self.assertEqual(sorted(self.config.parent.iterdir()), before)
                    self.assertFalse(exclusion.status().configured)

    def test_kconfig_group_names_are_case_sensitive(self):
        original = "[General]\nrules=SocketStream-Capture\ncount=1\n[SocketStream-Capture]\nkeep=true\n"
        self.config.write_text(original)
        exclusion = self.make("kde", self.home, runner=self.runner_for(), available=("kwin_wayland",))
        exclusion.remove()
        self.assertEqual(self.config.read_text(), original)
        exclusion.install()
        exclusion.remove()
        self.assertIn("[SocketStream-Capture]\nkeep=true", self.config.read_text())
        self.assertEqual(rules._kwin_rule_ids(self.config.read_text()), ["SocketStream-Capture"])

    def test_kwin_68_order_preserves_all_user_groups(self):
        for registry in ("Order=alpha,beta", ""):
            with self.subTest(registry=registry):
                original = (f"[General]\n{registry}\nother=yes\n"
                            "[alpha]\nDescription=A\n[beta]\nDescription=B\n"
                            "[unlisted]\nDescription=Unlisted active rule\n[$Version]\nx=y\n")
                self.config.write_text(original)
                exclusion = self.make("kde", self.home, runner=self.runner_for("6.8.0"),
                                      available=("kwin_wayland",))
                self.assertTrue(exclusion.install().configured)
                installed = self.config.read_text()
                general = rules._parse_ini(installed)["General"]
                self.assertNotIn("rules", general)
                self.assertNotIn("count", general)
                ids = rules._kwin_rule_ids(installed)
                self.assertEqual(ids[0], rules.KDE_GROUP)
                self.assertEqual(set(ids), {rules.KDE_GROUP, "alpha", "beta", "unlisted"})
                self.assertTrue(exclusion.status().configured)
                exclusion.remove()
                text = self.config.read_text()
                self.assertNotIn("[socket_stream_capture]", text)
                for group in ("[alpha]\nDescription=A", "[beta]\nDescription=B",
                              "[unlisted]\nDescription=Unlisted active rule", "[$Version]\nx=y"):
                    self.assertIn(group, text)
                self.assertEqual(rules._kwin_rule_ids(text), ids[1:])

    def test_order_file_is_preserved_when_compositor_version_is_unknown(self):
        exclusion = self.make("kde", self.home, runner=FakeRunner(), available=())
        original = "[General]\nOrder=socket_stream_capture,alpha\n[alpha]\nx=y\n"
        self.config.write_text(original + exclusion.backend._block())
        exclusion.remove()
        self.assertIn("Order=alpha", self.config.read_text())
        self.assertNotIn("rules=", self.config.read_text())

    def test_kconfig_modifiers_refuse_before_backup_write_or_reload(self):
        registry = "[General]\nrules=socket_stream_capture,alpha\ncount=2\n"
        runner = self.runner_for()
        exclusion = self.make("kde", self.home, runner=runner,
                              available=("kwin_wayland", "qdbus6"))
        groups = exclusion.backend._block() + "[alpha]\nDescription=Keep\n"
        fixtures = (registry + "rules[$d]\n" + groups,
                    registry + "rules[en_US]=alpha\n" + groups,
                    registry.replace("[General]", r"[Gen\x65ral]") + groups,
                    "[$i]\n" + registry + groups,
                    registry.replace("rules=socket_stream_capture,alpha", r"rules=socket_stream_capture,with\,comma") + groups)
        backup = self.home / "kwinrulesrc.socket_stream.bak"
        for original in fixtures:
            for with_backup in (False, True):
                for action in (exclusion.install, exclusion.remove):
                    with self.subTest(original=original, backup=with_backup, action=action):
                        if backup.exists():
                            backup.unlink()
                        if with_backup:
                            backup.write_bytes(b"Existing backup sentinel\n")
                        self.config.write_text(original)
                        self.config.chmod(0o640)
                        before = {p.name: p.read_bytes() for p in self.home.iterdir()}
                        with self.assertRaises(rules.CaptureRuleError):
                            action()
                        self.assertEqual({p.name: p.read_bytes() for p in self.home.iterdir()}, before)
                        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
                        status = exclusion.status()
                        self.assertFalse(status.configured)
                        self.assertFalse(status.can_install)
                        self.assertFalse(status.can_remove)
                        self.assertFalse(runner.called("reconfigure"))

    def test_modern_comma_id_keeps_authentic_kconfig_serialization(self):
        original = ("[General]\nOrder=alpha,with\\\\,comma,omega\n"
                    "[alpha]\nx=y\n[with,comma]\nx=y\n[omega]\nx=y\n")
        self.config.write_text(original)
        exclusion = self.make("kde", self.home, runner=self.runner_for("6.8.0"),
                              available=("kwin_wayland",))
        exclusion.install()
        self.assertIn("Order=socket_stream_capture,alpha,with\\\\,comma,omega\n", self.config.read_text())
        exclusion.remove()
        self.assertIn("Order=alpha,with\\\\,comma,omega\n", self.config.read_text())

    def test_legacy_migration_refuses_retired_groups_without_side_effects(self):
        runner = self.runner_for("6.8.0")
        exclusion = self.make("kde", self.home, runner=runner,
                              available=("kwin_wayland", "qdbus6"))
        original = ("[General]\nrules=socket_stream_capture,alpha\ncount=2\n"
                    "[alpha]\nEnabled=true\n[retired]\nEnabled=true\n" + exclusion.backend._block())
        backup = self.home / "kwinrulesrc.socket_stream.bak"
        backup.write_bytes(b"Existing backup sentinel\n")
        for action in (exclusion.install, exclusion.remove):
            self.config.write_text(original)
            self.config.chmod(0o640)
            before = {p.name: p.read_bytes() for p in self.home.iterdir()}
            with self.assertRaises(rules.CaptureRuleError):
                action()
            self.assertEqual({p.name: p.read_bytes() for p in self.home.iterdir()}, before)
            self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
            status = exclusion.status()
            self.assertFalse(status.configured or status.can_install or status.can_remove)
            self.assertFalse(runner.called("reconfigure"))

    def test_legacy_migration_without_retired_groups_preserves_precedence(self):
        self.config.write_text("[General]\nrules=beta,alpha\ncount=2\n[alpha]\nx=y\n[beta]\nx=y\n")
        exclusion = self.make("kde", self.home, runner=self.runner_for("6.8.0"), available=("kwin_wayland",))
        exclusion.install()
        self.assertIn("Order=socket_stream_capture,beta,alpha\n", self.config.read_text())
        exclusion.remove()
        self.assertIn("Order=beta,alpha\n", self.config.read_text())

    def test_kwin_68_legacy_managed_orphan_is_repairable_not_configured(self):
        exclusion = self.make("kde", self.home, runner=self.runner_for("6.8.0"), available=("kwin_wayland",))
        original = "[General]\nrules=alpha\ncount=1\n[alpha]\nEnabled=true\n" + exclusion.backend._block()
        self.config.write_text(original)
        status = exclusion.status()
        self.assertFalse(status.configured)
        self.assertTrue(status.can_install and status.can_remove)
        self.assertTrue(exclusion.install().configured)
        self.assertIn("Order=socket_stream_capture,alpha\n", self.config.read_text())
        self.config.write_text(original)
        exclusion.remove()
        self.assertNotIn("[socket_stream_capture]", self.config.read_text())
        self.assertIn("Order=alpha\n", self.config.read_text())
        self.config.write_text(original.replace("rules=alpha\ncount=1", "Order=alpha"))
        self.assertTrue(exclusion.status().configured)


class HyprlandRules(Harness):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.directory = self.home / "hypr"
        self.directory.mkdir(parents=True)

    def test_conf_rule_uses_the_documented_block_syntax(self):
        config = self.directory / "hyprland.conf"
        config.write_text("# user config\n", encoding="utf-8")
        runner = FakeRunner()
        exclusion = self.make(
            "hyprland", self.home, runner=runner, available=("Hyprland", "hyprctl"),
        )
        status = exclusion.install()
        self.assertTrue(status.configured)
        self.assertTrue(status.config_validated)
        self.assertFalse(status.verified)
        text = config.read_text(encoding="utf-8")
        self.assertIn("windowrule {", text)
        self.assertIn("name = socket_stream_capture-exclusion", text)
        self.assertIn("match:class = ^(socket_stream_overlay)$", text)
        self.assertIn("no_screen_share = true", text)
        self.assertIn("# user config", text)
        self.assertTrue(runner.called("--verify-config"))
        self.assertTrue(runner.called("hyprctl reload"))
        self.assertTrue(runner.called("configerrors"))

        exclusion.remove()
        text = config.read_text(encoding="utf-8")
        self.assertNotIn("no_screen_share", text)
        self.assertIn("# user config", text)

    def test_lua_rule_uses_hl_window_rule(self):
        config = self.directory / "hyprland.lua"
        config.write_text("-- user config\n", encoding="utf-8")
        exclusion = self.make(
            "hyprland", self.home, available=("Hyprland", "hyprctl"),
        )
        exclusion.install()
        text = config.read_text(encoding="utf-8")
        self.assertIn("hl.window_rule({", text)
        self.assertIn('match = { class = "^(socket_stream_overlay)$" }', text)
        self.assertIn("no_screen_share = true", text)

    def test_config_error_rolls_back_the_file(self):
        config = self.directory / "hyprland.conf"
        original = "# user config\n"
        config.write_text(original, encoding="utf-8")
        runner = FakeRunner([
            ("configerrors", 0, "error: no_screen_share: invalid rule", ""),
        ])
        exclusion = self.make(
            "hyprland", self.home, runner=runner, available=("Hyprland", "hyprctl"),
        )
        with self.assertRaises(rules.CaptureRuleError):
            exclusion.install()
        self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_missing_config_is_not_created(self):
        exclusion = self.make(
            "hyprland", self.home, available=("Hyprland", "hyprctl"),
        )
        status = exclusion.status()
        self.assertFalse(status.can_install)
        with self.assertRaises(rules.CaptureRuleError):
            exclusion.install()
        self.assertFalse((self.directory / "hyprland.conf").exists())


class UnsupportedSessions(Harness):
    def test_gnome_without_the_custom_runtime_is_unsupported(self):
        with tempfile.TemporaryDirectory() as temp:
            exclusion = self.make("gnome", Path(temp))
            status = exclusion.status()
            self.assertFalse(status.supported)
            self.assertFalse(status.deployed)
            self.assertFalse(status.configured)
            self.assertFalse(status.restart_required)
            self.assertFalse(status.verified)
            self.assertIn("not installed", status.detail)
            self.assertIn("single application window", status.guidance)
            self.assertFalse(status.can_install)
            with self.assertRaises(rules.CaptureRuleError):
                exclusion.install()

    def test_gnome_x11_points_at_the_wayland_requirement(self):
        with tempfile.TemporaryDirectory() as temp:
            exclusion = self.make("gnome", Path(temp), session_type=guard.SESSION_X11)
            status = exclusion.status()
            self.assertFalse(status.supported)
            self.assertIn("Wayland", status.guidance)
            self.assertFalse(status.deployed)

    def test_x11_reports_root_window_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            exclusion = self.make("x11", Path(temp), session_type=guard.SESSION_X11)
            status = exclusion.status()
            self.assertFalse(status.supported)
            self.assertIn("root window", status.guidance)

    def test_sway_reports_no_documented_rule(self):
        with tempfile.TemporaryDirectory() as temp:
            exclusion = self.make("sway", Path(temp))
            status = exclusion.status()
            self.assertFalse(status.supported)
            self.assertIn("no documented", status.detail)

    def test_unknown_compositor_is_honest(self):
        with tempfile.TemporaryDirectory() as temp:
            exclusion = self.make("unknown", Path(temp))
            status = exclusion.status()
            self.assertFalse(status.supported)
            self.assertIn("no documented", status.detail)


class Identity(unittest.TestCase):
    def test_panel_identity_is_stable_and_shared(self):
        self.assertEqual(rules.PANEL_APP_ID, "socket_stream_overlay")
        self.assertEqual(rules.PANEL_WM_CLASS, rules.PANEL_APP_ID)
        block = rules.NiriBackend(Path("/tmp/does-not-exist.kdl"), which=lambda _: None)._block()
        self.assertIn(rules.PANEL_APP_ID, block)


if __name__ == "__main__":
    unittest.main()
