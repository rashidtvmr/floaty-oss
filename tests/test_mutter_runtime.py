"""Login fallback, private runtime integrity, and transactional publication.

No compositor/session is started. Package and loader observations are injected;
launcher tests exec a recorder in place of GNOME Shell.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

NATIVE = Path(__file__).resolve().parents[1] / "native/mutter-capture-filter"
sys.path.insert(0, str(NATIVE))
import validate_runtime as validator
import stage_runtime as stager
sys.path.pop(0)

VERSION = "50.1-0ubuntu2.5"
PACKAGES = {name: "50.1-0ubuntu1.5" if "gnome-shell" in name else VERSION for name in validator.PACKAGES}


class RuntimeFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.root = self.base / "50.1.socket-stream.test"
        self.root.mkdir()
        self.uid = os.getuid()
        self.observed_packages = dict(PACKAGES)
        self.mixed = False
        self.loader_missing = False
        self.transitive_mixed = False
        self.search_paths = {}
        self.calls = []
        files = {}
        for name in validator.LIBRARIES + validator.TYPELIBS + validator.SCRIPTS + (validator.SHELL,):
            target = self.root / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(("synthetic fixture " + name).encode())
            target.chmod(0o555 if name in validator.EXECUTABLES else 0o444)
            files[name] = self.metadata(target)
        self.manifest = {
            "schema": validator.SCHEMA, "shell": validator.SHELL,
            "runtime_id": self.root.name, "source_version": VERSION,
            "source_sha256": "a" * 64, "patch_sha256": "b" * 64,
            "packages": dict(PACKAGES), "files": files,
            "libraries": list(validator.LIBRARIES), "library_dir": "lib", "typelib_dir": "typelib",
        }
        self.save()

    @staticmethod
    def metadata(path):
        data = path.read_bytes()
        return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

    def save(self):
        self.manifest["runtime_id"] = validator.runtime_identifier(self.manifest)
        path = self.root / "runtime.json"
        if path.exists():
            path.chmod(0o644)
        path.write_text(json.dumps(self.manifest))
        path.chmod(0o444)

    def runner(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == "/usr/bin/dpkg-query":
            stdout = "".join(f"{k}\t{v}\tinstall ok installed\n" for k, v in self.observed_packages.items())
        elif argv[0] == "/usr/bin/readelf":
            stdout = ""
            for name in argv[3:]:
                tag, value = self.search_paths.get(Path(name).name, (
                    ("RUNPATH", validator.SHELL_RUNPATH) if Path(name).name == validator.SHELL
                    else ("RPATH", "$ORIGIN")))
                stdout += f"\nFile: {name}\n 0x0000001d ({tag}) Library search path: [{value}]\n"
        elif argv[0] == "/usr/bin/ldd":
            directory = Path(kwargs["env"]["LD_LIBRARY_PATH"].split(":")[0])
            stdout = ""
            for index, name in enumerate(validator.LIBRARIES):
                target = directory / Path(name).name
                if self.mixed and index == 2:
                    target = self.root / name
                if self.transitive_mixed and Path(argv[1]).name == "libmutter-clutter-18.so.0" and index == 2:
                    target = self.base / "stock.so"
                    target.write_bytes(b"stock")
                if not (self.loader_missing and index == 3):
                    stdout += f" {Path(name).name} => {target} (0x1234)\n"
        else:
            self.fail(f"unexpected command: {argv}")
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def validate(self, path=None):
        return validator.validate_runtime(path or self.root / "runtime.json", run=self.runner, owner_uid=self.uid)


class RuntimeTests(RuntimeFixture):
    def test_runtime_label_cannot_claim_another_manifest(self):
        self.manifest["runtime_id"] = "50.1.socket-stream.misleading"
        path = self.root / "runtime.json"
        path.chmod(0o644)
        path.write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(validator.InvalidRuntime, "identifier does not match"):
            self.validate()

    def test_missing_validation_tool_has_an_actionable_failure(self):
        def missing(*_args, **_kwargs):
            raise FileNotFoundError()

        with self.assertRaisesRegex(validator.InvalidRuntime, "dpkg-query is unavailable"):
            validator.installed_packages(run=missing)
        with self.assertRaisesRegex(validator.InvalidRuntime, "readelf is unavailable"):
            validator.check_loader(self.root, run=missing)
        def missing_ldd(argv, **kwargs):
            if argv[0] == "/usr/bin/ldd":
                raise FileNotFoundError()
            return self.runner(argv, **kwargs)
        with self.assertRaisesRegex(validator.InvalidRuntime, "ldd is unavailable"):
            validator.check_loader(self.root, run=missing_ldd)

    def test_complete_runtime_resolves_current_to_immutable_root(self):
        current = self.base / "current"
        current.symlink_to(self.root.name)
        root, data = self.validate(current / "runtime.json")
        self.assertEqual(root, self.root)
        self.assertEqual(data, self.manifest)
        self.assertEqual(len(self.calls), 7)
        self.assertEqual(self.calls[1][0][0], "/usr/bin/readelf")
        self.assertEqual(self.calls[2][0], ["/usr/bin/ldd", str(self.root / validator.SHELL)])
        self.assertNotIn("SOCKET_STREAM_CAPTURE_EXCLUDE", self.calls[-1][1]["env"])

    def test_equal_size_payload_tampering_is_rejected(self):
        target = self.root / validator.LIBRARIES[0]
        target.chmod(0o644)
        target.write_bytes(b"x" * target.stat().st_size)
        with self.assertRaisesRegex(validator.InvalidRuntime, "hash or size"):
            self.validate()

    def test_package_upgrade_or_partial_upgrade_is_rejected(self):
        self.observed_packages["libmutter-18-0"] = "50.1-0ubuntu2.6"
        with self.assertRaisesRegex(validator.InvalidRuntime, "versions changed"):
            self.validate()
        self.assertEqual(len(self.calls), 1)

    def test_manifest_cannot_omit_required_packages(self):
        del self.manifest["packages"]["gnome-shell-common"]
        self.save()
        with self.assertRaisesRegex(validator.InvalidRuntime, "package contract"):
            self.validate()

    def test_source_version_must_match_mutter_package_set(self):
        self.manifest["source_version"] = "50.1-0ubuntu2.4"
        self.save()
        with self.assertRaisesRegex(validator.InvalidRuntime, "source and package"):
            self.validate()

    def test_loader_directory_cannot_shadow_unrelated_system_libraries(self):
        (self.root / "lib/libc.so.6").write_bytes(b"unexpected")
        with self.assertRaisesRegex(validator.InvalidRuntime, "unexpected files"):
            self.validate()

    def test_symlink_payload_cannot_escape_runtime(self):
        target = self.root / validator.LIBRARIES[0]
        outside = self.base / "outside.so"
        target.rename(outside)
        target.symlink_to(outside)
        with self.assertRaisesRegex(validator.InvalidRuntime, "ownership, permissions, or type"):
            self.validate()

    def test_writable_runtime_and_replaceable_parent_are_rejected(self):
        self.root.chmod(0o777)
        with self.assertRaisesRegex(validator.InvalidRuntime, "directory"):
            self.validate()
        self.root.chmod(0o755)
        self.base.chmod(0o777)
        with self.assertRaisesRegex(validator.InvalidRuntime, "directory"):
            self.validate()
        self.base.chmod(0o700)

    def test_missing_member_or_incomplete_loader_is_rejected(self):
        self.loader_missing = True
        with self.assertRaisesRegex(validator.InvalidRuntime, "dependency set is incomplete"):
            self.validate()
        (self.root / validator.TYPELIBS[0]).unlink()
        with self.assertRaisesRegex(validator.InvalidRuntime, "unexpected files"):
            self.validate()

    def test_duplicate_manifest_field_and_path_traversal_are_rejected(self):
        path = self.root / "runtime.json"
        path.chmod(0o644)
        path.write_text('{"schema":1,"schema":1}')
        with self.assertRaisesRegex(validator.InvalidRuntime, "duplicate"):
            self.validate()
        self.manifest["files"]["../outside"] = {"sha256": "a" * 64, "size": 1}
        self.save()
        with self.assertRaisesRegex(validator.InvalidRuntime, "file set"):
            self.validate()

    def test_removed_launcher_execute_bit_is_rejected(self):
        (self.root / "launch-gnome-shell").chmod(0o444)
        with self.assertRaisesRegex(validator.InvalidRuntime, "not executable"):
            self.validate()

    def test_legacy_schema_without_private_shell_is_rejected(self):
        self.manifest["schema"] = 1
        self.save()
        with self.assertRaisesRegex(validator.InvalidRuntime, "unsupported runtime"):
            self.validate()

    def test_private_shell_is_a_required_executable_payload(self):
        target = self.root / validator.SHELL
        target.chmod(0o444)
        with self.assertRaisesRegex(validator.InvalidRuntime, "not executable"):
            self.validate()
        target.chmod(0o555)
        del self.manifest["files"][validator.SHELL]
        self.save()
        with self.assertRaisesRegex(validator.InvalidRuntime, "file set"):
            self.validate()

    def test_a_transitive_dso_cannot_reintroduce_stock_family_members(self):
        self.transitive_mixed = True
        with self.assertRaisesRegex(validator.InvalidRuntime, "mix stock and custom"):
            self.validate()

    def test_a_hash_valid_shell_still_needs_its_private_gi_search_path(self):
        for paths in (("RUNPATH", ""), ("RUNPATH", "$ORIGIN/lib:/usr/lib/gnome-shell"),
                      ("RPATH", validator.SHELL_RUNPATH)):
            self.search_paths[validator.SHELL] = paths
            with self.subTest(paths=paths), self.assertRaisesRegex(validator.InvalidRuntime, "GI loader contract"):
                self.validate()

    def test_every_mutter_dso_needs_local_rpath_precedence(self):
        for name in validator.LIBRARIES:
            for paths in (("RPATH", "/usr/lib/x86_64-linux-gnu/mutter-18"), ("RUNPATH", "$ORIGIN")):
                self.search_paths = {Path(name).name: paths}
                with self.subTest(name=name, paths=paths), self.assertRaisesRegex(
                        validator.InvalidRuntime, "GI loader contract"):
                    self.validate()

    def test_incomplete_duplicate_and_unexpected_elf_evidence_is_refused(self):
        complete = self.runner(["/usr/bin/readelf", "--dynamic", "--wide", str(self.root / validator.SHELL),
                                *(str(self.root / name) for name in validator.LIBRARIES)]).stdout
        for text in ("", complete + complete, complete.replace(str(self.root / validator.SHELL), "/wrong/shell"),
                     complete + " 0x1d (RUNPATH) Library runpath: [$ORIGIN]\n"):
            with self.subTest(text=text[:80]), self.assertRaises(validator.InvalidRuntime):
                validator.check_search_paths(self.root, run=lambda argv, **kw:
                                             subprocess.CompletedProcess(argv, 0, text, ""))


class PublicationTests(RuntimeFixture):
    def stage(self):
        artifacts = {name: self.root / name for name in validator.LIBRARIES + validator.TYPELIBS}
        dsc, diff = self.base / "source.dsc", self.base / "capture.patch"
        dsc.write_bytes(b"synthetic source descriptor")
        diff.write_bytes(b"synthetic capture patch")
        with patch.object(stager, "prepare_library") as relocate, patch.object(stager, "prepare_shell") as shell:
            target = stager.stage_runtime(
                artifacts, prefix=self.base / "published", dsc=dsc, patch=diff, version=VERSION,
                packages=dict(PACKAGES), run=self.runner, owner_uid=self.uid,
                shell_source=self.root / validator.SHELL,
            )
            self.assertEqual(relocate.call_count, 4)
            self.assertTrue(all(call.args[0].parent.name == "lib" for call in relocate.call_args_list))
            self.assertEqual(shell.call_count, 1)
        return target

    def test_publication_is_complete_and_idempotent_without_modifying_inputs(self):
        before = {name: (self.root / name).read_bytes() for name in validator.LIBRARIES}
        target = self.stage()
        original_inode = target.stat().st_ino
        self.assertEqual((self.base / "published/current").resolve(), target)
        self.assertEqual(self.stage(), target)
        self.assertEqual(target.stat().st_ino, original_inode)
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in validator.LIBRARIES})
        self.assertEqual(list((self.base / "published").glob(".stage-*")), [])

    def test_mixed_loader_aborts_before_publishing(self):
        self.mixed = True
        with self.assertRaisesRegex(validator.InvalidRuntime, "mix stock and custom"):
            self.stage()
        self.assertFalse((self.base / "published/current").exists())
        self.assertEqual(list((self.base / "published").iterdir()), [])

    def test_current_collision_is_preserved(self):
        prefix = self.base / "published"
        prefix.mkdir()
        (prefix / "current").write_text("user file")
        with self.assertRaisesRegex(validator.InvalidRuntime, "managed runtime symlink"):
            self.stage()
        self.assertEqual((prefix / "current").read_text(), "user file")

    def test_corrupt_existing_version_is_not_replaced(self):
        target = self.stage()
        library = target / validator.LIBRARIES[0]
        library.chmod(0o644)
        library.write_bytes(b"modified runtime")
        with self.assertRaisesRegex(validator.InvalidRuntime, "hash or size"):
            self.stage()
        self.assertEqual(library.read_bytes(), b"modified runtime")
        self.assertEqual((self.base / "published/current").resolve(), target)


class SourceDescriptorTests(unittest.TestCase):
    def test_source_archives_are_checked_and_traversal_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            descriptor = root / "mutter.dsc"
            lines = []
            for name in ("mutter_50.1.orig.tar.xz", "mutter_50.1-0ubuntu2.5.debian.tar.xz"):
                data = name.encode()
                (root / name).write_bytes(data)
                lines.append(f" {hashlib.sha256(data).hexdigest()} {len(data)} {name}\n")
            text = f"Source: mutter\nVersion: {VERSION}\nChecksums-Sha256:\n" + "".join(lines)
            descriptor.write_text(text)
            self.assertEqual(stager.source_version(descriptor), VERSION)
            (root / "mutter_50.1.orig.tar.xz").write_bytes(b"corrupt")
            with self.assertRaisesRegex(validator.InvalidRuntime, "archive is missing or has changed"):
                stager.source_version(descriptor)
            descriptor.write_text(text.replace("mutter_50.1.orig.tar.xz", "../mutter_50.1.orig.tar.xz"))
            with self.assertRaisesRegex(validator.InvalidRuntime, "invalid source archive"):
                stager.source_version(descriptor)


class GuardedLaunchTests(RuntimeFixture):
    def test_guard_consumption_precedes_private_shell_exec_in_the_main_pid(self):
        calls = []
        guard = SimpleNamespace(consume=lambda *args, **kwargs: calls.append(("consume", args, kwargs)))
        with patch.object(validator, "load_sibling", return_value=guard):
            validator.launch_runtime(
                self.root / "runtime.json", self.base / "guard.json", "ubuntu",
                run=self.runner, owner_uid=self.uid, environ={"INVOCATION_ID": "a" * 32},
                execve=lambda *args: calls.append(("exec", args)),
            )
        self.assertEqual([call[0] for call in calls], ["consume", "exec"])
        self.assertEqual(calls[0][1], (self.base / "guard.json", self.root, "ubuntu"))
        executable, argv, env = calls[1][1]
        self.assertEqual(executable, str(self.root / validator.SHELL))
        self.assertEqual(argv, [executable, "--mode=ubuntu"])
        self.assertEqual(env["SOCKET_STREAM_CAPTURE_EXCLUDE"], "1")
        self.assertEqual(env["INVOCATION_ID"], "a" * 32)

    def test_blocked_guard_and_private_exec_error_both_exec_stock_with_clean_environment(self):
        for fail_at in ("consume", "exec"):
            calls = []
            def consume(*args, **kwargs):
                calls.append("consume")
                if fail_at == "consume":
                    raise RuntimeError("blocked")
            def execute(executable, argv, env):
                calls.append(executable)
                if executable != "/usr/bin/gnome-shell":
                    raise OSError("cannot exec private Shell")
                self.assertEqual(argv, [executable, "--mode=ubuntu"])
                self.assertNotIn("SOCKET_STREAM_CAPTURE_EXCLUDE", env)
                self.assertEqual(env["LD_LIBRARY_PATH"], "/unrelated/lib")
                self.assertNotIn("GI_TYPELIB_PATH", env)
            with self.subTest(fail_at=fail_at), patch.object(
                    validator, "load_sibling", return_value=SimpleNamespace(consume=consume)):
                validator.launch_runtime(
                    self.root / "runtime.json", self.base / "guard.json", "ubuntu",
                    run=self.runner, owner_uid=self.uid, execve=execute,
                    environ={"SOCKET_STREAM_CAPTURE_EXCLUDE": "1",
                             "LD_LIBRARY_PATH": f"{self.root}/lib:/opt/socket_stream-mutter/old/lib::/unrelated/lib",
                             "GI_TYPELIB_PATH": f"{self.root}/typelib"},
                )
            self.assertEqual(calls[-1], "/usr/bin/gnome-shell")

    def test_foreign_prefix_and_trailing_slash_loader_entries_are_removed(self):
        env = validator.clean_environment(self.root, {
            "SOCKET_STREAM_CAPTURE_EXCLUDE": "1",
            "LD_LIBRARY_PATH": f"{self.root}/lib///:/srv/runtimes/50.1.socket-stream.old/lib/::/unrelated/lib/",
            "GI_TYPELIB_PATH": "/srv/runtimes/50.1.socket-stream.old/typelib///:/opt/socket_stream-mutter/current/typelib/",
        })
        self.assertEqual(env, {"LD_LIBRARY_PATH": "/unrelated/lib"})

    def test_invalid_mode_falls_back_without_forwarding_invalid_shell_options(self):
        calls = []
        validator.launch_runtime(self.root / "runtime.json", self.base / "guard.json", "ubuntu --replace",
                                 run=self.runner, owner_uid=self.uid, environ={},
                                 execve=lambda *args: calls.append(args))
        self.assertEqual(calls, [("/usr/bin/gnome-shell", ["/usr/bin/gnome-shell"], {})])
        self.assertEqual(self.calls, [])

    def test_integrity_failure_is_not_misdiagnosed_as_guard_refusal(self):
        self.observed_packages["gnome-shell"] = "50.1-0ubuntu1.6"
        stderr = io.StringIO()
        calls = []
        with patch.object(validator.sys, "stderr", stderr):
            validator.launch_runtime(self.root / "runtime.json", self.base / "guard.json", "ubuntu",
                                     run=self.runner, owner_uid=self.uid, environ={},
                                     execve=lambda *args: calls.append(args))
        self.assertIn("validation failed: Ubuntu package versions changed", stderr.getvalue())
        self.assertNotIn("guard refused", stderr.getvalue())
        self.assertEqual(calls[0][0], "/usr/bin/gnome-shell")


class LauncherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve() / "runtime with spaces"
        self.root.mkdir()
        recorder = self.root / "record.py"
        recorder.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\nprint(json.dumps({'pid':os.getpid(),'args':sys.argv[1:], 'env': "
            "{k:os.environ.get(k) for k in ('SOCKET_STREAM_CAPTURE_EXCLUDE','LD_LIBRARY_PATH','GI_TYPELIB_PATH')}}))\n")
        recorder.chmod(0o755)
        launcher = (NATIVE / "launch-gnome-shell").read_text()
        launcher = launcher.replace("ss_python=/usr/bin/python3", "ss_python=" + shlex.quote(sys.executable))
        launcher = launcher.replace("ss_stock=/usr/bin/gnome-shell", "ss_stock=" + shlex.quote(str(recorder)))
        self.launcher = self.root / "launch-gnome-shell"
        self.launcher.write_text(launcher)
        self.launcher.chmod(0o755)
        self.validator = self.root / "validate_runtime.py"
        self.validator.write_text(
            "import json,os,sys\n"
            f"with open({str(self.root / 'validator-calls')!r},'a') as stream: stream.write('called\\n')\n"
            "if '--launch' in sys.argv:\n"
            " print(json.dumps({'pid':os.getpid(),'args':sys.argv[2:],'env':dict(os.environ)}),flush=True)\n"
            " os._exit(0)\n")

    def launch(self, *, success=True, launcher=None, guarded=False, args=None, expected=None):
        if not success:
            self.validator.write_text("raise SystemExit(1)\n")
        if args is None:
            args = (["--guard-state=/safe/state.json", "--mode=ubuntu"] if guarded else
                    ["--mode=ubuntu", "literal $(echo should-not-run); 'quoted'"])
        env = dict(os.environ, SOCKET_STREAM_CAPTURE_EXCLUDE="1",
                   LD_LIBRARY_PATH=f"{self.root}/lib///:/opt/socket_stream-mutter/old/lib:/srv/rt/50.1.socket-stream.old/lib/::/unrelated/lib",
                   GI_TYPELIB_PATH=f"{self.root}/typelib/:/opt/socket_stream-mutter/current/typelib/:/srv/rt/50.1.socket-stream.old/typelib/:/unrelated/typelib")
        with subprocess.Popen([str(launcher or self.launcher), *args], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            record = json.loads(stdout)
            self.assertEqual(record["pid"], process.pid, "the main PID must survive every exec and fallback")
        if expected is not None:
            self.assertEqual(record["args"], expected)
        elif guarded and success:
            self.assertEqual(record["args"], ["--launch", "--guard-state", "/safe/state.json", "--mode=ubuntu"])
        else:
            self.assertEqual(record["args"], ["--mode=ubuntu"])
        return record["env"], stderr

    def test_valid_runtime_dispatches_same_pid_guarded_launcher_with_clean_input(self):
        env, stderr = self.launch(guarded=True)
        self.assertNotIn("SOCKET_STREAM_CAPTURE_EXCLUDE", env)
        self.assertEqual(env["LD_LIBRARY_PATH"], "/unrelated/lib")
        self.assertEqual(env["GI_TYPELIB_PATH"], "/unrelated/typelib")
        self.assertEqual(stderr, "")
        self.assertEqual((self.root / "validator-calls").read_text(), "called\n")

    def test_invalid_runtime_falls_back_and_removes_stale_exports(self):
        env, stderr = self.launch(success=False)
        self.assertIsNone(env["SOCKET_STREAM_CAPTURE_EXCLUDE"])
        self.assertEqual(env["LD_LIBRARY_PATH"], "/unrelated/lib")
        self.assertEqual(env["GI_TYPELIB_PATH"], "/unrelated/typelib")
        self.assertIn("starting stock GNOME Shell", stderr)

    def test_missing_validator_falls_back_instead_of_breaking_login(self):
        self.validator.unlink()
        env, _ = self.launch(guarded=True, expected=["--mode=ubuntu"])
        self.assertIsNone(env["SOCKET_STREAM_CAPTURE_EXCLUDE"])

    def test_broken_or_early_returning_dispatcher_still_execs_stock(self):
        for text in ("syntax error!\n", "raise RuntimeError('fixture failure')\n", "raise SystemExit(2)\n", "pass\n"):
            self.validator.write_text(text)
            with self.subTest(text=text):
                env, stderr = self.launch(guarded=True, expected=["--mode=ubuntu"])
                self.assertIsNone(env["SOCKET_STREAM_CAPTURE_EXCLUDE"])
                self.assertIn("starting stock GNOME Shell", stderr)

    def test_misordered_internal_flag_is_never_forwarded_to_stock(self):
        env, _ = self.launch(success=False, args=["--mode=ubuntu", "--guard-state=/safe/state.json"])
        self.assertIsNone(env["SOCKET_STREAM_CAPTURE_EXCLUDE"])

    def test_unknown_duplicate_or_malformed_arguments_use_only_a_safe_stock_mode(self):
        for args, expected in ((["--mode=ubuntu", "--guard-state=/safe/state.json", "--replace"], ["--mode=ubuntu"]),
                               (["--guard-state=/safe/state.json", "--mode=ubuntu", "--mode=gdm"], ["--mode=ubuntu"]),
                               (["--guard-state=/safe/state.json", "--mode=ubuntu --replace"], []),
                               (["--guard-state=relative", "--mode=ubuntu"], ["--mode=ubuntu"]),
                               (["--guard-state=/safe/state.json", "--mode=ubuntu:gdm"], []),
                               (["--guard-state=/safe/state.json", "--mode=ubuntu/gdm"], []),
                               (["--guard-state=/safe/state.json", "--mode=" + "a" * 65], [])):
            with self.subTest(args=args):
                env, stderr = self.launch(args=args, expected=expected)
                self.assertIsNone(env["SOCKET_STREAM_CAPTURE_EXCLUDE"])
                self.assertIn("starting stock GNOME Shell", stderr)

    def test_current_symlink_uses_the_physical_version_directory(self):
        current = self.root.parent / "current"
        current.symlink_to(self.root.name)
        env, _ = self.launch(launcher=current / "launch-gnome-shell", guarded=True)
        self.assertEqual(env["LD_LIBRARY_PATH"], "/unrelated/lib")

    def test_legacy_unguarded_launcher_falls_back_even_if_the_runtime_is_valid(self):
        env, stderr = self.launch()
        self.assertIsNone(env["SOCKET_STREAM_CAPTURE_EXCLUDE"])
        self.assertIn("starting stock GNOME Shell", stderr)


if __name__ == "__main__":
    unittest.main()
