#!/usr/bin/env python3
"""Build the self-contained, offline Moonshine runtime for the host platform.

Run with Python >=3.10. Downloads pinned dependencies and English model at build
time. Output includes Python, native Moonshine libraries and model weights.
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-model", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        raise SystemExit("Moonshine requires Python 3.10+ (use python3.12 on macOS).")
    machine = platform.machine()
    if platform.system() == "Darwin" and (machine != "arm64" or int(platform.mac_ver()[0].split('.')[0]) < 15):
        raise SystemExit("Moonshine's pinned macOS wheel requires macOS 15+ Apple Silicon.")
    if platform.system() not in ("Darwin", "Linux"):
        raise SystemExit("SocketStream streaming capture supports macOS and Linux only.")
    if args.stage_model:
        from moonshine_voice import ModelArch
        from moonshine_voice.download import download_model_from_info, find_model_info
        model, _arch = download_model_from_info(
            find_model_info("en", ModelArch.TINY_STREAMING),
            cache_root=ROOT / "target/moonshine-downloads",
        )
        output = ROOT / "target/moonshine-assets/models/moonshine"
        shutil.copytree(model, output, dirs_exist_ok=True)
        manifest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(output.iterdir()) if p.is_file()}
        (output.parent / "SHA256.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return
    env_dir = ROOT / "target/moonshine-build-venv"
    if not (env_dir / "bin/python").exists():
        subprocess.run([sys.executable, "-m", "venv", str(env_dir)], check=True)
    python = str(env_dir / "bin/python")
    subprocess.run([python, "-m", "pip", "install", "moonshine-voice==0.1.6", "pyinstaller==6.17.0"], check=True)
    subprocess.run([python, __file__, "--stage-model"], check=True)
    output = ROOT / "target/native-helpers/moonshine"
    subprocess.run([
        python, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "socket_stream_moonshine", "--distpath", str(output),
        "--workpath", str(ROOT / "target/moonshine-freeze"),
        "--specpath", str(ROOT / "target/moonshine-freeze"),
        "--collect-all", "moonshine_voice", "--copy-metadata", "moonshine-voice",
        "--add-data", f"{ROOT / 'target/moonshine-assets/models'}{os.pathsep}models",
        str(ROOT / "scripts/moonshine-worker.py"),
    ], check=True)
    bundle = output / "socket_stream_moonshine"
    subprocess.run([str(bundle / "socket_stream_moonshine"), "--check"], check=True)
    (bundle / "PLATFORM.json").write_text(json.dumps({
        "system": platform.system(), "machine": machine, "moonshine_voice": "0.1.6",
        "model": "English TINY_STREAMING", "python": platform.python_version(),
    }, indent=2) + "\n")
    print(f"Built offline speech runtime: {bundle}")


if __name__ == "__main__":
    main()
