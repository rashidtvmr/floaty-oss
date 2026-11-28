"""Screen-to-model fidelity with synthetic structural accessibility branches.

The CLI and HTTP request path are real; all window content and AI responses
are fixtures. No native desktop, screenshot, audio, or external service is used.
"""
import json
import unittest

import test_incremental_screen_e2e as screen_e2e


class TreeProjectionE2E(unittest.TestCase):
    def setUp(self):
        self.fixture = screen_e2e.IncrementalScreenE2E(
            "test_observe_requests_use_snapshots_patches_and_conversation_history"
        )
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def structural_tree(self):
        nodes = [{"id": 1, "stable_id": "root", "parent": None, "role": "frame"}]
        next_id = 2
        # Each branch stays below the native helper's 80-child cap. The whole
        # 352-node tree also fits its default node/depth bounds.
        for group in range(5):
            parent = next_id
            next_id += 1
            nodes.append({"id": parent, "stable_id": f"group:{group}",
                          "parent": 1, "role": "panel"})
            for _ in range(69):
                nodes.append({"id": next_id, "stable_id": f"blank:{next_id}",
                              "parent": parent, "role": "panel"})
                next_id += 1
        nodes.append({"id": next_id, "stable_id": "question", "parent": 1,
                      "role": "paragraph",
                      "text": "Explain why binary search requires sorted input."})
        return {"schema": 1, "source": "atspi", "app": "Synthetic fixture", "pid": 42,
                "window_id": "window:1", "truncated": False, "nodes": nodes}

    def save_tree(self, tree):
        temporary = self.fixture.tree_file.with_suffix(".new")
        temporary.write_text(json.dumps(tree))
        temporary.replace(self.fixture.tree_file)

    def test_empty_structural_branches_cannot_hide_the_question_from_the_model(self):
        tree = self.structural_tree()
        self.save_tree(tree)
        self.fixture.start()
        update = self.fixture.update()
        self.assertTrue(any(node.get("text") == tree["nodes"][-1]["text"]
                            for node in update["nodes"]),
                        "The UI can read the question, so model context must retain it too")
        self.assertTrue(update["complete"])
        self.assertEqual(len(update["nodes"]), 2)
        self.assertLess(len(json.dumps(update)), 1000)

    def test_newly_meaningful_branches_keep_ancestors_and_reconcile_stable_question(self):
        tree = self.structural_tree()
        self.save_tree(tree)
        self.fixture.start()
        initial = self.fixture.update()
        self.assertTrue(initial["complete"])
        question = next(node for node in initial["nodes"] if node.get("text"))

        tree["nodes"][2]["text"] = "A newly readable detail inside this panel"
        self.save_tree(tree)
        self.fixture.read()
        update = self.fixture.update()
        self.assertEqual(update["kind"], "patch")
        self.assertEqual(len(update["added"]), 2)  # detail plus its empty ancestor
        self.assertEqual(update["updated"], [])
        self.assertEqual(update["removed"], [])
        known_ids = {node["id"] for node in initial["nodes"] + update["added"]}
        self.assertTrue(all(node["parent"] in known_ids for node in update["added"]))
        self.assertTrue(any(node["id"] == question["id"] for node in update["moved"]))
        self.assertNotIn(question["text"], self.fixture.prompt())

        del tree["nodes"][2]["text"]
        self.save_tree(tree)
        self.fixture.read()
        removed = self.fixture.update()
        self.assertEqual(set(removed["removed"]), {node["id"] for node in update["added"]})
        self.assertEqual(removed["updated"], [])
        self.assertNotIn(question["text"], self.fixture.prompt())


if __name__ == "__main__":
    unittest.main()
