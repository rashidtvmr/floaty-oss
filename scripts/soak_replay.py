#!/usr/bin/env python3
"""Bounded replay soak test for the socket_stream JSONL fixture path."""
import argparse
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests/fixtures/mock_provider/events.jsonl"
REPLAY_TIMEOUT_SECONDS = 30
MAX_SOAK_MINUTES = 24 * 60
RSS_INITIAL_SAMPLE_SECONDS = 0.1


def cli_path(root=ROOT):
    candidates = (
        root / "target/debug/socket_stream",
        root / "target/debug/bin/socket_stream",
        root / "bin/socket_stream",
    )
    return next((path for path in candidates if path.is_file()), None)


def process_group_children(process_group):
    """Return remaining members of a replay's private process group, or None if ps failed."""
    try:
        output = subprocess.check_output(
            ["ps", "-axo", "pid=,pgid="], text=True, stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        return None
    members = set()
    try:
        for line in output.splitlines():
            pid_text, pgid_text = line.split()
            pid, pgid = int(pid_text), int(pgid_text)
            if pgid == process_group and pid != process_group:
                members.add(pid)
    except (ValueError, TypeError):
        return None
    return members


def rss_kb(pid):
    """Read a process's resident set size in KiB using the system ps command."""
    try:
        output = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(pid)], text=True, stderr=subprocess.DEVNULL
        ).strip()
        value = int(output) if output else 0
        return value if value > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def sample_process_rss(process):
    """Sample fast CLI starts; briefly stop a still-live child if ps missed exec."""
    if process.poll() is not None:
        return None
    time.sleep(0.001)
    if process.poll() is not None:
        return None
    try:
        os.kill(process.pid, signal.SIGSTOP)
    except ProcessLookupError:
        return None
    try:
        return rss_kb(process.pid)
    finally:
        try:
            os.kill(process.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass


def signal_group(process_group, signum):
    try:
        os.killpg(process_group, signum)
    except ProcessLookupError:
        pass
    except OSError:
        pass


def stop_group(process_group, grace_seconds=1):
    """Best-effort cleanup for a replay and any descendants it left behind."""
    signal_group(process_group, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        members = process_group_children(process_group)
        if members == set():
            return
        time.sleep(0.05)
    signal_group(process_group, signal.SIGKILL)


def _error_text(stderr_file):
    stderr_file.flush()
    stderr_file.seek(0, os.SEEK_END)
    end = stderr_file.tell()
    stderr_file.seek(max(0, end - 500))
    return stderr_file.read(500).decode("utf-8", errors="replace")


def soak(minutes=30, max_growth_mb=128, fixture=DEFAULT_FIXTURE, dry_run=False):
    if (
        not math.isfinite(minutes)
        or minutes <= 0
        or minutes > MAX_SOAK_MINUTES
        or not math.isfinite(minutes * 60)
    ):
        raise RuntimeError(f"minutes must be finite and between 0 and {MAX_SOAK_MINUTES}")
    if not math.isfinite(max_growth_mb) or max_growth_mb < 0:
        raise RuntimeError("max-growth-mb must be finite and non-negative")

    binary = cli_path()
    if binary is None:
        raise RuntimeError("socket_stream not found; run `cargo build -p socket_stream`")
    if not fixture.is_file():
        raise RuntimeError(f"replay fixture not found: {fixture}")

    deadline = None if dry_run else time.monotonic() + minutes * 60
    max_iterations = 3 if dry_run else None
    baseline = peak = None
    completed = 0
    interrupted = False
    orphan_count = 0
    orphan_check_available = True
    process = None
    old_handler = signal.getsignal(signal.SIGINT)

    def interrupt(_signum, _frame):
        nonlocal interrupted
        interrupted = True
        if process is not None and process.poll() is None:
            # The replay is isolated in its own process group so the soak can
            # forward SIGINT once and then reap it without interrupting siblings.
            signal_group(process.pid, signal.SIGINT)

    signal.signal(signal.SIGINT, interrupt)
    try:
        while max_iterations is None or completed < max_iterations:
            if interrupted or (deadline is not None and time.monotonic() >= deadline):
                break
            timeout = REPLAY_TIMEOUT_SECONDS
            if deadline is not None:
                timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0:
                break

            with tempfile.TemporaryFile() as stderr_file:
                process = subprocess.Popen(
                    [str(binary), "replay-events", "--file", str(fixture)],
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    start_new_session=True,
                )
                started = time.monotonic()
                if interrupted:
                    signal_group(process.pid, signal.SIGINT)

                current = sample_process_rss(process)
                if current is not None:
                    baseline = current if baseline is None else baseline
                    peak = current if peak is None else max(peak, current)

                while process.poll() is None:
                    if interrupted:
                        break
                    if time.monotonic() - started >= timeout:
                        stop_group(process.pid)
                        process.wait()
                        raise RuntimeError("replay child timed out")
                    elapsed = time.monotonic() - started
                    time.sleep(0.002 if elapsed < RSS_INITIAL_SAMPLE_SECONDS else 0.05)
                    current = rss_kb(process.pid)
                    if current is not None:
                        baseline = current if baseline is None else baseline
                        peak = current if peak is None else max(peak, current)

                if process.poll() is None:
                    # SIGINT was delivered. Allow a brief graceful exit before
                    # escalating, but always reap the direct child.
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        stop_group(process.pid)
                        process.wait()
                else:
                    process.wait()

                members = process_group_children(process.pid)
                if members is None:
                    orphan_check_available = False
                elif members:
                    orphan_count += len(members)
                    stop_group(process.pid)

                if process.returncode != 0 and not interrupted:
                    raise RuntimeError(
                        f"replay failed (exit {process.returncode}): {_error_text(stderr_file)}"
                    )

            if interrupted:
                break
            if orphan_count:
                raise RuntimeError(f"replay left {orphan_count} orphan process(es)")
            if not orphan_check_available:
                raise RuntimeError("could not verify replay child cleanup with ps")
            completed += 1
            if not dry_run and deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(1, remaining))
    finally:
        signal.signal(signal.SIGINT, old_handler)
        if process is not None and process.poll() is None:
            signal_group(process.pid, signal.SIGINT if interrupted else signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                stop_group(process.pid)
                process.wait()

    growth_mb = None if baseline is None or peak is None else max(0, peak - baseline) / 1024
    report = {
        "iterations": completed,
        "interrupted": interrupted,
        "baseline_rss_mb": None if baseline is None else round(baseline / 1024, 2),
        "peak_rss_mb": None if peak is None else round(peak / 1024, 2),
        "rss_growth_mb": None if growth_mb is None else round(growth_mb, 2),
        "max_growth_mb": max_growth_mb,
        "orphan_children": orphan_count if orphan_check_available else None,
    }
    passed = (
        growth_mb is not None
        and growth_mb <= max_growth_mb
        and not interrupted
        and orphan_check_available
        and orphan_count == 0
    )
    return report, passed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--max-growth-mb", type=float, default=128)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--dry-run", action="store_true", help="run three quick iterations")
    args = parser.parse_args(argv)
    if not math.isfinite(args.minutes) or not 0 < args.minutes <= MAX_SOAK_MINUTES:
        parser.error(f"minutes must be finite and between 0 and {MAX_SOAK_MINUTES}")
    if not math.isfinite(args.max_growth_mb) or args.max_growth_mb < 0:
        parser.error("max-growth-mb must be finite and non-negative")
    try:
        report, passed = soak(args.minutes, args.max_growth_mb, args.fixture, args.dry_run)
    except RuntimeError as error:
        print(f"soak failed: {error}", file=sys.stderr)
        return 1
    print("Soak report: " + ", ".join(f"{key}={value}" for key, value in report.items()))
    if not passed:
        print("RSS growth limit exceeded, RSS unavailable, or soak interrupted.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
