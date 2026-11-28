"""Exercise the Linux package scripts without apt, D-Bus, or a desktop."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


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
        self.environment = {
            "PATH": str(self.bin) + os.pathsep + os.defpath,
            "PYTHONPATH": str(self.modules),
            "PYTHONDONTWRITEBYTECODE": "1",
            "FLOATY_TEST_COMMAND_LOG": str(self.log),
            "FLOATY_TEST_BRIDGE_LOG": str(self.bridge_log),
            "LC_ALL": "C",
        }
        for name in ("tesseract", "ffmpeg", "pactl", "gst-inspect-1.0",
                     "whisper-cli", "ollama"):
            self.command(name, "exit 0\n")
        self.command("apt-get", 'printf "apt-get %s\\n" "$*" >> "$FLOATY_TEST_COMMAND_LOG"\n')
        self.command("id", 'printf "%s\\n" "${FLOATY_TEST_UID:-0}"\n')
        self.command("sudo", 'printf "sudo %s\\n" "$*" >> "$FLOATY_TEST_COMMAND_LOG"\nexec "$@"\n')
        (self.modules / "dbus.py").write_text("# Synthetic import only; no bus is opened.\n")
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
            'def require_foreign(namespace):\n'
            '    assert namespace == "cairo"\n'
            '    Path(os.environ["FLOATY_TEST_BRIDGE_LOG"]).write_text(namespace)\n'
            '    if os.environ.get("FLOATY_TEST_CAIRO_BRIDGE") == "missing":\n'
            '        raise ImportError("SYNTHETIC_PRIVATE_IMPORT_DETAIL")\n'
        )
        (package / "repository.py").write_text("Gtk = Atspi = Gst = object()\n")

    def command(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o700)

    def run_script(self, name, *arguments):
        return subprocess.run(
            ["/bin/sh", str(ROOT / "scripts" / name), *arguments],
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


if __name__ == "__main__":
    unittest.main()
