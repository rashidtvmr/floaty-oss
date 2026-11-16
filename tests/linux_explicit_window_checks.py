#!/usr/bin/env python3
"""Opt-in real-desktop AT-SPI fixture -> mock answer; never reads user windows.

Run in the logged-in Linux desktop with SOCKET_STREAM_HOME pointing at the
package under test. Only synthetic fixture text is permitted for extraction.
"""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


def main():
    root = Path(os.environ["SOCKET_STREAM_HOME"])
    env = dict(os.environ, GDK_BACKEND="x11", GTK_MODULES="gail:atk-bridge")
    env.pop("NO_AT_BRIDGE", None)
    fixture = subprocess.Popen(["/usr/bin/python3", "-c", '''import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib
w = Gtk.Window(title="Explicit-target diagnostic fixture 20261116")
w.set_default_size(700, 200)
b = Gtk.TextView()
b.get_buffer().set_text("Explain the time complexity of bubble sort.")
w.add(b)
w.show_all()
GLib.timeout_add_seconds(30, lambda: Gtk.main_quit() or False)
Gtk.main()
'''], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc = None
    result = {}
    try:
        selected = []
        for _ in range(6):
            inventory = subprocess.run(
                ["/usr/bin/python3", str(root / "scripts/linux-accessibility.py"), "--mode", "windows"],
                env=env, capture_output=True, text=True, timeout=6, check=True)
            selected = [window for window in json.loads(inventory.stdout)
                        if window["title"] == "Explicit-target diagnostic fixture 20261116"]
            if selected and any(window.get("application") == "-c" for window in selected):
                break
            time.sleep(0.3)
        assert len(selected) == 1, "fixture must have exactly one non-compositor accessible window"
        env["SOCKET_STREAM_ATSPI_TARGET"] = selected[0]["id"]
        proc = subprocess.Popen(
            [str(root / "bin/socket_stream"), "observe-live", "--authorized", "--provider", "mock",
             "--interval", "2", "--force-ocr", "--portal", "--ocr-images"], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        events = queue.Queue(maxsize=256)

        def consume():
            for line in proc.stdout:
                events.put(json.loads(line), timeout=1)

        threading.Thread(target=consume, daemon=True).start()
        deadline = time.monotonic() + 15
        event_types = []
        while time.monotonic() < deadline:
            event = events.get(timeout=max(0.01, deadline - time.monotonic()))
            event_types.append(event.get("type"))
            assert event.get("type") != "error", "fixture screen read failed"
            if event.get("type") == "question_detected":
                normalized = " ".join(event.get("text", "").lower().split())
                result["fixture_question_detected"] = "bubble sort" in normalized
                result["accessibility_source"] = event.get("source") == "accessibility"
            if event.get("type") == "answer_completed":
                result["mock_answer_completed"] = True
                break
        assert result.get("fixture_question_detected") and result.get("mock_answer_completed"), (result, event_types)
        fixture.terminate()
        fixture.wait(timeout=3)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            event = events.get(timeout=max(0.01, deadline - time.monotonic()))
            if event.get("type") == "error":
                result["closed_target_unavailable"] = "Selected question window" in event.get("message", "")
                break
        assert result.get("closed_target_unavailable"), "closed target must fail without retargeting"
        result["raw_background_content_logged"] = False
        print(json.dumps(result))
    finally:
        if proc is not None:
            if proc.poll() is None:
                try:
                    proc.stdin.write("stop\n")
                    proc.stdin.flush()
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    proc.terminate()
                    proc.wait(timeout=3)
            proc.stdin.close()
            proc.stdout.close()
        if fixture.poll() is None:
            fixture.terminate()
            fixture.wait(timeout=3)


if __name__ == "__main__":
    main()
