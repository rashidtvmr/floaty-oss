import importlib.util
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dev_agent_runner", ROOT / "scripts/dev_agent_runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

def task(id, state="pending", deps=None, owner="core"):
    return {"id": id, "state": state, "dependencies": deps or [], "owner": owner,
            "title": "Example task", "acceptance": ["Pass tests"]}

class RunnerTests(unittest.TestCase):
    def test_dependencies(self):
        data = {"max_parallel_agents": 4, "tasks": [task("A", "done"), task("B", deps=["A"]), task("C", deps=["B"])]}
        self.assertEqual([t["id"] for t in runner.eligible_tasks(data)], ["B"])

    def test_ownership_conflicts(self):
        data = {"max_parallel_agents": 4, "tasks": [task("A"), task("B")]}
        with self.assertRaises(ValueError):
            runner.select_tasks(data, ["A", "B"], 2)

    def test_worker_limit(self):
        data = {"max_parallel_agents": 20, "tasks": [task("A")]}
        with self.assertRaises(ValueError):
            runner.select_tasks(data, ["A"], 5)

    def test_path_traversal_rejected(self):
        data = {"max_parallel_agents": 4, "tasks": [task("../evil")]}
        with self.assertRaises(ValueError):
            runner.select_tasks(data, ["../evil"], 1)

    def test_dry_run_does_not_invoke_codex(self):
        with mock.patch.object(runner, "execute_task", side_effect=AssertionError("must not execute")):
            self.assertEqual(runner.main([]), 0)


    def test_case_insensitive_task_collisions_rejected(self):
        data = {"max_parallel_agents": 4, "tasks": [task("OCR"), task("ocr")]}
        with self.assertRaisesRegex(ValueError, "case-colliding"):
            runner.eligible_tasks(data)

    def test_auto_selects_distinct_domains(self):
        data = {"max_parallel_agents": 4, "tasks": [
            task("A", owner="capture"), task("B", owner="capture"),
            task("C", owner="models"), task("D", owner="audio")
        ]}
        selected = runner.select_tasks(data, [], 2, automatic=True)
        self.assertEqual([item["id"] for item in selected], ["A", "C"])

    def test_busy_owners_not_claimed(self):
        data = {"max_parallel_agents": 4, "tasks": [
            task("A", state="in_progress", owner="capture"),
            task("B", owner="capture"), task("C", owner="models")
        ]}
        self.assertEqual([t["id"] for t in runner.eligible_tasks(data)], ["C"])

    def test_preflight_rejects_dirty_repo(self):
        with mock.patch.object(runner, "git_output", return_value=" M code.rs\n"):
            with self.assertRaisesRegex(ValueError, "working tree"):
                runner.preflight(ROOT, [task("A")], ROOT / "worktrees")

    def test_preflight_rejects_path_and_branch_collision(self):
        with mock.patch.object(runner, "git_output", side_effect=["", "worktree /repo\n"]):
            with mock.patch("pathlib.Path.exists", return_value=True):
                with self.assertRaises(FileExistsError):
                    runner.preflight(ROOT, [task("A")], ROOT / "worktrees")
        with mock.patch.object(runner, "git_output", side_effect=["", "worktree /repo\n"]):
            with mock.patch("pathlib.Path.exists", return_value=False):
                with mock.patch.object(runner.subprocess, "run",
                                       return_value=mock.Mock(returncode=0)):
                    with self.assertRaisesRegex(ValueError, "branch already exists"):
                        runner.preflight(ROOT, [task("A")], ROOT / "worktrees")

    def test_codex_is_sandboxed_and_no_auto_merge(self):
        import inspect
        source = inspect.getsource(runner.execute_task)
        self.assertIn("workspace-write", source)
        self.assertNotIn("dangerously-bypass", source)
        self.assertNotIn('"merge"', source)

if __name__ == "__main__":
    unittest.main()
