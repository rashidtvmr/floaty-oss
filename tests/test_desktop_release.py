"""Exercise the GitHub publisher shell against a local fake gh, without uploading."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/desktop-builds.yml"
ARCHIVES = ("socket_stream_macos-arm64-test.zip", "socket_stream_linux-x86_64-test.tar.gz")


@unittest.skipUnless(shutil.which("bash") and shutil.which("sha256sum"), "requires bash and sha256sum")
class DesktopReleaseTest(unittest.TestCase):
    def run_publisher(self, *, tag="v1.2.3", mode="ok", corrupt=False, missing=False):
        # The publisher is the last step in the workflow; run its actual script.
        script = textwrap.dedent(WORKFLOW.read_text().split("          TAG: ${{ github.ref_name }}\n        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ARCHIVES[:1] if missing else ARCHIVES:
                payload = name.encode()
                (root / name).write_bytes(payload)
                digest = hashlib.sha256(payload).hexdigest()
                (root / (name + ".sha256")).write_text(f"{digest}  {name}\n")
            if corrupt:
                (root / ARCHIVES[0]).write_bytes(b"not the verified archive")
            fake = root / "gh"
            fake.write_text(textwrap.dedent('''\
                #!/bin/bash
                printf '%s\n' "$*" >> calls
                case "$2" in
                  view)
                    if [[ "$#" == 3 ]]; then [[ "$MODE" == existing ]]; exit $?; fi
                    if [[ "$MODE" == incomplete ]]; then head -n 1 uploaded; else cat uploaded; fi ;;
                  create)
                    [[ "$MODE" != failed ]] || exit 1
                    printf '%s\n' "$4" "$5" "$6" "$7" > uploaded ;;
                  edit) ;;
                  *) exit 2 ;;
                esac
                '''))
            fake.chmod(0o755)
            env = dict(os.environ, PATH=f"{root}{os.pathsep}{os.environ['PATH']}", TAG=tag, MODE=mode)
            result = subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=root,
                                    env=env, capture_output=True, text=True, timeout=10)
            calls = (root / "calls").read_text().splitlines() if (root / "calls").exists() else []
        return result, calls

    def test_builds_draft_first_then_publishes_both_archives_and_checksums(self):
        for tag in ("v1.2.3", "v1.2.3-rc.1"):
            with self.subTest(tag=tag):
                result, calls = self.run_publisher(tag=tag)
                self.assertEqual(result.returncode, 0, result.stderr)
                for archive in ARCHIVES:
                    self.assertIn(f"{archive} {archive}.sha256", calls[1])
                self.assertIn("--verify-tag --draft", calls[1])
                self.assertEqual("--prerelease" in calls[1], tag.endswith("-rc.1"))
                self.assertEqual(calls[-1], f"release edit {tag} --draft=false")

    def test_rejects_bad_inputs_before_contacting_github(self):
        for kwargs in ({"tag": "vnot-a-release"}, {"corrupt": True}, {"missing": True}):
            with self.subTest(kwargs=kwargs):
                result, calls = self.run_publisher(**kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_existing_release_and_upload_failures_never_publish(self):
        for mode in ("existing", "failed", "incomplete"):
            with self.subTest(mode=mode):
                result, calls = self.run_publisher(mode=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(call.startswith("release edit ") for call in calls))
