"""Exercise the Linux package scripts without apt, D-Bus, or a desktop."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
import venv


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix", "The dependency scripts require a POSIX shell")
class LinuxDependencyScripts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="floaty-dependency-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.modules = self.root / "modules"
        self.modules.mkdir()
        self.log = self.root / "commands.log"
        self.bridge_log = self.root / "bridge.log"
        self.ui_bridge_log = self.root / "ui-bridge.json"
        self.probe_log = self.root / "probes.log"
        self.environment = {
            "PATH": str(self.bin) + os.pathsep + os.defpath,
            "PYTHONPATH": str(self.modules),
            "PYTHONDONTWRITEBYTECODE": "1",
            "FLOATY_TEST_COMMAND_LOG": str(self.log),
            "FLOATY_TEST_BRIDGE_LOG": str(self.bridge_log),
            "FLOATY_TEST_UI_BRIDGE_LOG": str(self.ui_bridge_log),
            "FLOATY_TEST_PROBE_LOG": str(self.probe_log),
            "LC_ALL": "C",
        }
        for name in ("tesseract", "ffmpeg", "pactl", "gst-inspect-1.0",
                     "whisper-cli", "ollama", "gdbus"):
            self.command(name, "exit 0\n")
        self.command("python3", "exec " + shlex.quote(sys.executable) + ' "$@"\n')
        self.command("ffmpeg", 'printf "ffmpeg %s\\n" "$*" >> "$FLOATY_TEST_PROBE_LOG"\n'
                     'test "$*" = "-hide_banner -devices" || exit 99\n'
                     'test "${FLOATY_TEST_FFMPEG_PULSE:-}" = missing || printf " D  pulse  PulseAudio input\\n"\n')
        self.command("pactl", 'printf "pactl %s\\n" "$*" >> "$FLOATY_TEST_PROBE_LOG"\n'
                     'test "$*" = info || exit 99\n'
                     'test "${FLOATY_TEST_AUDIO_SERVER:-}" != missing\n')
        self.command("tesseract", 'printf "tesseract %s\\n" "$*" >> "$FLOATY_TEST_PROBE_LOG"\n'
                     'test "$*" = --list-langs || exit 99\n'
                     'printf "List of available languages:\\nosd\\n"\n'
                     'test "${FLOATY_TEST_ENGLISH:-}" = missing || printf "eng\\n"\n')
        self.command("timeout", 'test "$1" = --kill-after=1s || exit 99\n'
                     'case "$2" in 5s|20s) ;; *) exit 99;; esac\n'
                     'case "$3" in */socket_stream_moonshine) probe=moonshine;; *) probe=$3;; esac\n'
                     'test "${FLOATY_TEST_TIMEOUT:-}" != "$probe" || exit 124\n'
                     'shift 2\nexec "$@"\n')
        self.command("apt-get", 'printf "apt-get %s\\n" "$*" >> "$FLOATY_TEST_COMMAND_LOG"\n')
        self.command("id", 'printf "%s\\n" "${FLOATY_TEST_UID:-0}"\n')
        self.command("sudo", 'printf "sudo %s\\n" "$*" >> "$FLOATY_TEST_COMMAND_LOG"\nexec "$@"\n')
        (self.modules / "dbus.py").write_text(
            'import os\nif os.environ.get("FLOATY_TEST_NATIVE_GI") == "dbus":\n'
            '    raise ImportError("SYNTHETIC_PRIVATE_NATIVE_DETAIL")\n'
        )
        (self.modules / "cairo.py").write_text(
            'import os\nif os.environ.get("FLOATY_TEST_PYCAIRO") == "missing":\n'
            '    raise ImportError("SYNTHETIC_PRIVATE_IMPORT_DETAIL")\n'
        )
        package = self.modules / "gi"
        package.mkdir()
        (package / "__init__.py").write_text(
            'import os\nfrom pathlib import Path\n'
            'def require_version(namespace, version):\n'
            '    assert (namespace, version) in (("Gtk", "3.0"), ("Atspi", "2.0"), ("Gst", "1.0"))\n'
            '    if os.environ.get("FLOATY_TEST_NATIVE_GI") == namespace:\n'
            '        raise ValueError("SYNTHETIC_PRIVATE_NATIVE_DETAIL")\n'
            'def require_foreign(namespace):\n'
            '    assert namespace == "cairo"\n'
            '    Path(os.environ["FLOATY_TEST_BRIDGE_LOG"]).write_text(namespace)\n'
            '    if os.environ.get("FLOATY_TEST_CAIRO_BRIDGE") == "missing":\n'
            '        raise ImportError("SYNTHETIC_PRIVATE_IMPORT_DETAIL")\n'
        )
        (package / "repository.py").write_text("Gtk = Atspi = Gst = object()\n")
        self.package_root = self.root / "release"
        self.package_root.mkdir()
        self.checker = self.package_root / "check-dependencies.sh"
        self.checker.write_bytes((ROOT / "scripts/linux-check-dependencies.sh").read_bytes())
        self.worker = self.package_root / "target/native-helpers/moonshine/socket_stream_moonshine/socket_stream_moonshine"
        self.worker.parent.mkdir(parents=True)
        self.worker.write_text('#!/bin/sh\nprintf "moonshine %s\\n" "$*" >> "$FLOATY_TEST_PROBE_LOG"\n'
                               'test "$*" = --check || exit 99\n'
                               'case "${FLOATY_TEST_SPEECH:-}" in\n'
                               '  error) printf \'{"type":"error","text":"SYNTHETIC_PRIVATE_MODEL_DETAIL"}\\n\'; exit 1;;\n'
                               '  empty) exit 0;;\n'
                               'esac\nprintf \'{"type":"ready","text":""}\\n\'\n')
        self.worker.chmod(0o700)

    def command(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o700)

    def ui_python(self):
        """A real private venv with synthetic GUI modules; no native imports."""
        runtime = self.root / "private UI runtime"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime)
        python = runtime / "bin/python3"
        package_root = Path(subprocess.check_output(
            [str(python), "-I", "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            env=self.environment, text=True, timeout=5).strip())
        package = package_root / "gi"
        package.mkdir()
        (package_root / "cairo.py").write_text("# Synthetic Pycairo; no native renderer.\n")
        (package / "__init__.py").write_text(
            'import json,os,sys\nfrom pathlib import Path\n'
            'def require_version(namespace, version):\n'
            '    assert (namespace, version) == ("Gtk", "3.0")\n'
            'def require_foreign(namespace):\n'
            '    assert namespace == "cairo"\n'
            '    Path(os.environ["FLOATY_TEST_UI_BRIDGE_LOG"]).write_text(json.dumps({\n'
            '        "prefix":sys.prefix,"executable":sys.executable,\n'
            '        "isolated":sys.flags.isolated,"no_bytecode":sys.dont_write_bytecode}))\n'
            '    if os.environ.get("FLOATY_TEST_UI_BRIDGE") == "missing":\n'
            '        raise ImportError("SYNTHETIC_PRIVATE_UI_DETAIL")\n'
        )
        (package / "repository.py").write_text("Gtk = object()\n")
        return python

    def run_script(self, name, *arguments):
        script = self.checker if name == "linux-check-dependencies.sh" else ROOT / "scripts" / name
        return subprocess.run(
            ["/bin/sh", str(script), *arguments],
            env=self.environment, text=True, capture_output=True, timeout=5,
            check=False,
        )

    @unittest.skipUnless(sys.platform.startswith("linux") and Path("/usr/bin/python3").is_file(),
                         "The checker deliberately uses distro Python on Linux")
    def test_checker_requires_the_foreign_bridge_even_when_pycairo_imports(self):
        result = self.run_script("linux-check-dependencies.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.bridge_log.is_file(), "The checker never verified the GI Cairo bridge")
        self.assertEqual(self.bridge_log.read_text(), "cairo")
        self.assertIn("[OK] Pycairo and GTK Cairo bridge", result.stdout)
        self.assertEqual(result.stderr, "")

    @unittest.skipUnless(sys.platform.startswith("linux") and Path("/usr/bin/python3").is_file(),
                         "The checker deliberately uses distro Python on Linux")
    def test_checker_rejects_missing_bridge_with_pycairo_present(self):
        self.environment["FLOATY_TEST_CAIRO_BRIDGE"] = "missing"
        result = self.run_script("linux-check-dependencies.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.bridge_log.read_text(), "cairo")
        self.assertIn("[MISSING] GTK Cairo bridge", result.stdout)
        self.assertIn("python3-gi-cairo", result.stdout)
        self.assertNotIn("[OK] Pycairo", result.stdout)
        self.assertNotIn("SYNTHETIC_PRIVATE_IMPORT_DETAIL", result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")

    @unittest.skipUnless(sys.platform.startswith("linux") and Path("/usr/bin/python3").is_file(),
                         "The checker deliberately uses distro Python on Linux")
    def test_checker_rejects_missing_pycairo_with_actionable_packages(self):
        self.environment["FLOATY_TEST_PYCAIRO"] = "missing"
        result = self.run_script("linux-check-dependencies.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(self.bridge_log.exists())
        self.assertIn("python3-cairo", result.stdout)
        self.assertIn("python3-gi-cairo", result.stdout)
        self.assertNotIn("[OK] Pycairo", result.stdout)
        self.assertNotIn("SYNTHETIC_PRIVATE_IMPORT_DETAIL", result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")

    def test_checker_explicit_private_ui_python_keeps_venv_and_isolation(self):
        python = self.ui_python()
        self.assertTrue(python.is_symlink(), "The fixture must exercise a venv interpreter symlink")
        # The default interpreter/PYTHONPATH cannot supply a GUI bridge. The
        # explicit venv has its own bridge, while native helpers stay separate.
        self.environment["FLOATY_TEST_CAIRO_BRIDGE"] = "missing"
        result = self.run_script("linux-check-dependencies.sh", "--ui-python=" + str(python))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observed = json.loads(self.ui_bridge_log.read_text())
        self.assertEqual(observed, {"prefix": str(python.parent.parent), "executable": str(python),
                                    "isolated": 1, "no_bytecode": True})
        self.assertFalse(self.bridge_log.exists(), "Native helpers do not need the GUI Cairo bridge")
        self.assertIn("UI Python: " + str(python), result.stdout)
        if Path("/usr/bin/python3").is_file():
            self.assertIn("Native helper Python: /usr/bin/python3", result.stdout)
        self.assertIn("[OK] DBus, AT-SPI2, GStreamer (native helper Python)", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_checker_explicit_ui_bridge_failure_is_not_hidden_by_system_python(self):
        python = self.ui_python()
        self.environment["FLOATY_TEST_UI_BRIDGE"] = "missing"
        result = self.run_script("linux-check-dependencies.sh", "--ui-python=" + str(python))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(self.ui_bridge_log.is_file())
        self.assertFalse(self.bridge_log.exists())
        self.assertIn("[MISSING] GTK Cairo bridge", result.stdout)
        self.assertIn("[OK] DBus, AT-SPI2, GStreamer (native helper Python)", result.stdout)
        self.assertNotIn("SYNTHETIC_PRIVATE_UI_DETAIL", result.stdout + result.stderr)

    def test_checker_explicit_ui_does_not_hide_native_helper_import_failures(self):
        python = self.ui_python()
        for namespace in ("dbus", "Atspi", "Gst"):
            with self.subTest(namespace=namespace):
                self.environment["FLOATY_TEST_NATIVE_GI"] = namespace
                result = self.run_script("linux-check-dependencies.sh", "--ui-python=" + str(python))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("[OK] Pycairo and GTK Cairo bridge", result.stdout)
                self.assertIn("[MISSING] Native helper Python", result.stdout)
                self.assertNotIn("SYNTHETIC_PRIVATE_NATIVE_DETAIL", result.stdout + result.stderr)
                self.assertEqual(result.stderr, "")

    def test_checker_validates_ui_python_arguments_before_any_probe(self):
        nonexecutable = self.root / "not-executable"
        nonexecutable.write_text("# No executable permission.\n")
        arguments = (("--unknown",), ("--ui-python",), ("--ui-python=",),
                     ("--ui-python=relative/python3",), ("--ui-python=" + str(self.root / "missing"),),
                     ("--ui-python=" + str(nonexecutable),), ("--ui-python=" + str(self.root),),
                     ("--ui-python=" + str(Path(sys.executable).absolute()),
                      "--ui-python=" + str(self.bin / "python3")))
        for values in arguments:
            with self.subTest(arguments=values):
                result = self.run_script("linux-check-dependencies.sh", *values)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse(self.bridge_log.exists())
                self.assertFalse(self.probe_log.exists())

    def test_checker_help_is_read_only_and_lists_explicit_ui_python(self):
        result = self.run_script("linux-check-dependencies.sh", "--help")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--ui-python", result.stdout)
        self.assertFalse(self.bridge_log.exists())
        self.assertFalse(self.probe_log.exists())

    def test_checker_rejects_timed_out_explicit_ui_probe(self):
        python = self.ui_python()
        self.environment["FLOATY_TEST_TIMEOUT"] = str(python)
        result = self.run_script("linux-check-dependencies.sh", "--ui-python=" + str(python))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(self.ui_bridge_log.exists())
        self.assertIn("[MISSING] UI Python", result.stdout)
        self.assertIn("[OK] DBus, AT-SPI2, GStreamer (native helper Python)", result.stdout)

    def test_installer_without_explicit_yes_never_calls_apt(self):
        result = self.run_script("linux-install-deps-debian.sh")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--yes", result.stdout)
        self.assertFalse(self.log.exists())

    def test_installer_requests_pycairo_and_the_separate_gi_bridge(self):
        result = self.run_script("linux-install-deps-debian.sh", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = self.log.read_text().splitlines()
        self.assertEqual(commands[0], "apt-get update")
        self.assertEqual(len(commands), 2)
        packages = commands[1].split()
        self.assertEqual(packages[:3], ["apt-get", "install", "-y"])
        for package in ("python3-cairo", "python3-gi-cairo", "python3-gi", "gir1.2-gtk-3.0"):
            self.assertEqual(packages.count(package), 1, package)

    def test_installer_preserves_explicit_sudo_path_for_non_root(self):
        self.environment["FLOATY_TEST_UID"] = "1000"
        result = self.run_script("linux-install-deps-debian.sh", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = self.log.read_text().splitlines()
        self.assertEqual(commands[0:2], ["sudo apt-get update", "apt-get update"])
        self.assertTrue(commands[2].startswith("sudo apt-get install -y "))
        self.assertEqual(commands[2].removeprefix("sudo "), commands[3])

    def test_installer_adds_source_speech_build_venv_without_selecting_an_audio_daemon(self):
        result = self.run_script("linux-install-deps-debian.sh", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        packages = self.log.read_text().splitlines()[-1].split()
        self.assertIn("python3-venv", packages)
        self.assertIn("ffmpeg", packages)
        self.assertIn("pulseaudio-utils", packages)
        self.assertNotIn("pulseaudio", packages)
        self.assertNotIn("pipewire-pulse", packages)
        self.assertNotIn("xdg-desktop-portal-gnome", packages)
        self.assertNotIn("xdg-desktop-portal-kde", packages)

    def test_installer_adds_only_the_explicit_desktop_portal_backend(self):
        for desktop, package in (("gnome", "xdg-desktop-portal-gnome"), ("kde", "xdg-desktop-portal-kde")):
            with self.subTest(desktop=desktop):
                self.log.unlink(missing_ok=True)
                result = self.run_script("linux-install-deps-debian.sh", "--yes", "--desktop=" + desktop)
                self.assertEqual(result.returncode, 0, result.stderr)
                packages = self.log.read_text().splitlines()[-1].split()
                self.assertEqual(packages.count(package), 1)
                other = "xdg-desktop-portal-kde" if desktop == "gnome" else "xdg-desktop-portal-gnome"
                self.assertNotIn(other, packages)

    def test_installer_rejects_unknown_or_conflicting_desktop_before_package_operations(self):
        for arguments in (("--desktop=unknown",), ("--desktop=gnome", "--desktop=kde")):
            with self.subTest(arguments=arguments):
                result = self.run_script("linux-install-deps-debian.sh", "--yes", *arguments)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.log.exists())

    def test_checker_rejects_ffmpeg_without_pulse_input(self):
        self.environment["FLOATY_TEST_FFMPEG_PULSE"] = "missing"
        result = self.run_script("linux-check-dependencies.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FFmpeg PulseAudio input", result.stdout)
        self.assertIn("[MISSING]", result.stdout)

    def test_checker_rejects_unreachable_audio_server_without_recording(self):
        self.environment["FLOATY_TEST_AUDIO_SERVER"] = "missing"
        result = self.run_script("linux-check-dependencies.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("PulseAudio-compatible audio server", result.stdout)
        self.assertIn("pactl info", self.probe_log.read_text())

    def test_checker_rejects_missing_english_ocr_data(self):
        self.environment["FLOATY_TEST_ENGLISH"] = "missing"
        result = self.run_script("linux-check-dependencies.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("tesseract-ocr-eng", result.stdout)

    def test_checker_checks_the_bundled_speech_engine_and_read_only_tool_capabilities(self):
        result = self.run_script("linux-check-dependencies.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(set(self.probe_log.read_text().splitlines()),
                         {"ffmpeg -hide_banner -devices", "pactl info", "tesseract --list-langs", "moonshine --check"})
        self.assertIn("[OK] bundled Moonshine", result.stdout)

    def test_checker_reports_missing_speech_bundle(self):
        self.worker.unlink()
        result = self.run_script("linux-check-dependencies.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Bundled Moonshine", result.stdout)
        self.assertNotIn("moonshine --check", self.probe_log.read_text())

    def test_checker_rejects_failed_or_empty_speech_readiness(self):
        for outcome in ("error", "empty"):
            with self.subTest(outcome=outcome):
                self.environment["FLOATY_TEST_SPEECH"] = outcome
                result = self.run_script("linux-check-dependencies.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("Moonshine", result.stdout)
                self.assertNotIn("SYNTHETIC_PRIVATE_MODEL_DETAIL", result.stdout + result.stderr)

    def test_checker_treats_audio_and_speech_probe_deadlines_as_failures(self):
        for probe in ("ffmpeg", "pactl", "moonshine"):
            with self.subTest(probe=probe):
                self.environment["FLOATY_TEST_TIMEOUT"] = probe
                result = self.run_script("linux-check-dependencies.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_checker_finds_the_speech_bundle_from_the_source_scripts_directory(self):
        source = self.root / "source"
        (source / "scripts").mkdir(parents=True)
        (source / "Cargo.toml").write_text("# Synthetic source-root marker\n")
        ui = source / "apps/socket_stream_desktop/ui/linux-gtk.py"
        ui.parent.mkdir(parents=True)
        ui.write_text("# Synthetic source-root marker\n")
        self.checker = source / "scripts/linux-check-dependencies.sh"
        self.checker.write_bytes((ROOT / "scripts/linux-check-dependencies.sh").read_bytes())
        worker = source / self.worker.relative_to(self.package_root)
        worker.parent.mkdir(parents=True)
        worker.write_bytes(self.worker.read_bytes())
        worker.chmod(0o700)
        for arguments in ((), ("--ui-python=" + str(self.ui_python()),)):
            with self.subTest(arguments=arguments):
                result = self.run_script("linux-check-dependencies.sh", *arguments)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("[OK] bundled Moonshine", result.stdout)


if __name__ == "__main__":
    unittest.main()
