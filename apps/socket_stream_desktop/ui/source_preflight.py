"""Readiness probes only. Never reads screen text or opens a recording device."""
import os
from pathlib import Path
import shutil
import selectors
import subprocess
import sys
import time
from types import SimpleNamespace


def _probe(args, *, timeout=3, env=None, **_kwargs):
    """Cap stdout while reading, not after an unbounded communicate()."""
    child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    data = bytearray()
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    try:
        os.set_blocking(child.stdout.fileno(), False)
        selector.register(child.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            if not selector.select(remaining):
                raise subprocess.TimeoutExpired(args, timeout)
            chunk = os.read(child.stdout.fileno(), 4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 65536:
                raise OSError("Readiness probe output exceeded its limit")
        status = child.wait(timeout=max(0.001, deadline - time.monotonic()))
        return SimpleNamespace(returncode=status, stdout=data.decode("utf-8", "replace"))
    finally:
        selector.close()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        child.stdout.close()


def check_sources(audio, screen, source, device, root, *, run=_probe,
                  which=shutil.which):
    errors = []
    notes = ["OS access may still require approval; a readiness check is not a permission grant."]
    if not audio and not screen:
        return ["Select audio and/or screen."], notes
    if audio:
        if not which("ffmpeg") or not which("pactl"):
            errors.append("Audio needs FFmpeg and PulseAudio/PipeWire's pactl. Run dependency setup.")
        else:
            try:
                result = run(["pactl", "list", "short", "sources"], capture_output=True,
                             text=True, timeout=3, check=False)
                names = {line.split()[1] for line in result.stdout[:65536].splitlines()
                         if len(line.split()) >= 2}
                if result.returncode != 0:
                    errors.append("Cannot access the audio server. Check the desktop session and audio permissions.")
                else:
                    selected = device.strip() or "default"
                    if selected == "default":
                        setting = "get-default-sink" if source == "system" else "get-default-source"
                        default = run(["pactl", setting], capture_output=True, text=True,
                                      timeout=3, check=False)
                        selected = default.stdout.strip() + (".monitor" if source == "system" else "")
                    if selected not in names:
                        errors.append("Selected audio input/monitor is unavailable. Choose an available device in Audio options.")
            except (OSError, subprocess.TimeoutExpired):
                errors.append("Audio readiness check timed out or could not run. Check the audio service.")
        worker = Path(root) / "target/native-helpers/moonshine/socket_stream_moonshine/socket_stream_moonshine"
        if not worker.is_file() or not os.access(worker, os.X_OK):
            errors.append("Bundled Moonshine speech engine is missing or not executable. Reinstall the release.")
    if screen:
        try:
            result = run([sys.executable, "-c",
                          "import gi; gi.require_version('Atspi', '2.0'); from gi.repository import Atspi"],
                         capture_output=True, text=True, timeout=3, check=False)
            if result.returncode:
                errors.append("Screen reading needs AT-SPI2 and Python GI. Run dependency setup and enable Accessibility.")
        except (OSError, subprocess.TimeoutExpired):
            errors.append("Accessibility readiness check could not run. Check the desktop accessibility service.")
        if which("gdbus"):
            try:
                bus = run(["gdbus", "call", "--session", "--dest", "org.a11y.Bus",
                           "--object-path", "/org/a11y/bus", "--method", "org.a11y.Bus.GetAddress"],
                          capture_output=True, text=True, timeout=3, check=False)
                if bus.returncode != 0:
                    errors.append("Cannot reach the desktop accessibility bus. Enable Accessibility in the logged-in desktop session.")
            except (OSError, subprocess.TimeoutExpired):
                errors.append("Accessibility bus check could not run. Check the desktop session.")
        else:
            notes.append("Accessibility bus connectivity was not checked because gdbus is unavailable.")
        notes.append("Screen reading prefers accessibility. When authorized OCR needs pixels, approve the OS screen chooser; cancelling stops that read.")
    return errors, notes
