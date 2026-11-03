#!/usr/bin/env python3
"""Inspect a dependency-aware task queue. Explicit --claim is needed to mutate it.
No agent execution happens automatically; this only manages work assignment.
"""
import argparse
import json
import os
import sys
from pathlib import Path

BACKLOG = Path(__file__).resolve().parent.parent / "tasks" / "backlog.json"

def ready(tasks):
    state = {task["id"]: task["state"] for task in tasks}
    return [
        task for task in tasks
        if task["state"] == "pending"
        and all(state.get(dep) == "done" for dep in task["dependencies"])
    ]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="list ready tasks (default)")
    parser.add_argument("--claim", help="claim a ready task")
    parser.add_argument("--worker", help="worker identity for --claim")
    args = parser.parse_args()
    with BACKLOG.open(encoding="utf-8") as handle:
        data = json.load(handle)
    tasks = data["tasks"]
    if args.claim:
        if not args.worker or any(c in args.worker for c in "/\\\n"):
            parser.error("--claim requires a safe --worker name")
        # Claim under an advisory lock to prevent duplicate ownership.
        if os.name != "posix":
            parser.error("task claiming currently requires POSIX")
        import fcntl
        with BACKLOG.open("r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            data = json.load(f)
            matches = [t for t in ready(data["tasks"]) if t["id"] == args.claim]
            if not matches:
                parser.error("task is not ready or not found")
            task = matches[0]
            task["state"] = "in_progress"
            task["assigned_to"] = args.worker
            f.seek(0)
            json.dump(data, f, indent=2)
            f.write("\n")
            f.truncate()
        print(f"Claimed {args.claim} by {args.worker}")
    else:
        print("Ready tasks:")
        for task in ready(tasks):
            print(f'  {task["id"]}: {task["title"]}')
        print(f'Maximum workers: {data["max_parallel_agents"]}')

if __name__ == "__main__":
    main()
