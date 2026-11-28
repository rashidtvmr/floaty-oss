"""Opt-in native Wayland/AT-SPI integration, isolated from the user's desktop.

Run after building the CLI:
  SOCKET_STREAM_RUN_WAYLAND_A11Y=1 python3 -m unittest discover \
    -s tests/acceptance -p test_wayland_atspi_runtime.py -v

Requires Weston, GTK3/Python GI, AT-SPI2 and dbus-run-session. Debian packages
may be extracted under SOCKET_STREAM_WAYLAND_RUNTIME instead of installed.
The test creates a private headless compositor, D-Bus session and synthetic
GTK application. It never connects to the user's display or accessibility bus,
never changes desktop settings, and uses only a loopback synthetic AI provider.
"""
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import test_incremental_screen_e2e as screen_fixture


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/linux-accessibility.py"
FIXTURE_TITLE = "Floaty private native Wayland acceptance fixture"
BEFORE = "Explain the time complexity of binary search before optimization."
AFTER = "Explain the time complexity of binary search after optimization."
UNCHANGED = "Stable synthetic context that must not be repeated in the patch."
SECRET = "synthetic-password-never-export"


def stop_owned(process):
    if process is not None and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
    if process is not None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def event_queue(process, *, as_json=False):
    events = queue.Queue()

    def consume():
        for line in process.stdout:
            try:
                events.put(json.loads(line) if as_json else line.strip())
            except ValueError:
                events.put({"type": "invalid", "text": line})
        events.put(None)

    threading.Thread(target=consume, daemon=True).start()
    return events


def wait_event(events, kind, timeout=15):
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        item = events.get(timeout=max(0.01, deadline - time.monotonic()))
        if item is None:
            raise AssertionError(f"fixture process exited while waiting for {kind}: {seen}")
        seen.append(item)
        if item == kind or (isinstance(item, dict) and item.get("type") == kind):
            return item
        if isinstance(item, dict) and item.get("type") == "error":
            raise AssertionError(f"fixture reported an error: {item}")
    raise AssertionError(f"timed out waiting for {kind}")


def command(process, text):
    process.stdin.write(text + "\n")
    process.stdin.flush()


def gtk_fixture():
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    window = Gtk.Window(title=FIXTURE_TITLE)
    window.set_default_size(800, 400)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    question = Gtk.Label(label=BEFORE)
    box.pack_start(question, False, False, 0)
    box.pack_start(Gtk.Label(label=UNCHANGED), False, False, 0)
    password = Gtk.Entry()
    password.set_visibility(False)
    password.set_text(SECRET)
    box.pack_start(password, False, False, 0)
    window.add(box)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    display_name = type(Gdk.Display.get_default()).__name__
    assert "Wayland" in display_name, f"expected native Wayland display, got {display_name}"

    def control(_source, _condition):
        text = sys.stdin.readline().strip()
        if text == "update":
            question.set_text(AFTER)
            print("updated", flush=True)
        elif text == "stop" or not text:
            Gtk.main_quit()
            return False
        return True

    GLib.io_add_watch(sys.stdin, GLib.IO_IN | GLib.IO_HUP, control)
    print("native-wayland-ready", flush=True)
    Gtk.main()


def isolated_worker():
    """Runs only inside the test-created dbus-run-session."""
    runtime = Path(os.environ["XDG_RUNTIME_DIR"])
    assert runtime.name.startswith("floaty-wayland-")
    assert not os.environ.get("DISPLAY")
    assert os.environ["GDK_BACKEND"] == "wayland"
    assert os.environ.get("DBUS_SESSION_BUS_ADDRESS")

    def terminate(_signum, _frame):
        raise SystemExit("native fixture cancelled")

    signal.signal(signal.SIGTERM, terminate)
    prefix = Path(os.environ["SOCKET_STREAM_WAYLAND_RUNTIME"]) if os.environ.get("SOCKET_STREAM_WAYLAND_RUNTIME") else None
    weston = str(prefix / "usr/bin/weston") if prefix else shutil.which("weston")
    launcher = str(prefix / "usr/libexec/at-spi-bus-launcher") if prefix else "/usr/libexec/at-spi-bus-launcher"
    backend = str(prefix / "usr/lib/x86_64-linux-gnu/libweston-14/headless-backend.so") if prefix else "headless"
    shell = str(prefix / "usr/lib/x86_64-linux-gnu/weston/kiosk-shell.so") if prefix else "kiosk-shell.so"
    weston_log = runtime / "weston.log"
    fixture = compositor = a11y = cli = None
    provider = None
    logs = []

    def spawn(args, name, *, stdin=None, stdout=None):
        log = (runtime / f"{name}.log").open("w+")
        logs.append(log)
        return subprocess.Popen(args, stdin=stdin, stdout=stdout or log, stderr=log,
                                text=True, start_new_session=True)

    def read_helper(mode="tree"):
        result = subprocess.run(["/usr/bin/python3", str(HELPER), "--mode", mode],
                                capture_output=True, text=True, timeout=7)
        if result.returncode:
            raise AssertionError(f"native helper failed ({result.returncode}): {result.stderr}")
        return json.loads(result.stdout)

    try:
        # Memory-only settings affect just this disposable accessibility bus.
        a11y = spawn([launcher, "--launch-immediately", "--a11y=1", "--screen-reader=0"], "a11y")
        compositor = spawn([weston, f"--backend={backend}", "--renderer=pixman",
                            f"--shell={shell}", "--no-config", "--idle-time=0",
                            "--socket=floaty-test-wayland", f"--log={weston_log}"], "compositor")
        deadline = time.monotonic() + 8
        while not (runtime / "floaty-test-wayland").exists():
            if compositor.poll() is not None or time.monotonic() >= deadline:
                raise AssertionError("headless Weston failed: " + (weston_log.read_text() if weston_log.exists() else "no log"))
            time.sleep(0.05)
        fixture = spawn(["/usr/bin/python3", str(Path(__file__).resolve()), "--gtk-fixture"],
                        "gtk", stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        fixture_events = event_queue(fixture)
        wait_event(fixture_events, "native-wayland-ready")
        deadline = time.monotonic() + 10
        selected = []
        while time.monotonic() < deadline:
            selected = [window for window in read_helper("windows") if window["title"] == FIXTURE_TITLE]
            if selected:
                break
            time.sleep(0.1)
        assert len(selected) == 1, "synthetic native Wayland window must register exactly once"
        os.environ["SOCKET_STREAM_ATSPI_TARGET"] = selected[0]["id"]
        before = read_helper()
        assert before["window_id"] == selected[0]["id"]
        assert BEFORE in json.dumps(before)
        assert SECRET not in json.dumps(before)
        assert all(node["stable_id"] for node in before["nodes"])

        # Inspect complete HTTP requests, not the deliberately bounded API
        # preview shown in the UI. This provider is bound to loopback only.
        provider = screen_fixture.Provider()
        os.environ.update(SOCKET_STREAM_BASE_URL=f"http://127.0.0.1:{provider.server.server_port}/v1",
                          SOCKET_STREAM_MODEL="fixture-model", SOCKET_STREAM_API_KEY="")
        binary = Path(os.environ["SOCKET_STREAM_TEST_CLI"])
        cli = spawn([str(binary), "observe-live", "--authorized", "--provider", "openai-compatible",
                     "--interval", "3600", "--force-ocr", "--portal", "--ocr-images"],
                    "cli", stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        cli_events = event_queue(cli, as_json=True)
        wait_event(cli_events, "session_started")
        # Observe-live performs one immediate automatic read. Wait for its
        # answer so a manual read cannot race it and create a duplicate turn.
        wait_event(cli_events, "answer_completed")
        assert len(provider.requests) == 1
        first_messages = provider.requests[0]["messages"]
        assert [message["role"] for message in first_messages] == ["system", "user"]
        first_prompt = first_messages[-1]["content"].split(screen_fixture.MARKER, 1)[1]
        first_update = json.loads(first_prompt)
        assert first_update["kind"] == "snapshot"

        command(fixture, "update")
        wait_event(fixture_events, "updated")
        deadline = time.monotonic() + 6
        while True:
            after = read_helper()
            if AFTER in json.dumps(after):
                break
            assert time.monotonic() < deadline, "updated GTK label did not reach AT-SPI"
            time.sleep(0.1)
        old_question = next(node for node in before["nodes"] if BEFORE in node["text"] or BEFORE in node["name"])
        new_question = next(node for node in after["nodes"] if AFTER in node["text"] or AFTER in node["name"])
        assert old_question["stable_id"] == new_question["stable_id"]
        assert before["window_id"] == after["window_id"]
        command(cli, "read-screen")
        wait_event(cli_events, "answer_completed")
        assert len(provider.requests) == 2
        second_messages = provider.requests[-1]["messages"]
        latest = second_messages[-1]["content"]
        patch = json.loads(latest.split(screen_fixture.MARKER, 1)[1])
        assert patch["kind"] == "patch", patch
        assert len(patch["updated"]) == 1, patch
        assert AFTER in json.dumps(patch["updated"])
        assert UNCHANGED not in json.dumps(patch)
        assert not patch["added"] and not patch["removed"]
        assert [message["role"] for message in second_messages] == ["system", "user", "assistant", "user"]
        assert second_messages[1] == first_messages[1]
        assert second_messages[2]["content"] == "Fixture answer 1."

        command(cli, "pause")
        wait_event(cli_events, "session_paused")
        command(cli, "read-screen")
        command(cli, "stop")
        wait_event(cli_events, "session_stopped")
        cli.wait(timeout=5)
        command(fixture, "stop")
        fixture.wait(timeout=5)
        stale = subprocess.run(["/usr/bin/python3", str(HELPER), "--mode", "tree"],
                               capture_output=True, text=True, timeout=7)
        assert stale.returncode == 3 and "Selected question window is unavailable" in stale.stderr
        print(json.dumps({"native_wayland": True, "native_atspi": True, "stable_keys": True,
                          "changed_nodes": len(patch["updated"]), "session_history": True,
                          "protected_text_excluded": True, "closed_target_refused": True,
                          "display_backend": "Weston headless/pixman", "external_ai": False}))
    finally:
        for process in (cli, fixture, compositor, a11y):
            stop_owned(process)
        if provider is not None:
            provider.close()
        for log in logs:
            log.close()


@unittest.skipUnless(sys.platform.startswith("linux") and os.environ.get("SOCKET_STREAM_RUN_WAYLAND_A11Y") == "1",
                     "opt in with SOCKET_STREAM_RUN_WAYLAND_A11Y=1; requires native Wayland test dependencies")
class NativeWaylandAcceptance(unittest.TestCase):
    def test_native_wayland_tree_updates_reach_the_session_as_one_changed_node(self):
        binary = Path(os.environ.get("SOCKET_STREAM_TEST_CLI", ROOT / "target/debug/socket_stream"))
        self.assertTrue(binary.is_file(), "build the CLI before running this opt-in acceptance test")
        self.assertIsNotNone(shutil.which("dbus-run-session"))
        with tempfile.TemporaryDirectory(prefix="floaty-wayland-") as temp:
            runtime = Path(temp)
            runtime.chmod(0o700)
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.bind(str(runtime / "capability-check.sock"))
                (runtime / "capability-check.sock").unlink()
            except OSError as error:
                self.skipTest(f"native Wayland/D-Bus require Unix sockets unavailable in this executor: {error}")
            env = dict(os.environ)
            for key in ("DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "DBUS_STARTER_ADDRESS", "DBUS_STARTER_BUS_TYPE",
                        "AT_SPI_BUS_ADDRESS", "SOCKET_STREAM_ATSPI_TARGET", "NO_AT_BRIDGE", "GTK_MODULES"):
                env.pop(key, None)
            env.update(XDG_RUNTIME_DIR=temp, XDG_DATA_HOME=str(runtime / "data"),
                       XDG_CONFIG_HOME=str(runtime / "config"), XDG_CACHE_HOME=str(runtime / "cache"),
                       XDG_SESSION_TYPE="wayland", WAYLAND_DISPLAY="floaty-test-wayland", GDK_BACKEND="wayland",
                       GSETTINGS_BACKEND="memory", GTK_A11Y="always", GTK_MODULES="atk-bridge",
                       SOCKET_STREAM_HOME=str(ROOT), SOCKET_STREAM_CONFIG=str(runtime / "missing-config.json"),
                       SOCKET_STREAM_TEST_CLI=str(binary.resolve()))
            prefix = env.get("SOCKET_STREAM_WAYLAND_RUNTIME")
            if prefix:
                lib = Path(prefix) / "usr/lib/x86_64-linux-gnu"
                env["LD_LIBRARY_PATH"] = os.pathsep.join([str(lib), str(lib / "weston"), env.get("LD_LIBRARY_PATH", "")])
                env["GI_TYPELIB_PATH"] = str(lib / "girepository-1.0")
                services = runtime / "data/dbus-1/services"
                services.mkdir(parents=True)
                (services / "org.a11y.Bus.service").write_text(
                    "[D-BUS Service]\nName=org.a11y.Bus\nExec=" + str(Path(prefix) / "usr/libexec/at-spi-bus-launcher") + "\n")
            process = subprocess.Popen(["dbus-run-session", "--", "/usr/bin/python3", str(Path(__file__).resolve()), "--isolated-worker"],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, start_new_session=True)
            try:
                stdout, stderr = process.communicate(timeout=60)
            finally:
                stop_owned(process)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            summary = json.loads(stdout.strip().splitlines()[-1])
            self.assertEqual(summary["changed_nodes"], 1)
            self.assertTrue(summary["native_wayland"] and summary["native_atspi"])
            self.assertFalse(summary["external_ai"])


if __name__ == "__main__":
    if "--gtk-fixture" in sys.argv:
        gtk_fixture()
    elif "--isolated-worker" in sys.argv:
        isolated_worker()
    else:
        unittest.main()
