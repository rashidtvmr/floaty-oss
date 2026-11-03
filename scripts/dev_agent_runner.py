#!/usr/bin/env python3
"""Bounded, explicit development task runner. Never automatically merges changes.

Inspect: python3 scripts/dev_agent_runner.py --auto-ready --limit 4
Execute only on request: python3 scripts/dev_agent_runner.py --auto-ready --execute
"""
import argparse
import concurrent.futures
import fcntl
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKLOG = ROOT / "tasks" / "backlog.json"
TASK_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
MAX_WORKERS = 4


def eligible_tasks(data):
    tasks = data["tasks"]
    ids = [task["id"] for task in tasks]
    if len(set(item.casefold() for item in ids)) != len(ids):
        raise ValueError("duplicate or case-colliding task identifiers")
    if any(not TASK_ID.fullmatch(item) for item in ids):
        raise ValueError("unsafe task identifier")
    states = {task["id"]: task["state"] for task in tasks}
    busy_owners = {task["owner"] for task in tasks
                   if task["state"] in ("in_progress", "in_review")}
    return [task for task in tasks if task["state"] == "pending"
            and task["owner"] not in busy_owners
            and all(states.get(dep) == "done" for dep in task["dependencies"])]


def select_tasks(data, requested, limit, automatic=False):
    if not 1 <= limit <= min(MAX_WORKERS, int(data["max_parallel_agents"])):
        raise ValueError("limit must be within the configured worker cap of four")
    ready = {task["id"]: task for task in eligible_tasks(data)}
    if automatic:
        if requested:
            raise ValueError("--auto-ready and --task cannot be combined")
        requested = list(ready)
    if len(set(item.casefold() for item in requested)) != len(requested):
        raise ValueError("duplicate requested task")
    if not automatic and len(requested) > limit:
        raise ValueError("requested tasks exceed worker limit")
    if any(item not in ready for item in requested):
        raise ValueError("task is missing, blocked, or already started")
    selected, owners = [], set()
    for task_id in requested:
        task = ready[task_id]
        if task["owner"] in owners:
            if automatic:
                continue
            raise ValueError("selected tasks have overlapping ownership domains")
        owners.add(task["owner"])
        selected.append(task)
        if automatic and len(selected) == limit:
            break
    return selected


def run_checked(command, cwd):
    subprocess.run(command, cwd=cwd, check=True, timeout=3600)


def git_output(root, *arguments):
    return subprocess.check_output(["git", *arguments], cwd=root, text=True)


def preflight(root, selected, worktrees):
    if git_output(root, "status", "--porcelain").strip():
        raise ValueError("working tree must be clean; commit or stash changes")
    listing = git_output(root, "worktree", "list", "--porcelain")
    other_worktrees = max(0, sum(
        line.startswith("worktree ") for line in listing.splitlines()) - 1)
    if other_worktrees + len(selected) > MAX_WORKERS:
        raise ValueError("worktree limit reached; manually review and clean old worktrees")
    for task in selected:
        task_id = task["id"]
        if not TASK_ID.fullmatch(task_id):
            raise ValueError("unsafe task identifier")
        worktree = worktrees / task_id
        if worktree.exists():
            raise FileExistsError(f"worktree already exists: {worktree}")
        branch = f"agent/{task_id.lower()}"
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=root, capture_output=True, check=False)
        if result.returncode == 0:
            raise ValueError(f"Git branch already exists: {branch}")
        if result.returncode != 1:
            raise RuntimeError("unable to check existing Git branches")


def execute_task(root, task, worktrees, codex="codex"):
    task_id = task["id"]
    if not TASK_ID.fullmatch(task_id):
        raise ValueError("unsafe task identifier")
    worktree = worktrees / task_id
    if worktree.exists():
        raise FileExistsError(f"worktree already exists: {worktree}")
    branch = f"agent/{task_id.lower()}"
    run_checked(["git", "worktree", "add", "-b", branch, str(worktree), "HEAD"], root)
    instruction = (
        f"Implement task {task_id}: {task['title']}. "
        f"Acceptance criteria: {json.dumps(task['acceptance'])}. "
        "Follow AGENTS.md and docs/ACCEPTANCE.md. "
        "Never merge, modify task states, bypass sandboxing, or access "
        "screen/audio/private credentials. Add regression tests."
    )
    run_checked(
        [codex, "exec", "--sandbox", "workspace-write",
         "--skip-git-repo-check", "--", instruction], worktree)
    for command in (
        ["cargo", "fmt", "--all", "--", "--check"],
        ["cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"],
        ["cargo", "test", "--workspace"],
        ["python3", "-m", "unittest", "discover", "-s", "tests", "-q"],
    ):
        run_checked(command, worktree)
    print(f"{task_id}: gates passed; review {branch} before a manual merge")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backlog", type=Path, default=BACKLOG)
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--auto-ready", action="store_true")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--worktrees", type=Path, default=ROOT.parent / "context-lens-worktrees")
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.backlog.read_text(encoding="utf-8"))
        selected = select_tasks(data, args.task, args.limit, args.auto_ready)
        if not args.execute:
            print(json.dumps({
                "ready": [task["id"] for task in eligible_tasks(data)],
                "selected": [task["id"] for task in selected],
                "worker_cap": min(MAX_WORKERS, int(data["max_parallel_agents"])),
            }, indent=2))
            return 0
        if not selected:
            parser.error("no ready task selected")
        root = args.backlog.resolve().parent.parent
        if not (root / ".git").is_dir():
            parser.error("backlog must be in a Git repository")
        worktrees = args.worktrees.resolve()
        with (root / ".git" / "contextlens-runner.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            preflight(root, selected, worktrees)
            worktrees.mkdir(parents=True, exist_ok=True)
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.limit) as executor:
                futures = [executor.submit(execute_task, root, task, worktrees)
                           for task in selected]
                for future in futures:
                    future.result()
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError,
            subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"runner: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
