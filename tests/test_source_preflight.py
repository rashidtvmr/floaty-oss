import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/socket_stream_desktop/ui"))
from source_preflight import check_sources, _probe


class SourcePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        worker = self.root / "target/native-helpers/moonshine/socket_stream_moonshine/socket_stream_moonshine"
        worker.parent.mkdir(parents=True)
        worker.write_text("fixture")
        worker.chmod(0o700)
        self.calls = []

    def probe(self, args, **kwargs):
        self.calls.append(args)
        self.assertLessEqual(kwargs["timeout"], 3)
        if args[:3] == ["pactl", "list", "short"]:
            output = "1 mic0 fixture\n2 sink0.monitor fixture\n"
        elif args[-1] == "get-default-sink":
            output = "sink0\n"
        elif args[-1] == "get-default-source":
            output = "mic0\n"
        else:
            output = ""
        return SimpleNamespace(returncode=0, stdout=output)

    def test_selected_audio_checks_devices_without_capture(self):
        errors, notes = check_sources(True, False, "system", "default", self.root,
                                     run=self.probe, which=lambda _: "/fixture")
        self.assertEqual(errors, [])
        self.assertTrue(any("not a permission grant" in note for note in notes))
        self.assertTrue(all(args[0] == "pactl" for args in self.calls))

    def test_unselected_audio_dependencies_do_not_block_screen(self):
        errors, _ = check_sources(False, True, "microphone", "default", self.root,
                                  run=self.probe, which=lambda _: None)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("getDesktop", " ".join(self.calls[0]))

    def test_missing_device_and_timeout_are_actionable_without_recording(self):
        errors, _ = check_sources(True, False, "microphone", "missing", self.root,
                                  run=self.probe, which=lambda _: "/fixture")
        self.assertIn("Audio options", " ".join(errors))

        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("fixture", 3)

        errors, _ = check_sources(True, False, "microphone", "default", self.root,
                                  run=timeout, which=lambda _: "/fixture")
        self.assertIn("timed out", " ".join(errors))

    def test_no_sources_does_not_probe(self):
        errors, _ = check_sources(False, False, "microphone", "default", self.root,
                                  run=self.probe, which=lambda _: None)
        self.assertTrue(errors)
        self.assertEqual(self.calls, [])

    def test_real_probe_bounds_output_and_time_without_sensor_access(self):
        with self.assertRaises(OSError):
            _probe([sys.executable, "-c", "print('x' * 100000)"], timeout=2)
        with self.assertRaises(subprocess.TimeoutExpired):
            _probe([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)

    def test_real_probe_returns_bounded_process_output_and_exit_status(self):
        result = _probe([sys.executable, "-c", "import sys; print('ready'); sys.exit(7)"], timeout=2)
        self.assertEqual((result.returncode, result.stdout), (7, "ready\n"))

    def test_expired_probe_and_sigterm_resistant_probe_are_reaped(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            _probe([sys.executable, "-c", "pass"], timeout=0)
        with self.assertRaises(subprocess.TimeoutExpired):
            _probe([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "print('ready', flush=True); time.sleep(10)"], timeout=0.25)

    def test_selected_audio_dependency_and_service_failures_are_reported(self):
        errors, _ = check_sources(True, False, "microphone", "default", self.root,
                                  run=self.probe, which=lambda _: None)
        self.assertTrue(any("FFmpeg" in error for error in errors))
        worker = self.root / "target/native-helpers/moonshine/socket_stream_moonshine/socket_stream_moonshine"
        worker.unlink()

        def failed_service(args, **kwargs):
            result = self.probe(args, **kwargs)
            result.returncode = 1
            return result

        errors, _ = check_sources(True, False, "microphone", "default", self.root,
                                  run=failed_service, which=lambda _: "/fixture")
        self.assertTrue(any("audio server" in error for error in errors))
        self.assertTrue(any("Moonshine" in error for error in errors))

    def test_screen_checks_use_the_same_distro_python_as_the_capture_helper(self):
        with mock.patch("source_preflight.Path.is_file", return_value=True):
            errors, _ = check_sources(False, True, "microphone", "default", self.root,
                                      run=self.probe, which=lambda _: None)
        self.assertEqual(errors, [])
        self.assertEqual(self.calls[0][0], "/usr/bin/python3")
        with mock.patch("source_preflight.Path.is_file", return_value=False):
            check_sources(False, True, "microphone", "default", self.root,
                          run=self.probe, which=lambda _: None)
        self.assertEqual(self.calls[-1][0], sys.executable)

    def test_disabled_toolkit_status_explains_fix_without_changing_settings(self):
        def disabled(args, **kwargs):
            result = self.probe(args, **kwargs)
            if args[-1] == "IsEnabled":
                result.stdout = "(<false>,)\n"
            return result

        errors, notes = check_sources(False, True, "microphone", "default", self.root,
                                     run=disabled, which=lambda _: "/fixture")
        self.assertEqual(errors, [])
        self.assertTrue(any("toolkit-accessibility true" in note for note in notes))
        self.assertTrue(any(args[-1] == "IsEnabled" for args in self.calls))
        self.assertFalse(any("org.freedesktop.DBus.Properties.Set" in args for args in self.calls))
        self.assertFalse(any(args[0] in ("gsettings", "xprop", "xdotool") for args in self.calls))

    def test_enabled_native_wayland_bus_does_not_require_an_x11_display(self):
        def enabled(args, **kwargs):
            result = self.probe(args, **kwargs)
            if args[-1] == "IsEnabled":
                result.stdout = "(<true>,)\n"
            return result

        with mock.patch.dict("os.environ", {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}, clear=True):
            errors, notes = check_sources(False, True, "microphone", "default", self.root,
                                         run=enabled, which=lambda _: "/fixture")
        self.assertEqual(errors, [])
        self.assertFalse(any("disabled" in note for note in notes))

    def test_failed_status_query_is_not_mistaken_for_a_disabled_toolkit(self):
        def unavailable(args, **kwargs):
            result = self.probe(args, **kwargs)
            if args[-1] == "IsEnabled":
                result.returncode = 1
            return result

        errors, notes = check_sources(False, True, "microphone", "default", self.root,
                                     run=unavailable, which=lambda _: "/fixture")
        self.assertEqual(errors, [])
        self.assertTrue(any("could not be checked" in note for note in notes))

    def test_unreachable_bus_and_missing_typelib_report_actionable_errors(self):
        def unavailable(args, **kwargs):
            result = self.probe(args, **kwargs)
            result.returncode = 1
            return result

        errors, _ = check_sources(False, True, "microphone", "default", self.root,
                                  run=unavailable, which=lambda _: "/fixture")
        self.assertTrue(any("Python GI" in error for error in errors))
        self.assertTrue(any("logged-in desktop session" in error for error in errors))
        self.assertFalse(any(args[-1] == "IsEnabled" for args in self.calls))

    def test_dependency_and_bus_timeouts_do_not_read_any_screen_content(self):
        def timeout(args, **kwargs):
            self.calls.append(args)
            raise subprocess.TimeoutExpired(args, 3)

        errors, _ = check_sources(False, True, "microphone", "default", self.root,
                                  run=timeout, which=lambda _: "/fixture")
        self.assertEqual(len(errors), 2)
        self.assertFalse(any("get_desktop" in " ".join(args) for args in self.calls))
