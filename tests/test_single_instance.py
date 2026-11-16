import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'apps/socket_stream_desktop/ui'))
from single_instance import acquire, _notify_existing


class SingleInstanceTests(unittest.TestCase):
    def test_duplicate_refuses_second_instance_and_notifies_only_ready_owner(self):
        with tempfile.TemporaryDirectory() as runtime:
            calls = []
            first = acquire('/fixture/linux-gtk.py', runtime=runtime)
            try:
                self.assertIsNone(acquire('/fixture/linux-gtk.py', runtime=runtime, notify=lambda *args: calls.append(args)))
                self.assertEqual(calls, [], 'startup must not signal before SIGUSR1 is installed')
                first.ready()
                self.assertIsNone(acquire('/fixture/linux-gtk.py', runtime=runtime, notify=lambda *args: calls.append(args)))
                self.assertEqual(calls, [(os.getpid(), '/fixture/linux-gtk.py')])
                lock = Path(runtime) / 'socket-stream-desktop/gtk.lock'
                self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
            finally:
                first.close()
            restarted = acquire('/fixture/linux-gtk.py', runtime=runtime)
            self.assertIsNotNone(restarted)
            restarted.close()

    def test_symlinks_and_unsafe_permissions_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as runtime:
            directory = Path(runtime) / 'socket-stream-desktop'
            directory.mkdir(mode=0o700)
            target = Path(runtime) / 'must-preserve'
            target.write_text('unchanged')
            lock = directory / 'gtk.lock'
            lock.symlink_to(target)
            with self.assertRaises(OSError):
                acquire('/fixture/linux-gtk.py', runtime=runtime)
            self.assertEqual(target.read_text(), 'unchanged')
            lock.unlink()
            lock.write_text('unchanged')
            lock.chmod(0o644)
            with self.assertRaises(ValueError):
                acquire('/fixture/linux-gtk.py', runtime=runtime)
            self.assertEqual(lock.read_text(), 'unchanged')

    def test_unrelated_current_process_is_never_signalled(self):
        # This test runner is not the requested GTK script; verification must
        # refuse it even if a stale/malformed lock names this live PID.
        self.assertFalse(_notify_existing(os.getpid(), '/not-the-running-program/linux-gtk.py'))

    def test_empty_xdg_values_use_private_home_cache_and_relative_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {'HOME': home, 'XDG_RUNTIME_DIR': '', 'XDG_CACHE_HOME': ''}):
                guard = acquire('/fixture/linux-gtk.py')
                try:
                    self.assertTrue((Path(home) / '.cache/socket-stream-desktop/gtk.lock').is_file())
                finally:
                    guard.close()
            with self.assertRaises(ValueError):
                acquire('/fixture/linux-gtk.py', runtime='relative-runtime')
