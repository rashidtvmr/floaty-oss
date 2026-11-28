"""Bounded background refreshes with main-loop delivery and stale-result guards.

This module has no GTK dependency. ``dispatch`` must queue its callback on the
UI thread (normally ``GLib.idle_add``); ``read`` must not access UI widgets.
At most one worker and one coalesced follow-up request belong to each instance.
"""
import threading


class BackgroundRefresh:
    def __init__(self, read, apply, dispatch, *, start=None):
        self._read = read
        self._apply = apply
        self._dispatch = dispatch
        self._start = start or self._start_thread
        self._lock = threading.Lock()
        self._generation = 0
        self._busy = False
        self._pending = False
        self._closed = False

    @staticmethod
    def _start_thread(work):
        threading.Thread(target=work, daemon=True).start()

    def request(self):
        """Start one read, or remember one follow-up without blocking the UI."""
        with self._lock:
            if self._closed:
                return False
            if self._busy:
                self._pending = True
                return False
            self._busy = True
            self._pending = False
            generation = self._generation
        try:
            self._start(lambda: self._run(generation))
        except Exception as error:  # thread creation can fail during shutdown
            self._deliver(generation, None, type(error).__name__)
        return True

    def invalidate(self):
        """Reject an old read before an explicitly requested state mutation."""
        with self._lock:
            self._generation += 1
            self._pending = False

    def close(self):
        """Discard pending and late results; never join a worker on the UI."""
        with self._lock:
            self._closed = True
            self._generation += 1
            self._pending = False

    def _run(self, generation):
        with self._lock:
            current = not self._closed and generation == self._generation
        if not current:
            self._deliver(generation, None, None)
            return
        try:
            value, error = self._read(), None
        except Exception as exc:  # a failed probe is data, not a UI exception
            value, error = None, type(exc).__name__
        self._deliver(generation, value, error)

    def _deliver(self, generation, value, error):
        try:
            self._dispatch(self._finish, generation, value, error)
        except Exception:
            # A stopped main context cannot accept callbacks. Leave no stuck
            # in-flight flag; no worker may apply a result to widgets itself.
            with self._lock:
                self._busy = False
                self._pending = False

    def _finish(self, generation, value, error):
        with self._lock:
            self._busy = False
            current = not self._closed and generation == self._generation
            again = not self._closed and self._pending
            self._pending = False
        try:
            if current:
                self._apply(value, error)
        finally:
            if again:
                self.request()
        return False
