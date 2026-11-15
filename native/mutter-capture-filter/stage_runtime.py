#!/usr/bin/env python3
"""Publish a validated, versioned Ubuntu Mutter runtime under /opt.

Input is a completed Meson build and its private DESTDIR installation, never
the live /usr tree. Activation of the desktop user's service is a separate step.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import patch_mutter
import validate_runtime as runtime


def _command(argv, *, run=subprocess.run, **kwargs):
    result = run(argv, capture_output=True, text=True, timeout=30, check=False, **kwargs)
    if result.returncode:
        raise runtime.InvalidRuntime(f"runtime staging command failed: {Path(argv[0]).name}")
    return result.stdout.strip()


def source_version(dsc: Path) -> str:
    """Verify the downloaded source descriptor's archive hashes, without unpacking."""
    if dsc.stat().st_size > 65536:
        raise runtime.InvalidRuntime("source descriptor is too large")
    text = dsc.read_text(encoding="utf-8")
    versions = re.findall(r"^Version: (\S+)$", text, re.MULTILINE)
    if (len(versions) != 1 or not runtime.VERSION.fullmatch(versions[0])
            or re.findall(r"^Source: (\S+)$", text, re.MULTILINE) != ["mutter"]):
        raise runtime.InvalidRuntime("expected an Ubuntu Mutter source descriptor")
    section = re.search(r"^Checksums-Sha256:\n((?: .+\n)+)", text, re.MULTILINE)
    if not section:
        raise runtime.InvalidRuntime("source archive checksums are missing")
    seen = set()
    for line in section[1].splitlines():
        fields = line.split()
        if (len(fields) != 3 or not runtime.SHA256.fullmatch(fields[0])
                or not fields[1].isdigit() or not re.fullmatch(r"mutter_[A-Za-z0-9.+~_\-]+\.tar\.[a-z0-9]+", fields[2])
                or fields[2] in seen):
            raise runtime.InvalidRuntime("invalid source archive checksum entry")
        seen.add(fields[2])
        archive = dsc.parent / fields[2]
        if not archive.is_file() or archive.is_symlink() or archive.stat().st_size != int(fields[1]):
            raise runtime.InvalidRuntime("source archive is missing or has changed")
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != fields[0]:
            raise runtime.InvalidRuntime("source archive checksum mismatch")
    if len(seen) != 2:
        raise runtime.InvalidRuntime("expected the upstream and Debian source archives")
    return versions[0]


def build_artifacts(build: Path, destdir: Path, version: str, patch: Path, *, run=subprocess.run):
    info = json.loads((build / "meson-info/meson-info.json").read_text(encoding="utf-8"))
    source = Path(info["directories"]["source"]).resolve(strict=True)
    changelog = (source / "debian/changelog").read_text(encoding="utf-8").splitlines()[0]
    if not changelog.startswith(f"mutter ({version}) "):
        raise runtime.InvalidRuntime("Meson source differs from the Ubuntu source descriptor")
    if patch_mutter.prepare(source):
        raise runtime.InvalidRuntime("Meson source does not contain the complete capture patch")
    if not patch.is_file() or not 1 <= patch.stat().st_size <= 1024 * 1024:
        raise runtime.InvalidRuntime("expected the applied capture patch")
    _command(["git", "apply", "--reverse", "--check", str(patch)], cwd=source, run=run)
    installed = json.loads((build / "meson-info/intro-installed.json").read_text(encoding="utf-8"))
    artifacts = {}
    for name in runtime.LIBRARIES + runtime.TYPELIBS:
        basename = Path(name).name
        installed_name = basename + ".0.0" if name in runtime.LIBRARIES else basename
        matches = [value for key, value in installed.items() if Path(key).name == installed_name]
        if len(matches) != 1 or not Path(matches[0]).is_absolute():
            raise runtime.InvalidRuntime("Meson install inventory lacks the expected Mutter artifacts")
        candidate = destdir / Path(matches[0]).relative_to("/")
        if (candidate.is_symlink() or not candidate.is_file()
                or not candidate.resolve(strict=True).is_relative_to(destdir)):
            raise runtime.InvalidRuntime("private DESTDIR installation is incomplete")
        artifacts[name] = candidate
    return artifacts


def prepare_library(path: Path, *, run=subprocess.run):
    if _command(["/usr/bin/patchelf", "--print-soname", str(path)], run=run) != path.name:
        raise runtime.InvalidRuntime("Mutter library SONAME does not match its filename")
    # Mutter intentionally uses DT_RPATH, which precedes LD_LIBRARY_PATH. Its
    # installed /usr RPATH would silently load stock Cogl/Mtk alongside our libs.
    # Modify only this private COPY and resolve its family next to itself.
    _command(["/usr/bin/patchelf", "--force-rpath", "--set-rpath", "$ORIGIN", str(path)], run=run)
    if _command(["/usr/bin/patchelf", "--print-rpath", str(path)], run=run) != "$ORIGIN":
        raise runtime.InvalidRuntime("could not make the private runtime relocatable")
    if path.name == "libmutter-clutter-18.so.0":
        symbols = _command(["/usr/bin/nm", "-D", "--defined-only", str(path)], run=run)
        names = {line.split()[-1] for line in symbols.splitlines() if line.split()}
        if not {"clutter_actor_set_capture_filter", "clutter_texture_content_set_capture_excluded"} <= names:
            raise runtime.InvalidRuntime("Clutter capture-filter exports are missing")


def prepare_shell(path: Path, *, run=subprocess.run):
    # GNOME Shell prepends its built-in /usr GI paths after reading the
    # environment. Its executable $ORIGIN introspection support must take
    # precedence too, or GI dlopen registers a second stock ClutterActor type.
    search = runtime.SHELL_RUNPATH
    _command(["/usr/bin/patchelf", "--set-rpath", search, str(path)], run=run)
    if _command(["/usr/bin/patchelf", "--print-rpath", str(path)], run=run) != search:
        raise runtime.InvalidRuntime("could not set the private GNOME Shell introspection paths")


def publish_current(prefix: Path, target: Path) -> None:
    """Atomic pointer update; never overwrite an unrelated file/directory."""
    if target.parent != prefix or target.is_symlink() or not target.is_dir():
        raise runtime.InvalidRuntime("current must point to a version directory inside the runtime prefix")
    current = prefix / "current"
    if os.path.lexists(current):
        if not current.is_symlink():
            raise runtime.InvalidRuntime("current is not a managed runtime symlink")
        old = os.readlink(current)
        if not runtime.IDENTIFIER.fullmatch(old) or ".socket-stream." not in old:
            raise runtime.InvalidRuntime("current points outside the managed runtime layout")
        if old == target.name:
            return
    descriptor, temporary = tempfile.mkstemp(prefix=".current-", dir=prefix)
    os.close(descriptor)
    temp = Path(temporary)
    try:
        temp.unlink()
        temp.symlink_to(target.name)
        os.replace(temp, current)
    finally:
        temp.unlink(missing_ok=True)


def stage_runtime(artifacts, *, prefix: Path, dsc: Path, patch: Path, version: str,
                  packages: dict[str, str], run=subprocess.run, owner_uid: int = 0,
                  shell_source: Path = Path("/usr/bin/gnome-shell")) -> Path:
    """Copy, relocate, hash, validate, then publish. Existing versions are immutable."""
    if set(artifacts) != set(runtime.LIBRARIES + runtime.TYPELIBS):
        raise runtime.InvalidRuntime("incomplete runtime artifact set")
    prefix = prefix.absolute()
    runtime.trusted_directory(prefix.parent, owner_uid=owner_uid)
    if prefix.is_symlink():
        raise runtime.InvalidRuntime("runtime prefix must not be a symlink")
    prefix.mkdir(mode=0o755, exist_ok=True)
    runtime.trusted_directory(prefix, owner_uid=owner_uid)
    temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=prefix))
    try:
        for directory in ("lib", "typelib"):
            (temporary / directory).mkdir(mode=0o755)
        payload = dict(artifacts)
        runtime.file_digest(shell_source, owner_uid=owner_uid)
        payload[runtime.SHELL] = shell_source
        payload.update({name: Path(__file__).with_name(name) for name in runtime.SCRIPTS})
        payload.update({"source.dsc": dsc, "capture.patch": patch})
        files = {}
        for name, source in payload.items():
            target = temporary / name
            shutil.copyfile(source, target)
            if name in runtime.LIBRARIES:
                prepare_library(target, run=run)
            elif name == runtime.SHELL:
                prepare_shell(target, run=run)
            target.chmod(0o555 if name in runtime.EXECUTABLES else 0o444)
            digest, size = runtime.file_digest(target, owner_uid=owner_uid)
            files[name] = {"sha256": digest, "size": size}
        manifest = {
            "schema": runtime.SCHEMA, "source_version": version, "shell": runtime.SHELL,
            "source_sha256": files["source.dsc"]["sha256"],
            "patch_sha256": files["capture.patch"]["sha256"],
            "packages": packages, "files": files, "libraries": list(runtime.LIBRARIES),
            "library_dir": "lib", "typelib_dir": "typelib",
        }
        manifest["runtime_id"] = runtime.runtime_identifier(manifest)
        if not runtime.IDENTIFIER.fullmatch(manifest["runtime_id"]):
            raise runtime.InvalidRuntime("runtime identifier is invalid")
        output = temporary / "runtime.json"
        output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        output.chmod(0o444)
        temporary.chmod(0o755)
        runtime.validate_runtime(output, run=run, owner_uid=owner_uid)
        target = prefix / manifest["runtime_id"]
        if os.path.lexists(target):
            if target.is_symlink():
                raise runtime.InvalidRuntime("existing runtime version is a symlink")
            _, existing = runtime.validate_runtime(target / "runtime.json", run=run, owner_uid=owner_uid)
            if existing != manifest:
                raise runtime.InvalidRuntime("existing runtime version differs; refusing to replace it")
        else:
            os.rename(temporary, target)
        # Re-check after the move: loader paths must resolve in the final root.
        runtime.validate_runtime(target / "runtime.json", run=run, owner_uid=owner_uid)
        publish_current(prefix, target)
        return target
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", required=True, type=Path)
    parser.add_argument("--destdir", required=True, type=Path)
    parser.add_argument("--source-dsc", required=True, type=Path)
    parser.add_argument("--patch", required=True, type=Path)
    parser.add_argument("--prefix", type=Path, default=Path("/opt/socket_stream-mutter"))
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise runtime.InvalidRuntime("publishing the runtime requires root ownership")
        dsc, patch = args.source_dsc.resolve(strict=True), args.patch.resolve(strict=True)
        version = source_version(dsc)
        packages = runtime.installed_packages()
        if any(packages[name] != version for name in runtime.PACKAGES if "gnome-shell" not in name):
            raise runtime.InvalidRuntime("source version does not match the installed Ubuntu packages")
        artifacts = build_artifacts(args.build.resolve(strict=True), args.destdir.resolve(strict=True), version, patch)
        target = stage_runtime(artifacts, prefix=args.prefix, dsc=dsc, patch=patch,
                               version=version, packages=packages)
        print(f"Published runtime: {target}")
        print("Desktop activation requires a passing isolated Shell startup check and an armed failure guard.")
        return 0
    except (runtime.InvalidRuntime, patch_mutter.PatchError) as exc:
        print(f"SocketStream Mutter: {exc}", file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("SocketStream Mutter: staging could not complete", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
