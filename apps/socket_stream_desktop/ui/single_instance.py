"""Per-user GTK launcher guard. Never starts capture or signals unrelated jobs."""
import fcntl
import os
from pathlib import Path
import signal
import stat


def _notify_existing(pid, main_file):
    pidfd = None
    try:
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            return False  # Fail closed rather than signal a potentially reused PID.
        pidfd = os.pidfd_open(pid)
        proc = Path('/proc') / str(pid)
        if proc.stat().st_uid != os.getuid():
            return False
        with (proc / 'cmdline').open('rb') as handle:
            command = handle.read(65537)
        if len(command) > 65536:
            return False
        args = command.split(b'\0')
        if len(args) < 2 or not Path(os.fsdecode(args[0])).name.startswith('python'):
            return False
        if Path(os.fsdecode(args[1])).resolve() != Path(main_file).resolve():
            return False
        signal.pidfd_send_signal(pidfd, signal.SIGUSR1)
        return True
    except (OSError, ValueError):
        return False
    finally:
        if pidfd is not None:
            os.close(pidfd)


class InstanceGuard:
    def __init__(self, fd):
        self.fd = fd

    def ready(self):
        # Publish only after main() installed its SIGUSR1 handler.
        os.ftruncate(self.fd, 0)
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.write(self.fd, (str(os.getpid()) + '\n').encode())

    def close(self):
        if self.fd is not None:
            try:
                os.ftruncate(self.fd, 0)
            finally:
                os.close(self.fd)
                self.fd = None


def acquire(main_file, *, runtime=None, notify=_notify_existing):
    """Return the held guard, or None when another launcher already owns it."""
    root = Path(runtime or os.environ.get('XDG_RUNTIME_DIR') or
                os.environ.get('XDG_CACHE_HOME') or (Path.home() / '.cache'))
    if not root.is_absolute():
        raise ValueError('Application runtime directory must be absolute')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    private = None
    fd = None
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError('Unsafe application runtime directory')
        try:
            os.mkdir('socket-stream-desktop', mode=0o700, dir_fd=directory)
        except FileExistsError:
            pass
        private = os.open('socket-stream-desktop', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        info = os.fstat(private)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Unsafe application instance directory')
        fd = os.open('gtk.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=private)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Unsafe application instance lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raw = os.pread(fd, 64, 0).strip()
            if raw.isdigit() and 1 < int(raw) <= 2**31 - 1:
                notify(int(raw), main_file)
            return None
        os.ftruncate(fd, 0)
        guard = InstanceGuard(fd)
        fd = None
        return guard
    finally:
        if fd is not None:
            os.close(fd)
        if private is not None:
            os.close(private)
        os.close(directory)
