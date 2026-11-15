#!/usr/bin/env python3
"""Validate a side-by-side Mutter runtime before it can start GNOME Shell.

This file is deliberately standalone: the login path needs only system Python.
Static success establishes file/package/direct-loader integrity, not a working
desktop. Activation also requires startup_probe.py and the persistent guard.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

SCHEMA = 2
SHELL = "gnome-shell"
SHELL_RUNPATH = "$ORIGIN/lib:$ORIGIN/typelib:/usr/lib/gnome-shell"
EXECUTABLES = (SHELL, "launch-gnome-shell")
LIBRARIES = tuple(f"lib/{name}.so.0" for name in (
    "libmutter-18", "libmutter-clutter-18", "libmutter-cogl-18", "libmutter-mtk-18",
))
TYPELIBS = tuple(f"typelib/{name}-18.typelib" for name in ("Meta", "Clutter", "Cogl", "Mtk"))
SCRIPTS = ("validate_runtime.py", "launch-gnome-shell", "startup_probe.py", "startup_guard.py")
PROVENANCE = ("source.dsc", "capture.patch")
PACKAGES = (
    "gnome-shell", "gnome-shell-common", "libmutter-18-0", "mutter-common",
    "mutter-common-bin", "gir1.2-mutter-18",
)
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+\-]{0,159}\Z")
VERSION = re.compile(r"[0-9][A-Za-z0-9.+:~_\-]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MODE = re.compile(r"[A-Za-z0-9_.\-]{1,64}\Z")


class InvalidRuntime(ValueError):
    """A fixed, actionable diagnostic safe to show in the session journal."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidRuntime("duplicate manifest field")
        result[key] = value
    return result


def trusted_directory(path: Path, *, owner_uid: int = 0) -> None:
    """Check the directory and its parents, including replaceable ancestors.

    owner_uid is injected only by tests. Production callers require root-owned
    paths. A root-owned sticky temporary ancestor cannot replace our directory.
    """
    for current in (path, *path.parents):
        info = current.lstat()
        sticky_parent = current != path and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, owner_uid)
                or (info.st_mode & 0o022 and not sticky_parent)):
            raise InvalidRuntime("runtime directory has unsafe ownership or permissions")


def _regular(path: Path, owner_uid: int):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid
            or info.st_mode & 0o022):
        raise InvalidRuntime("runtime file has unsafe ownership, permissions, or type")
    return info


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def file_digest(path: Path, *, owner_uid: int = 0) -> tuple[str, int]:
    before = _regular(path, owner_uid)
    if before.st_size < 1 or before.st_size > 256 * 1024 * 1024:
        raise InvalidRuntime("runtime file size is outside supported bounds")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise InvalidRuntime("runtime changed during validation")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise InvalidRuntime("runtime changed during validation")
    if _stamp(path.lstat()) != _stamp(before):
        raise InvalidRuntime("runtime changed during validation")
    return digest.hexdigest(), before.st_size


def installed_packages(names=PACKAGES, *, run=subprocess.run) -> dict[str, str]:
    try:
        result = run(
            ["/usr/bin/dpkg-query", "-W", "-f=${Package}\t${Version}\t${Status}\n", *names],
            capture_output=True, text=True, timeout=10, check=False,
            env=dict(os.environ, LC_ALL="C"),
        )
    except FileNotFoundError as exc:
        raise InvalidRuntime("required Ubuntu tool /usr/bin/dpkg-query is unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise InvalidRuntime("Ubuntu package validation timed out") from exc
    if result.returncode:
        raise InvalidRuntime("required Ubuntu packages are unavailable")
    packages = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if (len(fields) != 3 or fields[0] not in names or fields[0] in packages
                or fields[2] != "install ok installed" or not VERSION.fullmatch(fields[1])):
            raise InvalidRuntime("required Ubuntu package status is unknown")
        packages[fields[0]] = fields[1]
    if set(packages) != set(names):
        raise InvalidRuntime("required Ubuntu package status is incomplete")
    return packages


def loader_environment(root: Path, environ=None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    env["LD_LIBRARY_PATH"] = str(root / "lib") + (
        ":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    env["GI_TYPELIB_PATH"] = str(root / "typelib") + (
        ":" + env["GI_TYPELIB_PATH"] if env.get("GI_TYPELIB_PATH") else "")
    return env


def check_search_paths(root: Path, *, run=subprocess.run) -> None:
    """Enforce the ELF/GI search contract, even if a manifest was made elsewhere."""
    expected = {str(root / SHELL): ("RUNPATH", SHELL_RUNPATH)}
    expected.update({str(root / name): ("RPATH", "$ORIGIN") for name in LIBRARIES})
    try:
        result = run(["/usr/bin/readelf", "--dynamic", "--wide", *expected],
                     capture_output=True, text=True, timeout=10, check=False,
                     env=dict(clean_environment(root), LC_ALL="C"))
    except FileNotFoundError as exc:
        raise InvalidRuntime("required Ubuntu tool /usr/bin/readelf is unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise InvalidRuntime("GNOME Shell ELF search-path validation timed out") from exc
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise InvalidRuntime("GNOME Shell ELF search paths could not be inspected")
    found, current = {}, None
    for line in result.stdout.splitlines():
        if line.startswith("File: "):
            current = line[6:]
            if current not in expected or current in found:
                raise InvalidRuntime("unexpected ELF search-path evidence")
            found[current] = []
        elif "(RUNPATH)" in line or "(RPATH)" in line:
            match = re.fullmatch(r"\s*0x[0-9a-fA-F]+\s+\((RUNPATH|RPATH)\)\s+[^\[]*\[([^\]]*)\]\s*", line)
            if current is None or match is None:
                raise InvalidRuntime("invalid ELF search-path evidence")
            found[current].append(match.groups())
    if set(found) != set(expected) or any(found[name] != [paths] for name, paths in expected.items()):
        raise InvalidRuntime("private Shell RUNPATH or Mutter RPATH does not match the GI loader contract")


def check_loader(root: Path, *, run=subprocess.run) -> None:
    """Check search tags and direct dependencies. Full GI startup is a separate probe."""
    check_search_paths(root, run=run)
    expected = {Path(name).name: root / name for name in LIBRARIES}
    for executable in (SHELL, *LIBRARIES):
        try:
            result = run(
                ["/usr/bin/ldd", str(root / executable)], capture_output=True, text=True,
                timeout=10, check=False, env=dict(loader_environment(root, clean_environment(root)), LC_ALL="C"),
            )
        except FileNotFoundError as exc:
            raise InvalidRuntime("required Ubuntu tool /usr/bin/ldd is unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise InvalidRuntime("GNOME Shell loader validation timed out") from exc
        if result.returncode or "not found" in result.stdout:
            raise InvalidRuntime("GNOME Shell has unresolved runtime dependencies")
        found = set()
        for line in result.stdout.splitlines():
            match = re.fullmatch(r"\s*(libmutter[^\s]*)\s+=>\s+(.+?)\s+\(0x[0-9a-fA-F]+\)\s*", line)
            if not match:
                continue
            name, target = match.groups()
            if name not in expected or name in found:
                raise InvalidRuntime("GNOME Shell resolves an unexpected Mutter library")
            if Path(target).resolve(strict=True) != expected[name]:
                raise InvalidRuntime("GNOME Shell would mix stock and custom Mutter libraries")
            found.add(name)
        if executable == SHELL and found != set(expected):
            raise InvalidRuntime("GNOME Shell Mutter dependency set is incomplete")


def runtime_identifier(manifest) -> str:
    content = {key: value for key, value in manifest.items() if key != "runtime_id"}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:16]
    return f"{manifest['source_version']}.socket-stream.{digest}"


def validate_runtime(manifest_path: Path, *, run=subprocess.run, owner_uid: int = 0):
    """Production validation always requires owner_uid=0; other UIDs are test fixtures."""
    if not manifest_path.is_absolute():
        raise InvalidRuntime("runtime manifest path must be absolute")
    manifest_path = manifest_path.resolve(strict=True)
    if manifest_path.name != "runtime.json":
        raise InvalidRuntime("expected runtime.json")
    root = manifest_path.parent
    trusted_directory(root, owner_uid=owner_uid)
    info = _regular(manifest_path, owner_uid)
    if info.st_size > 65536:
        raise InvalidRuntime("runtime manifest is too large")
    data = json.loads(manifest_path.read_bytes(), object_pairs_hook=_object)
    if (not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != SCHEMA
            or not isinstance(data.get("runtime_id"), str)
            or not IDENTIFIER.fullmatch(data["runtime_id"])
            or not isinstance(data.get("source_version"), str)
            or not VERSION.fullmatch(data["source_version"])):
        raise InvalidRuntime("unsupported runtime manifest")
    for key in ("source_sha256", "patch_sha256"):
        if not isinstance(data.get(key), str) or not SHA256.fullmatch(data[key]):
            raise InvalidRuntime("runtime provenance is incomplete")
    if (data.get("library_dir") != "lib" or data.get("typelib_dir") != "typelib" or data.get("shell") != SHELL
            or not isinstance(data.get("libraries"), list)
            or sorted(data["libraries"], key=str) != sorted(LIBRARIES)):
        raise InvalidRuntime("unsupported Mutter library layout")
    packages = data.get("packages")
    if (not isinstance(packages, dict) or set(packages) != set(PACKAGES)
            or any(not isinstance(v, str) or not VERSION.fullmatch(v) for v in packages.values())):
        raise InvalidRuntime("runtime package contract is incomplete")
    if any(packages[name] != data["source_version"] for name in PACKAGES if "gnome-shell" not in name):
        raise InvalidRuntime("Mutter source and package versions differ")
    if installed_packages(run=run) != packages:
        raise InvalidRuntime("Ubuntu package versions changed; rebuild the custom runtime")
    files = data.get("files")
    required = set(LIBRARIES + TYPELIBS + SCRIPTS + (SHELL,))
    if not isinstance(files, dict) or not required <= files.keys() or files.keys() - required - set(PROVENANCE):
        raise InvalidRuntime("runtime file set is incomplete or unexpected")
    for directory, entries in (("lib", LIBRARIES), ("typelib", TYPELIBS)):
        trusted_directory(root / directory, owner_uid=owner_uid)
        if {p.name for p in (root / directory).iterdir()} != {Path(p).name for p in entries}:
            raise InvalidRuntime("runtime loader directory contains unexpected files")
    for name, expected in files.items():
        if (not isinstance(expected, dict) or set(expected) != {"sha256", "size"}
                or not isinstance(expected["sha256"], str) or not SHA256.fullmatch(expected["sha256"])
                or type(expected["size"]) is not int):
            raise InvalidRuntime("runtime file metadata is invalid")
        digest, size = file_digest(root / name, owner_uid=owner_uid)
        if size != expected["size"] or digest != expected["sha256"]:
            raise InvalidRuntime("runtime file hash or size mismatch")
    for name in EXECUTABLES:
        if not (root / name).stat().st_mode & stat.S_IXUSR:
            raise InvalidRuntime("runtime launcher or private Shell is not executable")
    for filename, key in (("source.dsc", "source_sha256"), ("capture.patch", "patch_sha256")):
        if filename in files and files[filename]["sha256"] != data[key]:
            raise InvalidRuntime("runtime provenance hash mismatch")
    if data["runtime_id"] != runtime_identifier(data):
        raise InvalidRuntime("runtime identifier does not match its manifest")
    check_loader(root, run=run)
    if _stamp(manifest_path.lstat()) != _stamp(info):
        raise InvalidRuntime("runtime manifest changed during validation")
    return root, data


def clean_environment(root: Path, environ=None) -> dict[str, str]:
    """The stock fallback must never inherit this or an older private runtime."""
    env = dict(os.environ if environ is None else environ)
    env.pop("SOCKET_STREAM_CAPTURE_EXCLUDE", None)
    for key, directory in (("LD_LIBRARY_PATH", "lib"), ("GI_TYPELIB_PATH", "typelib")):
        entries = []
        for value in env.get(key, "").split(":"):
            if not value:
                continue
            entry = os.path.normpath(value)
            path = Path(entry)
            if (path == root / directory
                    or (path.name == directory and (
                        entry.startswith("/opt/socket_stream-mutter/")
                        or ".socket-stream." in path.parent.name))):
                continue
            entries.append(entry)
        if entries:
            env[key] = ":".join(entries)
        else:
            env.pop(key, None)
    return env


def load_sibling(root: Path, name: str):
    """Load only an explicitly selected, already-validated runtime payload under -I."""
    spec = importlib.util.spec_from_file_location("socket_stream_" + name, root / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def launch_runtime(manifest: Path, state: Path, mode: str, *, run=subprocess.run,
                   execve=os.execve, environ=None, owner_uid: int = 0):
    """Consume the one-shot latch, then exec: PID and systemd notify stay intact."""
    env = clean_environment(manifest.parent, environ)
    arguments = [f"--mode={mode}"] if MODE.fullmatch(mode or "") else []
    try:
        if not MODE.fullmatch(mode):
            raise InvalidRuntime("unsupported GNOME Shell mode")
        root, _ = validate_runtime(manifest, run=run, owner_uid=owner_uid)
        guard = load_sibling(root, "startup_guard")
        guard.consume(state, root, mode, environ=env)
        custom = loader_environment(root, env)
        custom["SOCKET_STREAM_CAPTURE_EXCLUDE"] = "1"
        execve(str(root / SHELL), [str(root / SHELL), *arguments], custom)
    except Exception as exc:
        # Includes an exec failure after consumption: the pending latch persists,
        # and a stock READY notification cannot promote it to a custom ready state.
        detail = (f"custom runtime validation failed: {exc}" if isinstance(exc, InvalidRuntime)
                  else "custom runtime startup was refused or could not complete")
        print(f"SocketStream Mutter: {detail}; starting stock GNOME Shell.", file=sys.stderr)
        return execve("/usr/bin/gnome-shell", ["/usr/bin/gnome-shell", *arguments], env)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--guard-state", type=Path)
    parser.add_argument("--mode")
    args = parser.parse_args(argv)
    if args.launch:
        if args.guard_state is None or not args.guard_state.is_absolute() or not MODE.fullmatch(args.mode or ""):
            parser.error("launch requires an absolute guard state and a plain Shell mode")
        launch_runtime(args.manifest, args.guard_state, args.mode)
        return 1  # exec never returns in production
    try:
        validate_runtime(args.manifest)
        return 0
    except InvalidRuntime as exc:
        print(f"SocketStream Mutter: {exc}", file=sys.stderr)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print("SocketStream Mutter: runtime validation could not complete", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
