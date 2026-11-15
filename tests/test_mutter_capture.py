"""Tests for the custom Mutter runtime and its managed systemd activation.

Every test drives a temporary runtime manifest, a fake ``/proc`` tree, and a
fake ``systemd``/``gdbus``/``dpkg-query``.  Nothing reads the host's real
``/opt`` runtime, ``/proc``, session bus, or user services.

The runtime fixtures follow the native contract in
``native/mutter-capture-filter/validate_runtime.py``: canonical content-bound
``runtime_id``, the exact eight payload files, the six pinned packages, and a
trusted owner.  ``OWNER`` stands in for root, which a test cannot create.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "apps" / "socket_stream_desktop" / "ui"
if str(UI) not in sys.path:
    sys.path.insert(0, str(UI))

import capture_rules as rules  # noqa: E402
import mutter_capture as mc  # noqa: E402
import overlay_guard as guard  # noqa: E402

# The fake GNOME Shell runs as this uid inside the fake /proc tree.
UID = 1000
# The runtime files belong to the real user; production requires root.
OWNER = os.getuid()
SESSION_ID = "2"
PID = 4242
UNIT = "org.gnome.Shell@ubuntu.service"
MODE = "ubuntu"
VERSION = "50.1-0ubuntu2.5"
LIB_NAMES = (
    "libmutter-18.so.0",
    "libmutter-clutter-18.so.0",
    "libmutter-cogl-18.so.0",
    "libmutter-mtk-18.so.0",
)
TYPELIB_NAMES = (
    "Meta-18.typelib",
    "Clutter-18.typelib",
    "Cogl-18.typelib",
    "Mtk-18.typelib",
)
PACKAGES = {
    "gnome-shell": VERSION,
    "gnome-shell-common": VERSION,
    "libmutter-18-0": VERSION,
    "mutter-common": VERSION,
    "mutter-common-bin": VERSION,
    "gir1.2-mutter-18": VERSION,
}

STOCK_EXEC = (
    "{ path=/usr/bin/gnome-shell ; argv[]=/usr/bin/gnome-shell --mode=%s ; "
    "ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; "
    "code=(null) ; status=0/0 }"
)
OURS_EXEC = (
    "{ path=/bin/sh ; argv[]=/bin/sh -c %s ; ignore_errors=no ; "
    "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
)


def which_all(*names):
    available = {f"/usr/bin/{name}" for name in names}
    return lambda name: f"/usr/bin/{name}" if f"/usr/bin/{name}" in available else None


def result(argv, code=0, out="", err=""):
    return subprocess.CompletedProcess(list(argv), code, out, err)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def build_runtime(base, *, version=VERSION, shell_version=None, tamper=None,
                  drop=(), extra_lib_dir=(), extra_files=(), provenance=(),
                  libraries=None, extra_packages=(), document_patch=None,
                  name="runtime-v1", schema=2, shell="gnome-shell",
                  non_executable=()):
    """Create a small but contract-faithful schema-2 runtime directory."""
    root = Path(base) / name
    for directory in ("lib", "typelib"):
        (root / directory).mkdir(parents=True, exist_ok=True)
        (root / directory).chmod(0o755)
    files = {}

    def add(rel, payload, mode=0o644):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(mode)
        files[rel] = path

    for name in LIB_NAMES:
        add(f"lib/{name}", b"\x7fELF socket_stream " + name.encode())
    for name in TYPELIB_NAMES:
        add(f"typelib/{name}", b"typelib " + name.encode())
    add("launch-gnome-shell", b"#!/bin/sh\nexit 1\n", mode=0o755)
    add("gnome-shell", b"#!/bin/sh\nexit 1\n", mode=0o755)
    add("validate_runtime.py", b"# staged by the native tools\n")
    add("startup_probe.py", b"# staged by the native tools\n")
    add("startup_guard.py", b"# staged by the native tools\n")
    for rel in provenance:
        add(rel, f"provenance for {rel}".encode())
    for name in extra_lib_dir:
        add(f"lib/{name}", b"unrelated")
    for rel in extra_files:
        add(rel, b"unexpected")

    for rel in drop:
        (root / rel).unlink()
        files.pop(rel, None)
    for rel in non_executable:
        (root / rel).chmod(0o644)
    if tamper:
        path = root / tamper
        path.write_bytes(path.read_bytes() + b"tampered")

    packages = {name: version for name in PACKAGES}
    packages["gnome-shell"] = shell_version or version
    packages.update(extra_packages)
    document = {
        "schema": schema,
        "shell": shell,
        "source_version": version,
        "source_sha256": ("%s" % sha256(files["source.dsc"].read_bytes()).hexdigest()
                          if "source.dsc" in files else "a" * 64),
        "patch_sha256": (sha256(files["capture.patch"].read_bytes()).hexdigest()
                         if "capture.patch" in files else "b" * 64),
        "packages": packages,
        "files": {
            rel: {"sha256": sha256(path.read_bytes()).hexdigest(),
                  "size": path.stat().st_size}
            for rel, path in files.items()
        },
        "libraries": list(mc.LIBRARIES) if libraries is None else libraries,
        "library_dir": "lib",
        "typelib_dir": "typelib",
    }
    document["runtime_id"] = mc.runtime_identifier(document)
    if document_patch is not None:
        document_patch(document)
    (root / "runtime.json").write_text(json.dumps(document), encoding="utf-8")
    (root / "runtime.json").chmod(0o644)
    root.chmod(0o755)
    return root


def rewrite_manifest(root, **changes):
    """Apply a raw edit and keep ``runtime_id`` canonical unless it is changed."""
    path = Path(root) / "runtime.json"
    document = json.loads(path.read_text())
    document.update(changes)
    if "runtime_id" not in changes:
        document["runtime_id"] = mc.runtime_identifier(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o644)


def build_proc(base, *, pid=PID, uid=UID, unit=UNIT, environ=None, mapped=(),
               deleted=(), write_environ=True, comm="gnome-shell"):
    proc = Path(base) / "proc"
    directory = proc / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "comm").write_text(comm + "\n", encoding="utf-8")
    (directory / "status").write_text(
        f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n", encoding="utf-8")
    (directory / "cgroup").write_text(
        f"0::/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/{unit}\n",
        encoding="utf-8")
    environment = {"XDG_SESSION_ID": SESSION_ID, "WAYLAND_DISPLAY": "wayland-0"}
    if environ is not None:
        environment = environ
    if write_environ:
        (directory / "environ").write_bytes(b"".join(
            f"{key}={value}".encode() + b"\0" for key, value in environment.items()))
    lines = []
    address = 0x7F0000000000
    for path in mapped:
        info = Path(path).stat()
        device = mc._dev_text(info.st_dev)
        suffix = " (deleted)" if str(path) in {str(item) for item in deleted} else ""
        lines.append(
            f"{address:x}-{address + 0x1000:x} r-xp 00000000 {device} "
            f"{info.st_ino} {path}{suffix}")
        address += 0x1000
    (directory / "maps").write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8")
    return proc


_UNREADABLE = object()


class FakeSystemd:
    """Scripted systemctl/gdbus/dpkg-query plus the startup probe and guard.

    The drop-in is read from disk, so the effective ExecStart and the two
    lifecycle hooks follow whatever the app wrote.  The probe and guard are
    scripted, and a successful arm/disarm writes the guard's own JSON record so
    status sees the same phase the real helper would have left behind.
    """

    def __init__(self, *, config_home=None, unit=UNIT, mode=MODE, pid=PID,
                 main_pid=None, load_state="loaded", packages=_UNREADABLE,
                 package_status="install ok installed", extra_package_lines=(),
                 reload_ok=True, reload_fail_at=(), apply_dropin=True,
                 exec_override=None, dbus_pid=None, probe_ok=True, probe_exit=0,
                 probe_result=None, guard_arm_phase="armed", guard_arm_ok=True,
                 guard_arm_exit=0, guard_disarm_phase="blocked",
                  guard_disarm_ok=True, write_guard=True, post_hooks=True,
                  extra_exec_stop_post="", service_type="notify", notify_access="main"):
        self.config_home = config_home
        self.unit = unit
        self.mode = mode
        self.pid = pid
        self.main_pid = pid if main_pid is None else main_pid
        self.load_state = load_state
        # ``packages=None`` must mean "dpkg-query failed", never "use defaults".
        self.packages = dict(PACKAGES) if packages is _UNREADABLE else packages
        self.package_status = package_status
        self.extra_package_lines = tuple(extra_package_lines)
        self.reload_ok = reload_ok
        self.reload_fail_at = set(reload_fail_at)
        self.reloads = 0
        self.apply_dropin = apply_dropin
        self.exec_override = exec_override
        self.dbus_pid = pid if dbus_pid is None else dbus_pid
        self.probe_ok = probe_ok
        self.probe_exit = probe_exit
        self.probe_result = probe_result
        self.guard_arm_phase = guard_arm_phase
        self.guard_arm_ok = guard_arm_ok
        self.guard_arm_exit = guard_arm_exit
        self.guard_disarm_phase = guard_disarm_phase
        self.guard_disarm_ok = guard_disarm_ok
        self.write_guard = write_guard
        self.post_hooks = post_hooks
        self.extra_exec_stop_post = extra_exec_stop_post
        self.service_type = service_type
        self.notify_access = notify_access
        self.probes = 0
        self.arms = 0
        self.disarms = 0
        self.calls = []

    def called(self, needle):
        return any(needle in " ".join(call) for call in self.calls)

    def _dropin_text(self):
        if self.config_home is None or not self.apply_dropin:
            return ""
        path = mc.dropin_path(self.config_home, self.unit)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _exec_start(self):
        if self.exec_override is not None:
            return self.exec_override
        text = self._dropin_text()
        match = re.search(r"^ExecStart=/bin/sh -c '(.*)'$", text, re.MULTILINE)
        if match:
            return OURS_EXEC % match.group(1)
        return STOCK_EXEC % self.mode

    def _exec_post(self, key):
        if not self.post_hooks:
            return self.extra_exec_stop_post if key == "ExecStopPost" else ""
        entries = []
        for line in self._dropin_text().splitlines():
            if line.startswith(f"{key}="):
                value = line.split("=", 1)[1]
                ignore = "yes" if value.startswith("-") else "no"
                argv = value[1:] if value.startswith("-") else value
                entries.append(
                    f"{{ path={argv.split()[0]} ; argv[]={argv} ; ignore_errors={ignore} ; "
                    "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }")
        if key == "ExecStopPost" and self.extra_exec_stop_post:
            entries.append(self.extra_exec_stop_post)
        return " ; ".join(entries)

    def _write_state(self, argv, phase):
        if not self.write_guard:
            return
        options = dict(zip(argv[4::2], argv[5::2]))
        state = options.get("--state")
        if not state:
            return
        path = Path(state)
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.parent.chmod(0o700)
            path.write_text(json.dumps({
                "schema": 1, "runtime": options.get("--runtime", ""),
                "mode": options.get("--mode", ""), "phase": phase,
            }))
            path.chmod(0o600)
        except OSError:
            pass

    def __call__(self, argv, timeout=15, env=None):
        command = [str(part) for part in argv]
        self.calls.append(command)
        name = os.path.basename(command[0])
        joined = " ".join(command)
        if "startup_probe.py" in joined:
            self.probes += 1
            if self.probe_result is not None:
                return result(command, self.probe_exit, self.probe_result, "")
            body = json.dumps({"ok": bool(self.probe_ok)})
            return result(command, self.probe_exit, body, "")
        if "startup_guard.py" in joined:
            action = command[3] if len(command) > 3 else ""
            if action == "arm":
                self.arms += 1
                if not self.guard_arm_ok:
                    return result(command, self.guard_arm_exit or 1, "", "refused")
                self._write_state(command, self.guard_arm_phase)
                options = dict(zip(command[4::2], command[5::2]))
                return result(command, self.guard_arm_exit, json.dumps({
                    "schema": 1, "runtime": options["--runtime"], "mode": options["--mode"],
                    "phase": self.guard_arm_phase,
                }), "")
            if action == "disarm":
                self.disarms += 1
                if not self.guard_disarm_ok:
                    return result(command, 1, "", "refused")
                self._write_state(command, self.guard_disarm_phase)
                return result(command, 0, json.dumps({
                    "schema": 1, "runtime": "", "mode": "", "phase": self.guard_disarm_phase,
                }), "")
            return result(command, 0, json.dumps({
                "schema": 1, "runtime": "", "mode": "", "phase": "armed"}), "")
        if name == "gdbus":
            if self.dbus_pid is None:
                return result(command, 1, "", "No such name")
            return result(command, 0, f"(uint32 {self.dbus_pid},)\n")
        if name == "dpkg-query":
            if self.packages is None:
                return result(command, 1, "", "dpkg-query: error")
            body = "".join(
                f"{package}\t{version}\t{self.package_status}\n"
                for package, version in self.packages.items())
            body += "".join(f"{line}\n" for line in self.extra_package_lines)
            return result(command, 0, body)
        if "--user" in command and "daemon-reload" in command:
            self.reloads += 1
            if not self.reload_ok or self.reloads in self.reload_fail_at:
                return result(command, 1, "", "Failed to reload")
            return result(command, 0)
        if "--user" in command and "show" in command:
            if self.load_state == "not-found":
                return result(command, 1, "", "Unit not found")
            body = "\n".join([
                f"MainPID={self.main_pid}",
                f"LoadState={self.load_state}",
                f"ExecStart={self._exec_start()}",
                f"ExecStartPost={self._exec_post('ExecStartPost')}",
                f"ExecStopPost={self._exec_post('ExecStopPost')}",
                "FragmentPath=/usr/lib/systemd/user/org.gnome.Shell@.service",
                f"Type={self.service_type}", f"NotifyAccess={self.notify_access}",
            ]) + "\n"
            return result(command, 0, body)
        return result(command, 0)


class Base(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.base = Path(self._temp.name).resolve()
        self.config_home = self.base / "config"
        self.config_home.mkdir()
        self.runtime = build_runtime(self.base)
        self.manifest = self.runtime / "runtime.json"
        self.root = os.path.realpath(self.runtime)
        self.libs = [self.runtime / "lib" / name for name in LIB_NAMES]

    def environ(self, **extra):
        values = {
            "XDG_CONFIG_HOME": str(self.config_home),
            "SOCKET_STREAM_MUTTER_RUNTIME": str(self.manifest),
            "XDG_SESSION_ID": SESSION_ID,
            "XDG_SESSION_TYPE": "wayland",
            "WAYLAND_DISPLAY": "wayland-0",
            "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
        }
        values.update({key: value for key, value in extra.items() if value is not None})
        for key, value in extra.items():
            if value is None:
                values.pop(key, None)
        return values

    def load(self, environ=None):
        """Validate through the app with the test-owned runtime trusted."""
        return mc.load_runtime(environ or self.environ(), owner_uid=OWNER)

    def systemd(self, **kwargs):
        return FakeSystemd(config_home=self.config_home, **kwargs)

    def guard_state(self, root=None, mode=MODE):
        """The absolute guard state path the schema-2 override pins."""
        return str(mc.guard_state_path(
            self.config_home, self.root if root is None else root, mode))

    def proc(self, **kwargs):
        """Fake /proc with the shell mapping the fixture runtime."""
        kwargs.setdefault("mapped", self.libs)
        kwargs.setdefault(
            "environ",
            {"XDG_SESSION_ID": SESSION_ID, "SOCKET_STREAM_CAPTURE_EXCLUDE": "1",
             "WAYLAND_DISPLAY": "wayland-0"},
        )
        return build_proc(self.base, **kwargs)

    def exclusion(self, *, runner=None, proc_root=None, which=None, session=None,
                  environ=None):
        return rules.CaptureExclusion(
            session or guard.SessionInfo(
                guard.SESSION_WAYLAND, "gnome", "gnome", False, True),
            environ=environ if environ is not None else self.environ(),
            home=self.config_home,
            runner=runner if runner is not None else self.systemd(),
            which=which if which is not None else which_all(
                "gdbus", "systemctl", "dpkg-query"),
            proc_root=proc_root if proc_root is not None else self.proc(),
            uid=UID,
            runtime_owner_uid=OWNER,
        )


# --------------------------------------------------------------------------
# unit detection
# --------------------------------------------------------------------------
class UnitDetection(Base):
    def test_the_ubuntu_instance_is_used_verbatim(self):
        shell = mc.detect_shell(
            runner=self.systemd(), which=which_all("gdbus", "systemctl"),
            proc_root=self.proc(), uid=UID, environ=self.environ())
        self.assertTrue(shell.ok, shell.reason)
        self.assertEqual(shell.unit, "org.gnome.Shell@ubuntu.service")
        self.assertEqual(shell.mode, "ubuntu")
        self.assertEqual(shell.exec_kind, "stock")
        self.assertEqual(shell.managed_root, "")

    def test_a_wayland_instance_is_equally_supported(self):
        runner = self.systemd(unit="org.gnome.Shell@wayland.service", mode="wayland")
        shell = mc.detect_shell(
            runner=runner, which=which_all("gdbus", "systemctl"),
            proc_root=self.proc(unit="org.gnome.Shell@wayland.service", mapped=(),
                                environ={"XDG_SESSION_ID": SESSION_ID}),
            uid=UID, environ=self.environ())
        self.assertTrue(shell.ok, shell.reason)
        self.assertEqual(shell.mode, "wayland")

    def test_a_mismatched_main_pid_is_refused(self):
        shell = mc.detect_shell(
            runner=self.systemd(main_pid=9999), which=which_all("gdbus", "systemctl"),
            proc_root=self.proc(), uid=UID, environ=self.environ())
        self.assertFalse(shell.ok)
        self.assertIn("does not run this shell", shell.reason)

    def test_an_unfamiliar_exec_start_is_refused(self):
        shell = mc.detect_shell(
            runner=self.systemd(exec_override=STOCK_EXEC % "x11"),
            which=which_all("gdbus", "systemctl"), proc_root=self.proc(),
            uid=UID, environ=self.environ())
        self.assertFalse(shell.ok)
        self.assertIn("unfamiliar ExecStart", shell.reason)

    def test_another_users_shell_is_refused(self):
        shell = mc.detect_shell(
            runner=self.systemd(), which=which_all("gdbus", "systemctl"),
            proc_root=self.proc(uid=UID + 1), uid=UID, environ=self.environ())
        self.assertFalse(shell.ok)
        self.assertIn("not owned by this user", shell.reason)

    def test_a_non_gnome_shell_pid_is_refused(self):
        shell = mc.detect_shell(
            runner=self.systemd(), which=which_all("gdbus", "systemctl"),
            proc_root=self.proc(comm="gnome-terminal-"), uid=UID,
            environ=self.environ())
        self.assertFalse(shell.ok)

    def test_a_session_scope_shell_has_no_unit(self):
        shell = mc.detect_shell(
            runner=self.systemd(), which=which_all("gdbus", "systemctl"),
            proc_root=self.proc(unit="session-2.scope"), uid=UID,
            environ=self.environ())
        self.assertFalse(shell.ok)
        self.assertIn("unfamiliar", shell.reason)

    def test_without_gdbus_one_unique_shell_is_accepted(self):
        proc = self.proc()
        shell = mc.detect_shell(
            runner=self.systemd(), which=which_all("systemctl"),
            proc_root=proc, uid=UID, environ=self.environ())
        self.assertTrue(shell.ok, shell.reason)
        self.assertEqual(shell.source, "proc")

    def test_without_gdbus_two_shells_are_ambiguous(self):
        proc = self.proc()
        other = proc / "5150"
        other.mkdir()
        (other / "comm").write_text("gnome-shell\n")
        (other / "status").write_text(f"Uid:\t{UID}\t{UID}\t{UID}\t{UID}\n")
        (other / "environ").write_bytes(b"XDG_SESSION_ID=2\0")
        shell = mc.detect_shell(
            runner=self.systemd(), which=which_all("systemctl"),
            proc_root=proc, uid=UID, environ=self.environ())
        self.assertFalse(shell.ok)
        self.assertIn("several GNOME Shell", shell.reason)

    def test_exec_kind_distinguishes_stock_and_managed(self):
        self.assertEqual(mc.exec_kind(STOCK_EXEC % "ubuntu", "ubuntu"), "stock")
        state = "/some/state.json"
        script = mc.managed_script("/opt/socket_stream-mutter/current", "ubuntu", state)
        self.assertEqual(mc.exec_kind(OURS_EXEC % script, "ubuntu"), "ours")
        self.assertEqual(mc.managed_root(OURS_EXEC % script, "ubuntu"),
                         "/opt/socket_stream-mutter/current")
        self.assertEqual(mc.exec_kind(OURS_EXEC % script, "wayland"), "")
        self.assertEqual(mc.exec_kind(STOCK_EXEC % "ubuntu", "wayland"), "")
        self.assertEqual(mc.exec_kind("", "ubuntu"), "")
        self.assertEqual(mc.exec_kind("garbage", "ubuntu"), "")

    def test_the_installed_activation_format_is_recognised(self):
        # The exact text the reviewed deployment wrote on the real host (legacy schema1).
        pinned = ("/opt/socket_stream-mutter/50.1-0ubuntu2.5."
                  "socket-stream.e0130fa12159ba51")
        rendered = mc._render_legacy_dropin(pinned, "ubuntu")
        self.assertTrue(mc.is_managed_content(rendered))
        parsed = mc.parse_managed_content(rendered)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.root, pinned)
        self.assertEqual(parsed.mode, "ubuntu")
        self.assertEqual(parsed.format, "legacy")
        self.assertEqual(parsed.state, "")
        effective = (
            "{ path=/bin/sh ; argv[]=/bin/sh -c if test -x "
            f"{pinned}/launch-gnome-shell; then exec {pinned}/launch-gnome-shell "
            "--mode=ubuntu; fi; exec /usr/bin/gnome-shell --mode=ubuntu ; "
            "ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; "
            "code=(null) ; status=0/0 }"
        )
        self.assertEqual(mc.exec_kind(effective, "ubuntu"), "ours")
        self.assertEqual(mc.managed_root(effective, "ubuntu"), pinned)


# --------------------------------------------------------------------------
# runtime manifest
# --------------------------------------------------------------------------
class RuntimeManifest(Base):
    def test_a_valid_runtime_is_deployed(self):
        runtime = self.load()
        self.assertTrue(runtime.deployed, runtime.reason)
        document = json.loads(self.manifest.read_text())
        self.assertEqual(runtime.runtime_id, mc.runtime_identifier(document))
        self.assertEqual(len(runtime.libraries), 4)
        self.assertEqual(runtime.root, self.root)
        self.assertTrue(runtime.launcher.endswith("launch-gnome-shell"))

    def test_a_missing_runtime_is_not_deployed(self):
        runtime = self.load(self.environ(**{
            "SOCKET_STREAM_MUTTER_RUNTIME": str(self.base / "absent.json")}))
        self.assertFalse(runtime.deployed)
        self.assertIn("not installed", runtime.reason)

    def test_a_relative_override_is_refused(self):
        runtime = mc.load_runtime({"SOCKET_STREAM_MUTTER_RUNTIME": "runtime.json"})
        self.assertFalse(runtime.deployed)
        self.assertIn("absolute", runtime.reason)

    def test_a_manifest_not_named_runtime_json_is_refused(self):
        other = self.runtime / "manifest.json"
        other.write_text(self.manifest.read_text())
        runtime = self.load(self.environ(**{
            "SOCKET_STREAM_MUTTER_RUNTIME": str(other)}))
        self.assertFalse(runtime.deployed)
        self.assertIn("runtime.json", runtime.reason)

    def test_tampered_content_is_not_deployed(self):
        (self.runtime / "lib" / LIB_NAMES[0]).write_bytes(b"swapped library")
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("does not match", runtime.reason)

    def test_tampered_content_is_not_hidden_by_the_hash_cache(self):
        self.assertTrue(self.load().deployed)
        path = self.runtime / "lib" / LIB_NAMES[0]
        path.write_bytes(b"replaced after the first read")
        self.assertFalse(self.load().deployed)

    def test_a_same_size_rewrite_with_a_restored_mtime_is_detected(self):
        # A cached digest keyed only by (dev, inode, size, mtime) would serve
        # the old value here; the ctime part of the key prevents that.
        self.assertTrue(self.load().deployed)
        path = self.runtime / "lib" / LIB_NAMES[0]
        info = path.stat()
        payload = path.read_bytes()
        path.write_bytes(bytes(len(payload)))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        after = path.stat()
        self.assertEqual(after.st_mtime_ns, info.st_mtime_ns)
        self.assertEqual(after.st_size, info.st_size)
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("does not match", runtime.reason)

    def test_a_world_writable_file_is_refused(self):
        (self.runtime / "lib" / LIB_NAMES[0]).chmod(0o666)
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("unsafe ownership", runtime.reason)

    def test_a_symlinked_payload_is_refused(self):
        payload = self.runtime / "lib" / LIB_NAMES[0]
        copy = self.base / "copy.so"
        copy.write_bytes(payload.read_bytes())
        payload.unlink()
        payload.symlink_to(copy)
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("unsafe ownership, permissions, or type", runtime.reason)

    def test_the_library_directory_must_be_physical_for_guard_mapping_parity(self):
        libraries = self.runtime / "lib"
        other = self.base / "same-library-inodes"
        libraries.rename(other)
        libraries.symlink_to(other, target_is_directory=True)
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("runtime lib/ has unsafe ownership or permissions", runtime.reason)

    def test_an_unexpected_file_entry_is_refused(self):
        rewrite_manifest(self.runtime, files=dict(
            json.loads(self.manifest.read_text())["files"],
            **{"notes.txt": {"sha256": "c" * 64, "size": 1}}))
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("file set", runtime.reason)

    def test_a_missing_required_entry_is_refused(self):
        files = json.loads(self.manifest.read_text())["files"]
        files.pop(mc.TYPELIBS[0])
        rewrite_manifest(self.runtime, files=files)
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("file set", runtime.reason)

    def test_a_missing_launcher_is_refused(self):
        (self.runtime / "launch-gnome-shell").unlink()
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("launch-gnome-shell", runtime.reason)

    def test_a_foreign_library_in_lib_is_refused(self):
        # The manifest stays valid, so only the loader-directory check can
        # notice the extra entry: a library directory must hold exactly its
        # four declared files.
        (self.runtime / "lib" / "libc.so.6").write_bytes(b"unrelated")
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("unexpected files", runtime.reason)

    def test_a_single_library_manifest_is_refused(self):
        rewrite_manifest(self.runtime, libraries=[mc.LIBRARIES[0]])
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("library layout", runtime.reason)

    def test_a_relocated_library_directory_is_refused(self):
        rewrite_manifest(self.runtime, library_dir="libraries")
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("library layout", runtime.reason)

    def test_an_incomplete_package_manifest_is_refused(self):
        packages = dict(PACKAGES)
        packages.pop("libmutter-18-0")
        rewrite_manifest(self.runtime, packages=packages)
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("package contract", runtime.reason)

    def test_an_extra_package_is_refused(self):
        packages = dict(PACKAGES, **{"mutter-dev": VERSION})
        rewrite_manifest(self.runtime, packages=packages)
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("package contract", runtime.reason)

    def test_source_version_must_equal_the_mutter_pins(self):
        root = build_runtime(self.base / "skew")
        rewrite_manifest(root, source_version="50.2-0ubuntu1")
        runtime = self.load(self.environ(**{
            "SOCKET_STREAM_MUTTER_RUNTIME": str(root / "runtime.json")}))
        self.assertFalse(runtime.deployed)
        self.assertIn("versions differ", runtime.reason)

    def test_a_boolean_schema_is_refused(self):
        # ``True == 1`` in Python; the native contract requires an int.
        rewrite_manifest(self.runtime, schema=True)
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("schema", runtime.reason)

    def test_an_unknown_schema_is_refused(self):
        rewrite_manifest(self.runtime, schema=3)
        self.assertFalse(self.load().deployed)

    def test_a_duplicate_json_field_is_refused(self):
        text = self.manifest.read_text()
        doubled = text.replace('"schema": 2', '"schema": 2, "schema": 2', 1)
        self.assertNotEqual(text, doubled)
        self.manifest.write_text(doubled)
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("repeats a field", runtime.reason)

    def test_an_oversized_manifest_is_refused(self):
        document = json.loads(self.manifest.read_text())
        document["padding"] = "x" * mc.MAX_MANIFEST_BYTES
        self.manifest.write_text(json.dumps(document))
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("too large", runtime.reason)

    def test_a_non_canonical_runtime_id_is_refused(self):
        rewrite_manifest(self.runtime, runtime_id="50.1-0ubuntu2.5+sse1")
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("does not match its manifest", runtime.reason)

    def test_an_invalid_source_version_is_refused(self):
        rewrite_manifest(self.runtime, source_version="not a version")
        self.assertFalse(self.load().deployed)

    def test_a_provenance_hash_mismatch_is_refused(self):
        root = build_runtime(self.base / "prov", provenance=("source.dsc",))
        rewrite_manifest(root, source_sha256="c" * 64)
        runtime = self.load(self.environ(**{
            "SOCKET_STREAM_MUTTER_RUNTIME": str(root / "runtime.json")}))
        self.assertFalse(runtime.deployed)
        self.assertIn("provenance hash", runtime.reason)

    def test_optional_provenance_files_are_accepted(self):
        root = build_runtime(self.base / "prov2",
                             provenance=("source.dsc", "capture.patch"))
        runtime = self.load(self.environ(**{
            "SOCKET_STREAM_MUTTER_RUNTIME": str(root / "runtime.json")}))
        self.assertTrue(runtime.deployed, runtime.reason)

    def test_an_unsafe_directory_is_refused(self):
        self.runtime.chmod(0o777)
        runtime = self.load()
        self.assertFalse(runtime.deployed)
        self.assertIn("unsafe ownership", runtime.reason)

    def test_a_package_mismatch_is_reported(self):
        runtime = self.load()
        mismatched = dict(PACKAGES, **{"gnome-shell": "50.1-0ubuntu2.6"})
        ok, detail = mc.packages_match(
            runtime.packages, runner=self.systemd(packages=mismatched),
            which=which_all("dpkg-query"))
        self.assertFalse(ok)
        self.assertIn("50.1-0ubuntu2.6", detail)

    def test_every_package_must_be_installed(self):
        runtime = self.load()
        packages = dict(PACKAGES)
        packages.pop("mutter-common")
        ok, detail = mc.packages_match(
            runtime.packages, runner=self.systemd(packages=packages),
            which=which_all("dpkg-query"))
        self.assertIsNone(ok)
        self.assertIn("incomplete", detail)

    def test_a_duplicate_package_line_is_refused(self):
        runtime = self.load()
        ok, detail = mc.packages_match(
            runtime.packages,
            runner=self.systemd(extra_package_lines=(
                "libmutter-18-0\t50.1-0ubuntu2.5\tinstall ok installed",)),
            which=which_all("dpkg-query"))
        self.assertIsNone(ok)
        self.assertIn("unknown", detail)

    def test_a_non_installed_status_is_refused(self):
        runtime = self.load()
        ok, detail = mc.packages_match(
            runtime.packages,
            runner=self.systemd(package_status="deinstall ok config-files"),
            which=which_all("dpkg-query"))
        self.assertIsNone(ok)
        self.assertIn("unknown", detail)

    def test_unreadable_packages_are_not_success(self):
        runtime = self.load()
        ok, _detail = mc.packages_match(
            runtime.packages, runner=self.systemd(packages=None),
            which=which_all("dpkg-query"))
        self.assertIsNone(ok)
        ok, _detail = mc.packages_match(
            runtime.packages, runner=self.systemd(), which=which_all())
        self.assertIsNone(ok)


# --------------------------------------------------------------------------
# native contract parity
# --------------------------------------------------------------------------
class NativeParity(Base):
    def native(self):
        path = ROOT / "native" / "mutter-capture-filter" / "validate_runtime.py"
        if not path.is_file():
            self.skipTest("the native validator is not present in this checkout")
        spec = importlib.util.spec_from_file_location("native_validate_runtime", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_contract_constants_are_identical(self):
        native = self.native()
        self.assertEqual(mc.SCHEMA, native.SCHEMA)
        self.assertEqual(mc.SHELL, native.SHELL)
        self.assertEqual(mc.EXECUTABLES, native.EXECUTABLES)
        self.assertEqual(mc.LIBRARIES, native.LIBRARIES)
        self.assertEqual(mc.TYPELIBS, native.TYPELIBS)
        self.assertEqual(mc.SCRIPTS, native.SCRIPTS)
        self.assertEqual(mc.PROVENANCE, native.PROVENANCE)
        self.assertEqual(tuple(mc.REQUIRED_PACKAGES), native.PACKAGES)
        self.assertEqual(mc.MAX_MANIFEST_BYTES, 65536)
        for ours, name in ((mc.IDENTIFIER, "IDENTIFIER"), (mc.VERSION, "VERSION"),
                           (mc.SHA256, "SHA256")):
            self.assertEqual(ours.pattern, getattr(native, name).pattern)

    def test_the_runtime_identifier_is_computed_the_same_way(self):
        native = self.native()
        document = json.loads(self.manifest.read_text())
        self.assertEqual(mc.runtime_identifier(document),
                         native.runtime_identifier(document))
        self.assertEqual(self.load().runtime_id,
                         native.runtime_identifier(document))

    def test_the_app_accepts_what_the_native_contract_requires(self):
        native = self.native()
        document = json.loads(self.manifest.read_text())
        # The pure (non-executing) half of the native contract, asserted here
        # against the same document the app accepts.
        self.assertEqual(document["library_dir"], "lib")
        self.assertEqual(document["typelib_dir"], "typelib")
        self.assertEqual(set(document["packages"]), set(native.PACKAGES))
        self.assertLessEqual(
            set(native.LIBRARIES + native.TYPELIBS + native.SCRIPTS),
            set(document["files"]))
        self.assertEqual(document["runtime_id"], native.runtime_identifier(document))
        self.assertEqual(os.path.basename(self.manifest), "runtime.json")
        self.assertTrue(self.load().deployed)


# --------------------------------------------------------------------------
# live compositor evidence
# --------------------------------------------------------------------------
class LiveEvidence(Base):
    def live(self, *, mapped=None, environ=None, deleted=()):
        runtime = self.load()
        proc = self.proc(
            mapped=self.libs if mapped is None else mapped,
            deleted=deleted,
            environ=environ if environ is not None else {
                "XDG_SESSION_ID": SESSION_ID,
                "SOCKET_STREAM_CAPTURE_EXCLUDE": "1"},
        )
        shell = mc.detect_shell(
            runner=self.systemd(), which=which_all("gdbus", "systemctl"),
            proc_root=proc, uid=UID, environ=self.environ())
        self.assertTrue(shell.ok, shell.reason)
        return mc.read_live_state(shell, runtime, proc)

    def test_all_libraries_and_the_opt_in_are_active(self):
        self.assertEqual(self.live().state, "active")

    def test_the_opt_in_must_be_exactly_one(self):
        state = self.live(environ={
            "XDG_SESSION_ID": SESSION_ID, "SOCKET_STREAM_CAPTURE_EXCLUDE": "0"})
        self.assertEqual(state.state, "inactive")
        self.assertFalse(state.active)

    def test_a_missing_opt_in_is_inactive_not_active(self):
        state = self.live(environ={"XDG_SESSION_ID": SESSION_ID})
        self.assertEqual(state.state, "inactive")
        self.assertIn(mc.OPT_IN_ENVIRON, state.detail)

    def test_no_custom_library_mapped_is_missing(self):
        state = self.live(mapped=[])
        self.assertEqual(state.state, "missing")
        self.assertFalse(state.active)

    def test_one_library_from_the_system_is_mixed(self):
        stock = self.base / "usr/lib/libmutter-cogl-18.so.0"
        stock.parent.mkdir(parents=True, exist_ok=True)
        stock.write_bytes(b"stock library")
        mapped = [path for path in self.libs if path.name != LIB_NAMES[2]] + [stock]
        state = self.live(mapped=mapped)
        self.assertEqual(state.state, "mixed")
        self.assertIn("outside the validated runtime", state.detail)

    def test_a_split_mapping_of_one_library_is_mixed(self):
        # The same file name mapped from the runtime *and* from the system:
        # the two segments disagree, so activation cannot be claimed.
        stock = self.base / "usr/lib/libmutter-18.so.0"
        stock.parent.mkdir(parents=True, exist_ok=True)
        stock.write_bytes(b"stock library")
        state = self.live(mapped=self.libs + [stock])
        self.assertEqual(state.state, "mixed")
        self.assertIn("more than one directory", state.detail)

    def test_an_unexpected_family_member_is_mixed(self):
        other = self.base / "usr/lib/libmutter-test-18.so.0"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"surprise")
        state = self.live(mapped=self.libs + [other])
        self.assertEqual(state.state, "mixed")
        self.assertIn("not part of the validated runtime", state.detail)

    def test_a_deleted_mapping_is_stale(self):
        state = self.live(deleted=[str(self.libs[0])])
        self.assertEqual(state.state, "stale")
        self.assertIn("removed from disk", state.detail)

    def test_a_shell_that_never_loaded_the_runtime_is_stock(self):
        stock = self.base / "usr/lib/libmutter-18.so.0"
        stock.parent.mkdir(parents=True, exist_ok=True)
        stock.write_bytes(b"stock library")
        state = self.live(mapped=[stock])
        self.assertEqual(state.state, "stock")
        self.assertFalse(state.active)
        self.assertIn("system Mutter libraries", state.detail)

    def test_a_partial_load_is_not_active(self):
        state = self.live(mapped=self.libs[:1])
        self.assertEqual(state.state, "missing")
        self.assertIn(LIB_NAMES[1], state.detail)

    def test_a_replaced_file_behind_the_mapping_is_stale(self):
        runtime = self.load()
        target = self.libs[1]
        # Replacing the file gives the mapping a different inode than the
        # build that was validated, which is exactly the stale case.
        target.unlink()
        target.write_bytes(b"a different build than the manifest records")
        proc = self.proc()
        shell = mc.detect_shell(
            runner=self.systemd(), which=which_all("gdbus", "systemctl"),
            proc_root=proc, uid=UID, environ=self.environ())
        state = mc.read_live_state(shell, runtime, proc)
        self.assertEqual(state.state, "stale")
        self.assertIn("different", state.detail)

    def test_an_unreadable_environment_is_unknown(self):
        runtime = self.load()
        proc = build_proc(self.base, mapped=self.libs, write_environ=False)
        shell = mc.detect_shell(
            runner=self.systemd(), which=which_all("gdbus", "systemctl"),
            proc_root=proc, uid=UID, environ=self.environ())
        state = mc.read_live_state(shell, runtime, proc)
        self.assertEqual(state.state, "unknown")
        self.assertIsNone(state.opt_in)

    def test_unrelated_environment_entries_are_not_returned(self):
        proc = self.proc(environ={
            "XDG_SESSION_ID": SESSION_ID,
            "SOCKET_STREAM_CAPTURE_EXCLUDE": "1",
            "SECRET_TOKEN": "do-not-surface",
        })
        found = mc._read_environ(proc, PID)
        self.assertEqual(found.get(mc.OPT_IN_ENVIRON), "1")
        self.assertNotIn("SECRET_TOKEN", found)


# --------------------------------------------------------------------------
# status through the shared facade
# --------------------------------------------------------------------------
class Status(Base):
    def stock_proc(self):
        """A shell that still runs the system Mutter libraries."""
        stock = self.base / "usr/lib/libmutter-18.so.0"
        stock.parent.mkdir(parents=True, exist_ok=True)
        stock.write_bytes(b"stock library")
        return self.proc(mapped=[stock], environ={"XDG_SESSION_ID": SESSION_ID})

    def test_a_deployed_runtime_that_is_not_activated_is_offered(self):
        status = self.exclusion(proc_root=self.stock_proc()).status()
        self.assertTrue(status.supported)
        self.assertTrue(status.deployed)
        self.assertFalse(status.configured)
        self.assertFalse(status.runtime_active)
        self.assertFalse(status.restart_required)
        self.assertFalse(status.verified)
        self.assertTrue(status.can_install)
        self.assertFalse(status.can_remove)
        self.assertIn("activation is not set up", status.detail)

    def test_a_partial_load_without_activation_still_offers_setup(self):
        # No activation configured and the shell only mapped one library: the
        # runtime is still deployable and setup is still offered, but nothing
        # is claimed to be active.
        status = self.exclusion(proc_root=self.proc(
            mapped=self.libs[:1], environ={"XDG_SESSION_ID": SESSION_ID})).status()
        self.assertTrue(status.supported)
        self.assertFalse(status.configured)
        self.assertFalse(status.runtime_active)
        self.assertFalse(status.restart_required)
        self.assertTrue(status.can_install)

    def test_the_running_shell_reports_active_without_claiming_pixels(self):
        exclusion = self.exclusion()
        self.assertTrue(exclusion.install().configured)
        status = exclusion.status()
        self.assertTrue(status.configured)
        self.assertTrue(status.runtime_active)
        self.assertFalse(status.restart_required)
        self.assertFalse(status.verified)
        self.assertNotIn("panel is filtered", status.guidance)
        self.assertIn("real whole-screen share", status.guidance)

    def test_a_live_runtime_without_a_configured_activation_asks_for_a_login(self):
        # Nothing on disk names this runtime, so the running shell is already
        # out of step with the next login -- and setting it up is still allowed.
        status = self.exclusion().status()
        self.assertFalse(status.configured)
        self.assertTrue(status.runtime_active)
        self.assertTrue(status.restart_required)
        self.assertTrue(status.can_install)
        self.assertIn("activation changed", status.detail)

    def test_the_client_environment_never_counts_as_activation(self):
        proc = self.proc(environ={"XDG_SESSION_ID": SESSION_ID})
        exclusion = self.exclusion(
            proc_root=proc, environ=self.environ(SOCKET_STREAM_CAPTURE_EXCLUDE="1"))
        status = exclusion.status()
        self.assertFalse(status.runtime_active)
        self.assertIn(mc.OPT_IN_ENVIRON, exclusion.status().detail)

    def test_a_package_mismatch_disables_activation(self):
        status = self.exclusion(runner=self.systemd(
            packages=dict(PACKAGES, **{"gnome-shell": "50.1-0ubuntu2.6"}))).status()
        self.assertFalse(status.supported)
        self.assertFalse(status.can_install)
        self.assertIn("does not match the installed packages", status.detail)

    def test_configured_and_loaded_without_the_opt_in_asks_for_a_fresh_login(self):
        # The launcher exports the opt-in, so the correct fix is a new login --
        # not a reinstall.  The running shell may predate the activation.
        proc = self.proc(environ={"XDG_SESSION_ID": SESSION_ID})
        exclusion = self.exclusion(proc_root=proc)
        self.assertTrue(exclusion.install().configured)
        status = exclusion.status()
        self.assertFalse(status.runtime_active)
        self.assertTrue(status.restart_required)
        self.assertIn("log out and back in", status.guidance.lower())
        self.assertNotIn("reinstall", status.guidance.lower())

    def test_a_masked_unit_cannot_be_activated(self):
        status = self.exclusion(runner=self.systemd(
            load_state="masked")).status()
        self.assertFalse(status.supported)
        self.assertFalse(status.can_install)

    def test_a_foreign_file_at_the_managed_name_blocks_setup(self):
        # Not ours, so it must never be overwritten, deleted, or claimed.
        path = mc.dropin_path(self.config_home, UNIT)
        path.parent.mkdir(parents=True)
        path.write_text("[Service]\nExecStart=/bin/false\n")
        status = self.exclusion(proc_root=self.stock_proc()).status()
        self.assertFalse(status.configured)
        self.assertFalse(status.can_install)
        self.assertFalse(status.can_remove)
        self.assertIn("was not written by SocketStream", status.detail)
        self.assertEqual(path.read_text(), "[Service]\nExecStart=/bin/false\n")

    def test_a_symlink_at_the_managed_name_is_refused_and_preserved(self):
        target = self.base / "elsewhere.conf"
        target.write_text(mc.render_dropin(self.root, MODE, self.guard_state()))
        path = mc.dropin_path(self.config_home, UNIT)
        path.parent.mkdir(parents=True)
        path.symlink_to(target)
        status = self.exclusion(proc_root=self.stock_proc()).status()
        self.assertFalse(status.configured)
        self.assertFalse(status.can_install)
        self.assertFalse(status.can_remove)
        self.assertIn("not a regular file", status.detail)
        self.assertTrue(path.is_symlink())
        with self.assertRaises(rules.CaptureRuleError) as caught:
            self.exclusion(proc_root=self.stock_proc()).install()
        self.assertIn("left untouched", str(caught.exception))
        self.assertTrue(path.is_symlink())

    def test_a_broken_symlink_at_the_managed_name_is_refused(self):
        path = mc.dropin_path(self.config_home, UNIT)
        path.parent.mkdir(parents=True)
        path.symlink_to(self.base / "does-not-exist")
        status = self.exclusion(proc_root=self.stock_proc()).status()
        self.assertFalse(status.can_install)
        self.assertFalse(status.can_remove)
        self.assertTrue(path.is_symlink())

    def test_an_edited_override_is_never_adopted(self):
        # Regular and readable, but not byte-identical to what the renderer
        # produces for its own root, so it stays the user's file: a hand-added
        # header directive must never be treated as ours and deleted.
        path = mc.dropin_path(self.config_home, UNIT)
        path.parent.mkdir(parents=True)
        path.write_text(mc.render_dropin(self.root, MODE, self.guard_state()).replace(
            "[Service]\n", "# added by hand\n[Service]\n"))
        self.assertFalse(mc.scan_activation(self.config_home)[0].managed)
        status = self.exclusion(proc_root=self.stock_proc()).status()
        self.assertFalse(status.configured)
        self.assertFalse(status.can_install)
        self.assertFalse(status.can_remove)
        self.assertIn("was not written by SocketStream", status.detail)
        self.assertTrue(path.is_file())

    def test_an_oversized_file_of_that_name_is_not_read_as_ours(self):
        path = mc.dropin_path(self.config_home, UNIT)
        path.parent.mkdir(parents=True)
        path.write_text("x" * (mc.MAX_DROPIN_BYTES + 1))
        self.assertFalse(mc.activation_configured(
            self.config_home, UNIT, self.root, MODE, self.guard_state()))
        self.assertFalse(mc.scan_activation(self.config_home)[0].managed)
        status = self.exclusion(proc_root=self.stock_proc()).status()
        self.assertFalse(status.can_install)
        self.assertFalse(status.can_remove)
        self.assertIn("not a regular file", status.detail)
        self.assertTrue(path.is_file())

    def test_missing_proc_and_bus_degrade_to_unsupported(self):
        exclusion = self.exclusion(
            runner=FakeSystemd(dbus_pid=None), proc_root=self.base / "no-proc")
        status = exclusion.status()
        self.assertFalse(status.supported)
        self.assertIn("cannot be activated", status.detail)

    def test_status_never_raises_on_a_broken_manifest(self):
        self.manifest.write_text("{not json")
        status = self.exclusion().status()
        self.assertFalse(status.supported)
        self.assertFalse(status.deployed)
        self.assertIn("not valid JSON", status.detail)

    def test_a_failing_probe_still_offers_removal(self):
        # An unexpected failure must not strand a managed override the user
        # wants gone: the removal half is answered from the filesystem alone.
        path = mc.dropin_path(self.config_home, UNIT)
        path.parent.mkdir(parents=True)
        path.write_text(mc.render_dropin(self.root, MODE, self.guard_state()))
        original = mc.read_live_state

        def broken(*_args, **_kwargs):
            raise RuntimeError("probe broke")

        mc.read_live_state = broken
        self.addCleanup(setattr, mc, "read_live_state", original)
        exclusion = self.exclusion()
        status = exclusion.status()
        self.assertFalse(status.supported)
        self.assertFalse(status.configured)
        self.assertFalse(status.can_install)
        self.assertTrue(status.can_remove)
        self.assertEqual(status.path, str(path))
        exclusion.remove()
        self.assertFalse(path.exists())

    def test_status_json_carries_every_honest_field(self):
        payload = self.exclusion().status().as_dict()
        for key in ("deployed", "runtime_active", "restart_required", "verified",
                    "configured", "supported"):
            self.assertIn(key, payload)
        self.assertFalse(payload["verified"])

    def test_a_restart_is_not_claimed_while_the_live_state_is_unknown(self):
        # A configured activation plus an unreadable process table: the UI must
        # not assert that the running shell "started before" it.
        exclusion = self.exclusion(
            proc_root=build_proc(self.base, mapped=self.libs, write_environ=False))
        self.assertTrue(exclusion.install().configured)
        status = exclusion.status()
        self.assertFalse(status.restart_required)
        self.assertNotIn("started before", status.detail)


# --------------------------------------------------------------------------
# activation files
# --------------------------------------------------------------------------
class Activation(Base):
    def test_install_writes_the_managed_override_for_the_exact_unit(self):
        runner = self.systemd()
        # The shell that runs now still uses the system libraries, so the new
        # activation can only take effect after a fresh login.
        stock = self.base / "usr/lib/libmutter-18.so.0"
        stock.parent.mkdir(parents=True, exist_ok=True)
        stock.write_bytes(b"stock library")
        exclusion = self.exclusion(
            runner=runner,
            proc_root=self.proc(mapped=[stock],
                                environ={"XDG_SESSION_ID": SESSION_ID}),
        )
        status = exclusion.install()
        path = mc.dropin_path(self.config_home, UNIT)
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text(), mc.render_dropin(self.root, MODE, self.guard_state()))
        self.assertIn("/bin/sh -c", path.read_text())
        self.assertIn(f"--mode={MODE}", path.read_text())
        self.assertNotIn("wayland", str(path))
        self.assertTrue(runner.called("daemon-reload"))
        self.assertTrue(status.configured)
        self.assertTrue(status.restart_required)
        self.assertFalse(status.runtime_active)
        self.assertIn("log out and back in", exclusion.note())

    def test_the_override_pins_the_resolved_version_directory(self):
        # Never the mutable ``current`` pointer: an installed activation must
        # keep its meaning when the runtime is republished.
        published = self.base / "opt" / "socket_stream-mutter"
        published.mkdir(parents=True)
        current = published / "current"
        current.symlink_to(self.runtime)
        environ = self.environ(**{
            "SOCKET_STREAM_MUTTER_RUNTIME": str(current / "runtime.json")})
        exclusion = self.exclusion(environ=environ)
        exclusion.install()
        content = mc.dropin_path(self.config_home, UNIT).read_text()
        self.assertIn(f"test -x {self.root}/launch-gnome-shell", content)
        self.assertNotIn(str(current), content)
        self.assertTrue(exclusion.status().configured)

    def test_a_republished_current_needs_a_new_setup(self):
        published = self.base / "opt" / "socket_stream-mutter"
        published.mkdir(parents=True)
        current = published / "current"
        current.symlink_to(self.runtime)
        environ = self.environ(**{
            "SOCKET_STREAM_MUTTER_RUNTIME": str(current / "runtime.json")})
        exclusion = self.exclusion(environ=environ)
        self.assertTrue(exclusion.install().configured)

        newer = build_runtime(self.base / "v2", name="runtime-v2",
                              provenance=("source.dsc",))
        current.unlink()
        current.symlink_to(newer)

        republished = self.exclusion(environ=environ)
        status = republished.status()
        self.assertEqual(status.deployed, True)
        self.assertFalse(status.configured)
        self.assertTrue(status.can_install)
        self.assertTrue(status.can_remove)
        # Installing again moves the override to the new version directory.
        self.assertTrue(republished.install().configured)
        content = mc.dropin_path(self.config_home, UNIT).read_text()
        self.assertIn(f"test -x {os.path.realpath(newer)}/launch-gnome-shell", content)
        self.assertNotIn(f"test -x {self.root}/launch-gnome-shell", content)

    def test_install_is_idempotent(self):
        runner = self.systemd()
        exclusion = self.exclusion(runner=runner)
        exclusion.install()
        before = runner.calls.count(["/usr/bin/systemctl", "--user", "daemon-reload"])
        exclusion.install()
        after = runner.calls.count(["/usr/bin/systemctl", "--user", "daemon-reload"])
        self.assertEqual(before, after)
        self.assertEqual(
            mc.dropin_path(self.config_home, UNIT).read_text().count("ExecStart="), 2)

    def test_install_preserves_user_dropins(self):
        user = mc.dropin_dir(self.config_home, UNIT) / "50-user.conf"
        user.parent.mkdir(parents=True)
        user.write_text("[Service]\nEnvironment=MY_FLAG=1\n")
        other = mc.dropin_dir(self.config_home, "org.gnome.Shell@wayland.service")
        other.mkdir(parents=True)
        (other / "90-socket_stream-capture.conf").write_text(
            mc.render_dropin("/opt/other", "wayland", str(mc.guard_state_path(self.config_home, "/opt/other", "wayland"))))
        self.exclusion().install()
        self.assertEqual(user.read_text(), "[Service]\nEnvironment=MY_FLAG=1\n")
        self.assertTrue((other / "90-socket_stream-capture.conf").is_file())

    def test_install_refuses_a_foreign_file_at_the_managed_name(self):
        path = mc.dropin_path(self.config_home, UNIT)
        path.parent.mkdir(parents=True)
        path.write_text("[Service]\nExecStart=/bin/false\n")
        with self.assertRaises(rules.CaptureRuleError) as caught:
            self.exclusion().install()
        self.assertIn("left untouched", str(caught.exception))
        self.assertEqual(path.read_text(), "[Service]\nExecStart=/bin/false\n")

    def test_install_can_replace_an_activation_for_another_root(self):
        path = mc.dropin_path(self.config_home, UNIT)
        path.parent.mkdir(parents=True)
        old_root = "/opt/socket_stream-mutter/50.0-0ubuntu1.socket-stream.deadbeef0000"
        path.write_text(mc.render_dropin(old_root, MODE, str(mc.guard_state_path(self.config_home, old_root, MODE))))
        # The fake systemd reports whatever override is on disk, so it starts
        # out naming the old runtime directory and follows the replacement.
        status = self.exclusion().status()
        self.assertFalse(status.configured)
        self.assertTrue(status.can_install)
        self.assertTrue(status.can_remove)
        status = self.exclusion().install()
        self.assertTrue(status.configured)
        self.assertIn(f"test -x {self.root}/launch-gnome-shell", path.read_text())

    def test_a_failed_apply_check_rolls_back_and_reloads_again(self):
        # systemd accepts daemon-reload but reports the stock ExecStart: the
        # file must be restored *and* the cache reloaded again.
        runner = self.systemd(apply_dropin=False)
        with self.assertRaises(rules.CaptureRuleError) as caught:
            self.exclusion(runner=runner).install()
        self.assertIn("rolled back", str(caught.exception))
        self.assertEqual(runner.reloads, 2)
        self.assertFalse(mc.dropin_path(self.config_home, UNIT).exists())
        self.assertFalse(mc.dropin_dir(self.config_home, UNIT).exists())

    def test_a_rollback_restores_the_previous_override_and_reloads(self):
        path = mc.dropin_path(self.config_home, UNIT)
        path.parent.mkdir(parents=True)
        old_root = "/opt/socket_stream-mutter/50.0-0ubuntu1.socket-stream.deadbeef0000"
        previous = mc.render_dropin(old_root, MODE, str(mc.guard_state_path(self.config_home, old_root, MODE)))
        path.write_text(previous)
        runner = self.systemd(apply_dropin=False)
        with self.assertRaises(rules.CaptureRuleError):
            self.exclusion(runner=runner).install()
        self.assertEqual(path.read_text(), previous)
        self.assertEqual(runner.reloads, 2)

    def test_a_failed_rollback_reload_is_reported_as_incomplete(self):
        runner = self.systemd(reload_ok=False)
        with self.assertRaises(rules.CaptureRuleError) as caught:
            self.exclusion(runner=runner).install()
        message = str(caught.exception)
        self.assertIn("rollback incomplete", message)
        self.assertNotIn("; the change was rolled back", message)
        self.assertEqual(runner.reloads, 2)
        self.assertFalse(mc.dropin_path(self.config_home, UNIT).exists())

    def test_install_requires_a_deployed_runtime(self):
        self.manifest.unlink()
        with self.assertRaises(rules.CaptureRuleError) as caught:
            self.exclusion().install()
        self.assertIn("not installed", str(caught.exception))
        self.assertFalse(mc.scan_activation(self.config_home))

    def test_remove_deletes_only_managed_files(self):
        exclusion = self.exclusion()
        exclusion.install()
        user = mc.dropin_dir(self.config_home, UNIT) / "50-user.conf"
        user.write_text("[Service]\nEnvironment=MY_FLAG=1\n")
        runner = self.systemd()
        status = self.exclusion(runner=runner).remove()
        self.assertFalse(mc.dropin_path(self.config_home, UNIT).exists())
        self.assertEqual(user.read_text(), "[Service]\nEnvironment=MY_FLAG=1\n")
        self.assertFalse(status.configured)
        self.assertTrue(runner.called("daemon-reload"))

    def test_remove_works_after_the_session_unit_changed(self):
        stale = mc.dropin_dir(self.config_home, "org.gnome.Shell@wayland.service")
        stale.mkdir(parents=True)
        (stale / mc.MANAGED_FILENAME).write_text(
            mc.render_dropin(self.root, "wayland", str(mc.guard_state_path(self.config_home, self.root, "wayland"))))
        status = self.exclusion().status()
        self.assertTrue(status.can_remove)
        self.assertFalse(status.configured)
        self.exclusion().remove()
        self.assertFalse((stale / mc.MANAGED_FILENAME).exists())

    def test_remove_without_managed_files_does_not_reload(self):
        runner = self.systemd()
        exclusion = self.exclusion(runner=runner)
        status = exclusion.remove()
        self.assertIn("no managed activation", exclusion.note())
        self.assertFalse(status.configured)
        self.assertFalse(runner.called("daemon-reload"))

    def test_remove_rolls_back_and_reloads_when_the_first_reload_fails(self):
        self.exclusion().install()
        path = mc.dropin_path(self.config_home, UNIT)
        content = path.read_text()
        runner = self.systemd(reload_fail_at=(1,))
        with self.assertRaises(rules.CaptureRuleError) as caught:
            self.exclusion(runner=runner).remove()
        message = str(caught.exception)
        self.assertIn("rolled back", message)
        self.assertNotIn("rollback incomplete", message)
        # The file is back *and* systemd was told about it again.
        self.assertEqual(path.read_text(), content)
        self.assertEqual(runner.reloads, 2)
        self.assertTrue(self.exclusion().status().configured)

    def test_a_partial_removal_reports_an_incomplete_rollback(self):
        self.exclusion().install()
        runner = self.systemd(reload_ok=False)
        with self.assertRaises(rules.CaptureRuleError) as caught:
            self.exclusion(runner=runner).remove()
        self.assertIn("rollback incomplete", str(caught.exception))
        self.assertEqual(runner.reloads, 2)
        self.assertTrue(mc.dropin_path(self.config_home, UNIT).is_file())

    def test_remove_refuses_a_modified_file(self):
        path = mc.dropin_path(self.config_home, UNIT)
        path.parent.mkdir(parents=True)
        path.write_text(mc.render_dropin(self.root, MODE, self.guard_state()) + "# edited\n")
        with self.assertRaises(rules.CaptureRuleError) as caught:
            self.exclusion().remove()
        self.assertIn("left untouched", str(caught.exception))
        self.assertTrue(path.is_file())

    def test_removal_keeps_working_when_the_runtime_is_incompatible(self):
        self.exclusion().install()
        runner = self.systemd(packages=dict(PACKAGES, **{
            "libmutter-18-0": "50.1-0ubuntu2.6"}))
        exclusion = self.exclusion(runner=runner)
        self.assertFalse(exclusion.status().supported)
        self.assertTrue(exclusion.status().can_remove)
        exclusion.remove()
        self.assertFalse(mc.dropin_path(self.config_home, UNIT).exists())

    def test_removal_keeps_working_when_the_runtime_disappeared(self):
        self.exclusion().install()
        self.manifest.unlink()
        exclusion = self.exclusion()
        self.assertFalse(exclusion.status().supported)
        self.assertTrue(exclusion.status().can_remove)
        exclusion.remove()
        self.assertFalse(mc.scan_activation(self.config_home))

    def test_a_configured_activation_survives_a_package_mismatch(self):
        exclusion = self.exclusion()
        self.assertTrue(exclusion.install().configured)
        status = self.exclusion(runner=self.systemd(
            packages=dict(PACKAGES, **{"libmutter-18-0": "50.1-0ubuntu2.6"}))).status()
        self.assertTrue(status.configured)
        self.assertFalse(status.supported)
        self.assertFalse(status.runtime_active)
        self.assertTrue(status.can_remove)

    def test_removing_the_activation_while_the_runtime_is_live_needs_a_restart(self):
        exclusion = self.exclusion()
        self.assertTrue(exclusion.install().configured)
        before = exclusion.status()
        self.assertTrue(before.runtime_active)
        self.assertTrue(before.configured)
        self.assertFalse(before.restart_required)

        status = exclusion.remove()
        self.assertFalse(status.configured)
        self.assertTrue(status.restart_required)
        self.assertIn("activation changed", status.detail)

    def test_gnome_on_x11_can_still_remove_a_leftover_activation(self):
        path = mc.dropin_dir(self.config_home, UNIT) / mc.MANAGED_FILENAME
        path.parent.mkdir(parents=True)
        path.write_text(mc.render_dropin(self.root, MODE, self.guard_state()))
        exclusion = rules.CaptureExclusion(
            guard.SessionInfo(guard.SESSION_X11, "gnome", "gnome", True, False),
            environ=self.environ(), home=self.config_home,
            runner=self.systemd(), which=which_all("systemctl"),
            proc_root=self.proc(), uid=UID, runtime_owner_uid=OWNER,
        )
        status = exclusion.status()
        self.assertFalse(status.supported)
        self.assertFalse(status.can_install)
        self.assertTrue(status.can_remove)
        self.assertEqual(status.path, str(path))
        exclusion.remove()
        self.assertFalse(path.exists())

    def test_render_rejects_paths_a_unit_file_cannot_hold(self):
        for root in ("/opt/it's", "/opt/100%", "/opt/$HOME", "/opt/a b"):
            with self.subTest(root=root):
                with self.assertRaises(mc.MutterCaptureError):
                    mc.render_dropin(root, MODE, "/some/state.json")
        with self.assertRaises(mc.MutterCaptureError):
            mc.render_dropin("/opt/good", "bad mode", "/some/state.json")

    def test_managed_content_recognition_is_exact(self):
        good = mc.render_dropin("/opt/socket_stream-mutter/current", MODE, "/some/state.json")
        self.assertTrue(mc.is_managed_content(good))
        self.assertFalse(mc.is_managed_content(good + "\n"))
        self.assertFalse(mc.is_managed_content(
            good.replace("Managed by SocketStream", "Edited by hand")))
        self.assertFalse(mc.is_managed_content(
            good.replace("# Recovery from a TTY", "# Added by hand\n# Recovery from a TTY")))
        self.assertFalse(mc.is_managed_content(
            good.replace("[Service]\n", "[Service]\nEnvironment=EVIL=1\n")))
        self.assertFalse(mc.is_managed_content(good.replace("/bin/sh -c", "/bin/bash -c")))
        self.assertFalse(mc.is_managed_content(
            good.replace("; fi; exec /usr/bin/gnome-shell", "; fi; /bin/false;")))
        self.assertFalse(mc.is_managed_content(""))

    def test_the_scan_ignores_unrelated_files(self):
        directory = mc.dropin_dir(self.config_home, UNIT)
        directory.mkdir(parents=True)
        (directory / "10-unrelated.conf").write_text("[Service]\n")
        (directory / "org.gnome.Shell@wayland.service.d").mkdir()
        self.assertFalse(mc.scan_activation(self.config_home))


class GuardedStartupTransactions(Base):
    def configured_file(self):
        path = mc.dropin_path(self.config_home, UNIT)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(mc.render_dropin(self.root, MODE, self.guard_state()))
        return path

    def write_guard(self, phase, **changes):
        record = {"schema": 1, "runtime": self.root, "mode": MODE, "phase": phase}
        if phase in ("pending", "ready"):
            record.update(invocation_id="a" * 32, boot_id="b" * 32, pid=PID, starttime=100)
        record.update(changes)
        path = Path(self.guard_state())
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
        path.chmod(0o600)
        return path

    def stock_proc(self):
        stock = self.base / "stock/libmutter-18.so.0"
        stock.parent.mkdir(exist_ok=True)
        stock.write_bytes(b"stock fixture")
        return self.proc(mapped=[stock], environ={"XDG_SESSION_ID": SESSION_ID})

    def current_attempt_proc(self):
        proc = self.proc(environ={"XDG_SESSION_ID": SESSION_ID, mc.OPT_IN_ENVIRON: "1",
                                 "INVOCATION_ID": "a" * 32})
        boot = proc / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True, exist_ok=True)
        boot.write_text("b" * 32 + "\n")
        (proc / str(PID) / "stat").write_text(f"{PID} (gnome-shell) S " + "0 " * 18 + "100 0\n")
        (proc / str(PID) / "exe").symlink_to(self.runtime / mc.SHELL)
        return proc

    def test_probe_failure_never_installs_or_arms(self):
        for setup in ({"probe_ok": False}, {"probe_exit": 1}, {"probe_result": "not JSON"}):
            runner = self.systemd(**setup)
            with self.subTest(setup=setup), self.assertRaisesRegex(rules.CaptureRuleError, "startup check"):
                self.exclusion(runner=runner).install()
            self.assertFalse(mc.dropin_path(self.config_home, UNIT).exists())
            self.assertFalse(Path(self.guard_state()).exists())
            self.assertEqual((runner.probes, runner.arms, runner.reloads), (1, 0, 0))

    def test_probe_precedes_configuration_and_arming(self):
        scripted = self.systemd()
        observed = []
        def runner(argv, *args):
            if "startup_probe.py" in " ".join(argv):
                observed.append("probe")
                self.assertFalse(mc.dropin_path(self.config_home, UNIT).exists())
                self.assertFalse(Path(self.guard_state()).exists())
            elif "startup_guard.py" in " ".join(argv):
                observed.append("arm")
                self.assertEqual(scripted.reloads, 1)
                self.assertTrue(mc.activation_configured(self.config_home, UNIT, self.root, MODE, self.guard_state()))
                self.assertTrue(mc.read_guard_state(Path(self.guard_state())).blocked)
            return scripted(argv, *args)
        self.exclusion(runner=runner).install()
        self.assertEqual(observed, ["probe", "arm"])

    def test_stale_consumed_states_are_not_mistaken_for_armed(self):
        self.configured_file()
        for phase in ("blocked", "pending", "ready"):
            self.write_guard(phase)
            runner = self.systemd()
            exclusion = self.exclusion(runner=runner, proc_root=self.stock_proc())
            status = exclusion.status()
            self.assertTrue(status.fallback_latched)
            self.assertTrue(status.can_install)
            self.assertFalse(status.restart_required)
            self.assertNotIn("log out", status.guidance.lower())
            self.assertIn("Set up exclusion", status.guidance)
            exclusion.install()
            self.assertEqual((runner.probes, runner.arms), (1, 1))
            self.assertTrue(mc.read_guard_state(Path(self.guard_state())).armed)

    def test_missing_or_foreign_guard_never_promises_a_useful_restart(self):
        self.configured_file()
        for changes in (None, {"runtime": "/opt/other-runtime"}, {"mode": "wayland"}):
            if changes:
                self.write_guard("armed", **changes)
            status = self.exclusion(proc_root=self.stock_proc()).status()
            self.assertTrue(status.configured)
            self.assertTrue(status.fallback_latched)
            self.assertTrue(status.can_install)
            self.assertFalse(status.restart_required)

    def test_a_current_ready_invocation_needs_no_rearm_or_probe(self):
        self.configured_file()
        state = self.write_guard("ready")
        before = state.read_bytes()
        runner = self.systemd()
        exclusion = self.exclusion(runner=runner, proc_root=self.current_attempt_proc())
        self.assertFalse(exclusion.status().fallback_latched)
        exclusion.install()
        self.assertIn("already active", exclusion.note())
        self.assertEqual((runner.probes, runner.arms), (0, 0))
        self.assertEqual(state.read_bytes(), before)

    def test_pending_current_attempt_cannot_be_claimed_as_ready_or_rearmed(self):
        self.configured_file()
        state = self.write_guard("pending")
        runner = self.systemd()
        exclusion = self.exclusion(runner=runner, proc_root=self.current_attempt_proc())
        status = exclusion.status()
        self.assertTrue(status.fallback_latched)
        self.assertFalse(status.can_install)
        with self.assertRaisesRegex(rules.CaptureRuleError, "still running"):
            exclusion.install()
        self.assertEqual((runner.probes, runner.arms), (0, 0))
        self.assertEqual(mc.read_guard_state(state).phase, "pending")

    def test_boot_or_pid_reuse_does_not_bypass_the_probe(self):
        self.configured_file()
        proc = self.current_attempt_proc()
        for changes in ({"boot_id": "c" * 32}, {"starttime": 101}, {"pid": PID + 1}):
            self.write_guard("ready", **changes)
            runner = self.systemd()
            self.exclusion(runner=runner, proc_root=proc).install()
            self.assertEqual((runner.probes, runner.arms), (1, 1))

    def test_an_effective_command_without_the_exact_file_is_reinstalled(self):
        path = self.configured_file()
        self.write_guard("armed")
        scripted = self.systemd()
        effective = mc._systemctl_show(scripted, which_all("systemctl"), UNIT)
        path.unlink()
        def runner(argv, *args):
            if "show" in argv:
                return result(argv, out="\n".join(f"{key}={value}" for key, value in effective.items()))
            return scripted(argv, *args)
        self.exclusion(runner=runner).install()
        self.assertTrue(path.is_file())
        self.assertEqual((scripted.probes, scripted.arms, scripted.reloads), (1, 1, 1))

    def test_a_user_edit_during_the_probe_is_preserved(self):
        path = mc.dropin_path(self.config_home, UNIT)
        scripted = self.systemd()
        def runner(argv, *args):
            if "startup_probe.py" in " ".join(argv):
                path.parent.mkdir(parents=True)
                path.write_text("# user edit during startup probe\n")
            return scripted(argv, *args)
        with self.assertRaisesRegex(rules.CaptureRuleError, "changed during"):
            self.exclusion(runner=runner).install()
        self.assertEqual(path.read_text(), "# user edit during startup probe\n")
        self.assertEqual(scripted.arms, 0)

    def test_ready_hooks_require_main_process_notify_semantics(self):
        for kind, access in (("simple", "main"), ("exec", "main"), ("notify", "all"), ("notify", "exec")):
            runner = self.systemd(service_type=kind, notify_access=access)
            exclusion = self.exclusion(runner=runner)
            self.assertFalse(exclusion.status().supported)
            with self.assertRaisesRegex(rules.CaptureRuleError, "Type=notify"):
                exclusion.install()
            self.assertEqual((runner.probes, runner.arms), (0, 0))

    def test_effective_hooks_must_be_present_and_unambiguous(self):
        extra = " ".join(mc.guard_command(self.root, MODE, "/wrong/state.json", "finish"))
        hook = f"{{ path=/usr/bin/python3 ; argv[]={extra} ; ignore_errors=yes ; pid=0 }}"
        for options in ({"post_hooks": False}, {"extra_exec_stop_post": hook}):
            runner = self.systemd(**options)
            with self.assertRaisesRegex(rules.CaptureRuleError, "did not apply"):
                self.exclusion(runner=runner).install()
            self.assertEqual(runner.arms, 0)
            self.assertFalse(mc.dropin_path(self.config_home, UNIT).exists())

    def test_post_hooks_append_without_changing_the_base_service(self):
        content = mc.render_dropin(self.root, MODE, self.guard_state())
        for key in ("Type=", "NotifyAccess=", "Restart=", "OnFailure=", "SuccessExitStatus="):
            self.assertNotIn(key, content)
        self.assertNotIn("\nExecStopPost=\n", content)
        self.assertNotIn("\nExecStartPost=\n", content)
        upstream = "{ path=systemctl ; argv[]=systemctl --user unset-environment DISPLAY ; ignore_errors=yes ; pid=0 }"
        runner = self.systemd(extra_exec_stop_post=upstream)
        self.assertTrue(self.exclusion(runner=runner).install().configured)
        properties = mc._systemctl_show(runner, which_all("systemctl"), UNIT)
        self.assertIn(upstream, properties["ExecStopPost"])

    def test_a_failed_arm_rolls_back_and_leaves_a_blocked_guard(self):
        for options in ({"guard_arm_ok": False}, {"guard_arm_phase": "ready"}, {"write_guard": False}):
            runner = self.systemd(**options)
            with self.assertRaisesRegex(rules.CaptureRuleError, "rolled back"):
                self.exclusion(runner=runner).install()
            self.assertFalse(mc.dropin_path(self.config_home, UNIT).exists())
            self.assertTrue(mc.read_guard_state(Path(self.guard_state())).blocked)
            self.assertEqual(runner.reloads, 2)

    def test_failed_revocation_cannot_be_reported_as_a_complete_rollback(self):
        runner = self.systemd(guard_arm_ok=False)
        revoke = mc._revoke_guard
        calls = 0
        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise mc.MutterCaptureError("fixture disk failure")
            return revoke(*args)
        with patch.object(mc, "_revoke_guard", side_effect=fail_second):
            with self.assertRaisesRegex(rules.CaptureRuleError, "rollback incomplete.*revocation incomplete"):
                self.exclusion(runner=runner).install()
        self.assertFalse(mc.dropin_path(self.config_home, UNIT).exists())

    def test_removal_executes_no_runtime_helper_even_when_it_is_present_but_untrusted(self):
        self.exclusion().install()
        (self.runtime / "startup_guard.py").write_text("raise RuntimeError('must never run')")
        runner = self.systemd()
        self.exclusion(runner=runner).remove()
        self.assertFalse(runner.called("startup_guard.py"))
        self.assertTrue(mc.read_guard_state(Path(self.guard_state())).blocked)

    def test_altered_guard_path_is_never_adopted_or_mutated(self):
        path = self.configured_file()
        outside = self.base / "unrelated.json"
        outside.write_text("user data")
        path.write_text(mc.render_dropin(self.root, MODE, str(outside)))
        before = path.read_bytes()
        exclusion = self.exclusion(proc_root=self.stock_proc())
        self.assertFalse(exclusion.status().can_remove)
        with self.assertRaisesRegex(rules.CaptureRuleError, "left untouched"):
            exclusion.remove()
        with self.assertRaisesRegex(mc.MutterCaptureError, "unexpected startup guard path"):
            mc._revoke_guard(self.config_home, self.root, MODE, str(outside))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(outside.read_text(), "user data")

    def test_removal_reports_a_held_guard_lock_and_keeps_the_lock_inode(self):
        import fcntl
        path = self.configured_file()
        state = self.write_guard("armed")
        lock = state.with_name(state.name + ".lock")
        lock.touch(mode=0o600)
        inode = lock.stat().st_ino
        with lock.open("rb") as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(rules.CaptureRuleError, "removed 1.*revocation incomplete"):
                self.exclusion().remove()
        self.assertFalse(path.exists())
        self.assertTrue(mc.read_guard_state(state).armed)
        self.assertEqual(lock.stat().st_ino, inode)

    def test_removal_rollback_does_not_rearm_without_another_probe(self):
        self.configured_file()
        self.write_guard("ready")
        runner = self.systemd(reload_fail_at=(1,))
        with self.assertRaisesRegex(rules.CaptureRuleError, "guards remain blocked"):
            self.exclusion(runner=runner).remove()
        self.assertTrue(mc.dropin_path(self.config_home, UNIT).exists())
        self.assertTrue(mc.read_guard_state(Path(self.guard_state())).blocked)
        self.assertEqual(runner.probes, 0)

    def test_revocation_and_reload_failure_never_restore_a_potentially_armed_override(self):
        for failures in ((1,), (1, 2)):
            path = self.configured_file()
            state = self.write_guard("armed")
            runner = self.systemd(reload_fail_at=failures)
            with self.subTest(failures=failures), patch.object(
                    mc, "_revoke_guard", side_effect=mc.MutterCaptureError("fixture held lock")):
                with self.assertRaises(rules.CaptureRuleError) as caught:
                    self.exclusion(runner=runner).remove()
            message = str(caught.exception)
            self.assertIn("revocation incomplete", message)
            self.assertIn(f"activation file kept removed: {path}", message)
            self.assertNotIn("guards remain blocked", message)
            self.assertNotIn("were rolled back", message)
            self.assertFalse(path.exists())
            self.assertTrue(mc.read_guard_state(state).armed)
            self.assertEqual(runner.reloads, 2)
            if len(failures) == 2:
                self.assertIn("rollback incomplete", message)

    def test_failed_partial_removal_keeps_the_unrevoked_entry_removed(self):
        first = self.configured_file()
        state = self.write_guard("armed")
        second = mc.dropin_path(self.config_home, "org.gnome.Shell@wayland.service")
        second.parent.mkdir(parents=True)
        other_state = mc.guard_state_path(self.config_home, self.root, "wayland")
        second.write_text(mc.render_dropin(self.root, "wayland", str(other_state)))
        revoke, remove = mc._revoke_guard, mc._remove_tree
        def revocation(config, root, mode, state):
            if mode == MODE:
                raise mc.MutterCaptureError("fixture held lock")
            return revoke(config, root, mode, state)
        def removal(path, **kwargs):
            if path == second:
                raise mc.MutterCaptureError("fixture unlink failure")
            return remove(path, **kwargs)
        with patch.object(mc, "_revoke_guard", side_effect=revocation), patch.object(
                mc, "_remove_tree", side_effect=removal):
            with self.assertRaises(rules.CaptureRuleError) as caught:
                self.exclusion().remove()
        self.assertIn("fixture unlink failure", str(caught.exception))
        self.assertIn("revocation incomplete", str(caught.exception))
        self.assertNotIn("guards remain blocked", str(caught.exception))
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(mc.read_guard_state(state).armed)
        self.assertTrue(mc.read_guard_state(other_state).blocked)

    def test_write_failure_after_revocation_discloses_the_blocked_guard(self):
        runner = self.systemd()
        with patch.object(mc, "_write_atomic", side_effect=mc.MutterCaptureError("fixture disk full")):
            with self.assertRaisesRegex(rules.CaptureRuleError, "disk full.*guard is blocked.*did not complete"):
                self.exclusion(runner=runner).install()
        self.assertTrue(mc.read_guard_state(Path(self.guard_state())).blocked)
        self.assertFalse(mc.dropin_path(self.config_home, UNIT).exists())
        self.assertEqual((runner.probes, runner.arms, runner.reloads), (1, 0, 0))

    def test_write_failure_after_retiring_an_activation_discloses_both_blocked_guards(self):
        path = self.configured_file()
        old_root = "/opt/socket_stream-mutter/50.0.socket-stream.old"
        old_state = mc.guard_state_path(self.config_home, old_root, MODE)
        old_state.parent.mkdir(mode=0o700, parents=True)
        old_state.write_text(json.dumps({"schema": 1, "runtime": old_root, "mode": MODE, "phase": "armed"}))
        old_state.chmod(0o600)
        previous = mc.render_dropin(old_root, MODE, str(old_state))
        path.write_text(previous)
        with patch.object(mc, "_write_atomic", side_effect=mc.MutterCaptureError("fixture disk full")):
            with self.assertRaisesRegex(rules.CaptureRuleError, "previous activation remains installed.*guard is now revoked"):
                self.exclusion().install()
        self.assertEqual(path.read_text(), previous)
        self.assertTrue(mc.read_guard_state(old_state).blocked)
        self.assertTrue(mc.read_guard_state(Path(self.guard_state())).blocked)

    def test_multiple_failed_revocations_are_all_disclosed_after_removal_or_failed_reload(self):
        for reload_failures in ((), (1,)):
            first = self.configured_file()
            self.write_guard("armed")
            second = mc.dropin_path(self.config_home, "org.gnome.Shell@wayland.service")
            second.parent.mkdir(parents=True, exist_ok=True)
            state = mc.guard_state_path(self.config_home, self.root, "wayland")
            second.write_text(mc.render_dropin(self.root, "wayland", str(state)))
            runner = self.systemd(reload_fail_at=reload_failures)
            with self.subTest(reload_failures=reload_failures), patch.object(
                    mc, "_revoke_guard", side_effect=mc.MutterCaptureError("fixture busy guard")):
                with self.assertRaises(rules.CaptureRuleError) as caught:
                    self.exclusion(runner=runner).remove()
            message = str(caught.exception)
            for path in (first, second):
                self.assertIn(f"startup guard revocation incomplete for {path}", message)
                self.assertFalse(path.exists())
                if reload_failures:
                    self.assertIn(f"activation file kept removed: {path}", message)
            self.assertNotIn("guards remain blocked", message)

    def test_retired_guard_revocation_failure_does_not_claim_a_stock_next_login(self):
        path = self.configured_file()
        old_root = "/opt/socket_stream-mutter/50.0.socket-stream.old"
        old_state = mc.guard_state_path(self.config_home, old_root, MODE)
        old_state.parent.mkdir(mode=0o700, parents=True)
        old_state.write_text(json.dumps({"schema": 1, "runtime": old_root, "mode": MODE, "phase": "armed"}))
        old_state.chmod(0o600)
        previous = mc.render_dropin(old_root, MODE, str(old_state))
        path.write_text(previous)
        revoke = mc._revoke_guard
        def revocation(config, root, mode, state):
            if root == old_root:
                raise mc.MutterCaptureError("fixture old lock busy")
            return revoke(config, root, mode, state)
        with patch.object(mc, "_revoke_guard", side_effect=revocation):
            with self.assertRaisesRegex(rules.CaptureRuleError, "new runtime's startup guard is blocked.*previous activation"):
                self.exclusion().install()
        self.assertEqual(path.read_text(), previous)
        self.assertTrue(mc.read_guard_state(old_state).armed)
        self.assertTrue(mc.read_guard_state(Path(self.guard_state())).blocked)

    def test_failed_arm_revocation_and_failed_file_rollback_report_both(self):
        revoke = mc._revoke_guard
        calls = 0
        def revocation(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise mc.MutterCaptureError("fixture revoke failure")
            return revoke(*args)
        with patch.object(mc, "_revoke_guard", side_effect=revocation), patch.object(
                mc, "_remove_tree", side_effect=mc.MutterCaptureError("fixture rollback unlink failure")):
            with self.assertRaises(rules.CaptureRuleError) as caught:
                self.exclusion(runner=self.systemd(guard_arm_ok=False)).install()
        self.assertIn("rollback incomplete: fixture rollback unlink failure", str(caught.exception))
        self.assertIn("revocation incomplete: fixture revoke failure", str(caught.exception))

    def test_unsafe_existing_guard_and_lock_permissions_are_not_repaired_or_armed(self):
        self.configured_file()
        state = self.write_guard("blocked")
        lock = state.with_name(state.name + ".lock")
        lock.touch(mode=0o600)
        for unsafe in (state, lock):
            unsafe.chmod(0o644)
            runner = self.systemd()
            with self.subTest(unsafe=unsafe.name), self.assertRaisesRegex(rules.CaptureRuleError, "permissions"):
                self.exclusion(runner=runner).install()
            self.assertEqual(unsafe.stat().st_mode & 0o777, 0o644)
            self.assertEqual(runner.arms, 0)
            unsafe.chmod(0o600)

    def test_restrictive_umask_install_and_removal_use_native_guard_permissions(self):
        # Subprocess keeps the process-wide umask out of the rest of the suite.
        script = '''
import os
from pathlib import Path
from tests.test_mutter_capture import GuardedStartupTransactions, mc, UNIT
case = GuardedStartupTransactions()
case.setUp()
previous = None
try:
    exclusion = case.exclusion()
    previous = os.umask(0o277)
    assert exclusion.install().configured
    state = Path(case.guard_state())
    lock = state.with_name(state.name + ".lock")
    activation = mc.dropin_path(case.config_home, UNIT)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in (state, lock, activation))
    assert state.parent.stat().st_mode & 0o777 == 0o700
    assert mc.read_guard_state(state).armed
    exclusion.remove()
    assert not activation.exists()
    assert mc.read_guard_state(state).blocked
    assert state.stat().st_mode & 0o777 == 0o600
finally:
    if previous is not None:
        os.umask(previous)
    case.doCleanups()
'''
        completed = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                                   capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_deployment_alone_is_not_reported_as_validated_configuration(self):
        status = self.exclusion().status()
        self.assertTrue(status.deployed)
        self.assertFalse(status.config_validated)
        self.assertTrue(self.exclusion().install().config_validated)
        status = self.exclusion(runner=self.systemd(packages=dict(
            PACKAGES, **{"libmutter-18-0": "50.1-0ubuntu2.6"}))).status()
        self.assertTrue(status.configured)
        self.assertFalse(status.config_validated)

    def test_symlinked_configuration_ancestor_never_redirects_a_write(self):
        outside = self.base / "elsewhere"
        outside.mkdir(mode=0o755)
        (outside / "sentinel").write_text("keep")
        (self.config_home / "systemd").symlink_to(outside)
        before = outside.stat().st_mode
        runner = self.systemd()
        with self.assertRaisesRegex(rules.CaptureRuleError, "left untouched"):
            self.exclusion(runner=runner).install()
        self.assertEqual(list(outside.iterdir()), [outside / "sentinel"])
        self.assertEqual(outside.stat().st_mode, before)
        self.assertEqual((runner.probes, runner.arms), (0, 0))

    def test_guard_directory_collision_never_chmods_or_writes_outside(self):
        state = Path(self.guard_state())
        state.parent.parent.mkdir(parents=True)
        outside = self.base / "private-user-data"
        outside.mkdir(mode=0o755)
        state.parent.symlink_to(outside)
        with self.assertRaises(mc.MutterCaptureError):
            mc._revoke_guard(self.config_home, self.root, MODE, str(state))
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(outside.stat().st_mode & 0o777, 0o755)

    def test_guard_reader_rejects_wrong_permissions_duplicate_keys_and_bad_identity(self):
        path = self.write_guard("armed")
        path.chmod(0o644)
        self.assertTrue(mc.read_guard_state(path).missing)
        path.chmod(0o600)
        for document in ('{"schema":1,"schema":1}', '{"schema":true}',
                         json.dumps({"schema": 1, "runtime": self.root, "mode": MODE, "phase": "ready"})):
            path.write_text(document)
            self.assertTrue(mc.read_guard_state(path).missing)


if __name__ == "__main__":
    unittest.main()
