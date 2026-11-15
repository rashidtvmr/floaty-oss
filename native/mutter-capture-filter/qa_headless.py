#!/usr/bin/env python3
"""Isolated synthetic compositor QA; never connects to the user's desktop.

Launched through run_headless_qa.sh (private D-Bus). Builds need tests enabled;
runtime dependencies: GTK3 PyGObject, Pillow, PipeWire, WirePlumber, GStreamer.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def window(identity, ready, dynamic=False):
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
    GLib.set_prgname(identity)
    Gdk.set_program_class(identity)
    panel = Gtk.Window(title=identity)
    panel.set_default_size(300, 180)
    panel.move(700 if identity == "socket_stream_overlay" else 100, 100)
    if dynamic:
        panel.fullscreen()
    area = Gtk.DrawingArea()
    draws = 0

    def draw(widget, cr):
        nonlocal draws
        draws += 1
        color = (1, 0, 1) if identity == "socket_stream_overlay" else (0, 1, 0)
        if dynamic and identity != "socket_stream_overlay" and int(time.monotonic() * 4) % 2:
            color = (0, 0, 1)
        cr.set_source_rgb(*color)
        cr.paint()
        Path(ready).write_text(json.dumps(dict(draws=draws, width=widget.get_allocated_width(),
                                              height=widget.get_allocated_height(), color=color)))
        return False

    area.connect("draw", draw)
    if dynamic and identity != "socket_stream_overlay":
        # Frame-clock driven: a completely occluded Wayland client must still
        # receive frame callbacks. The opaque panel itself never animates.
        area.add_tick_callback(lambda widget, clock: (widget.queue_draw(), True)[1])
    panel.add(area)
    panel.show_all()
    panel.present()
    Gtk.main()


def scene(output, backend, enabled, dynamic=False):
    from PIL import Image
    processes = []
    try:
        for identity in ("socket_stream_qa-control", "socket_stream_overlay"):
            ready = output / (identity + ".ready")
            process = subprocess.Popen([sys.executable, __file__, "window", identity, str(ready),
                                        str(int(dynamic))],
                                       env=dict(os.environ, GDK_BACKEND=backend))
            processes.append(process)
            deadline = time.monotonic() + 15
            while not ready.exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"{identity} did not draw")
                time.sleep(0.05)
        # Let default-plugin map animations finish before taking measured frames.
        time.sleep(1)
        results = []
        if dynamic:
            subprocess.run([sys.executable, str(HERE / "qa_screencast.py"),
                            str(output / "frame-%d.png"), "--frames", "60"],
                           check=True, timeout=30)
            for filename in sorted(output.glob("frame-*.png")):
                image = Image.open(filename).convert("RGB")
                colors = image.getcolors(image.width * image.height)
                green = sum(n for n, (r, g, b) in colors if r < 40 and g > 210 and b < 40)
                blue = sum(n for n, (r, g, b) in colors if r < 40 and g < 40 and b > 210)
                magenta = sum(n for n, (r, g, b) in colors if r > 210 and g < 40 and b > 210)
                assert magenta < 10, "Stationary opaque panel leaked into animation"
                results.append(dict(frame=filename.name, green=green, blue=blue, magenta=magenta))
            (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
            assert len(results) == 60, "Animation did not deliver 60 frames"
            assert any(r["green"] > 800000 for r in results), "Fullscreen green background missing"
            assert any(r["blue"] > 800000 for r in results), "Fully obscured background stopped updating"
            return
        for source in ("monitor", "area", "window"):
            filename = output / f"{source}.png"
            subprocess.run([sys.executable, str(HERE / "qa_screencast.py"), str(filename),
                            "--source", source], check=True, timeout=30)
            image = Image.open(filename).convert("RGB")
            colors = list(image.getdata())
            green = sum(r < 40 and g > 210 and b < 40 for r, g, b in colors)
            magenta = sum(r > 210 and g < 40 and b > 210 for r, g, b in colors)
            result = dict(backend=backend, enabled=enabled, source=source,
                          green=green, magenta=magenta, size=image.size)
            print(json.dumps(result), flush=True)
            if source != "window":
                assert green > 1000, "Positive control missing from capture"
            assert magenta < 10 if enabled else magenta > 1000, "Panel capture mismatch"
            results.append(result)
        (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    finally:
        for process in reversed(processes):
            stop(process)


def run(build, output, scenario="all"):
    output.mkdir(parents=True, exist_ok=False)
    for backend in ("x11", "wayland"):
        for enabled, dynamic in ((False, False), (True, False), (True, True)):
            if scenario == "static" and dynamic or scenario == "dynamic" and not dynamic:
                continue
            case = output / (f"{backend}-{int(enabled)}" + ("-dynamic" if dynamic else ""))
            case.mkdir()
            with tempfile.TemporaryDirectory(prefix="socket_stream_qa-") as runtime:
                env = dict(os.environ, XDG_RUNTIME_DIR=runtime,
                           SOCKET_STREAM_CAPTURE_EXCLUDE=str(int(enabled)),
                           LIBGL_ALWAYS_SOFTWARE="1", GST_REGISTRY=str(Path(runtime) / "gst-registry.bin"))
                for name in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
                    directory = Path(runtime) / name.lower()
                    directory.mkdir(mode=0o700)
                    env[name] = str(directory)
                for name in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "PIPEWIRE_REMOTE"):
                    env.pop(name, None)
                processes = []
                with (case / "session.log").open("w") as log:
                    try:
                        # Populate the private plugin cache before animation can
                        # compete with GStreamer's expensive first-use scanner.
                        subprocess.run(["gst-inspect-1.0", "pipewiresrc"], env=env,
                                       stdout=log, stderr=log, check=True, timeout=60)
                        for service in ("pipewire", "wireplumber"):
                            processes.append(subprocess.Popen([service], env=env, stdout=log, stderr=log))
                        subprocess.run([
                            str(build / "src/mutter"), "--wayland", "--headless",
                            "--virtual-monitor", "1280x720", "--wayland-display=socket_stream_qa",
                            f"--mutter-plugin={build / 'src/compositor/plugins/libdefault.so'}", "--",
                            sys.executable, __file__, "scene", str(case), backend, str(int(enabled)),
                            str(int(dynamic)),
                        ], env=env, stdout=log, stderr=log, check=True, timeout=120)
                    finally:
                        for process in reversed(processes):
                            stop(process)
            print(f"PASS {case.name}: " + ("60 obscured animation frames" if dynamic else
                                           "monitor, area, focused window"), flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "window":
        window(sys.argv[2], sys.argv[3], len(sys.argv) > 4 and sys.argv[4] == "1")
    elif sys.argv[1] == "scene":
        scene(Path(sys.argv[2]), sys.argv[3], sys.argv[4] == "1",
              len(sys.argv) > 5 and sys.argv[5] == "1")
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("build", type=Path)
        parser.add_argument("output", type=Path)
        parser.add_argument("--scenario", choices=("all", "static", "dynamic"), default="all")
        args = parser.parse_args()
        run(args.build.resolve(), args.output.resolve(), args.scenario)
