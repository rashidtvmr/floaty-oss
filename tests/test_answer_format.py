import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/socket_stream_desktop/ui"))
from answer_format import answer_parts, markdown_runs, question_title


class AnswerFormatTests(unittest.TestCase):
    def test_streaming_envelope_never_echoes_question_in_answer_area(self):
        answer = "Question: Find two indices.\nAnswer: Use a hash map."
        split = answer.index("Use a hash map.")
        for index in range(1, split):
            self.assertEqual(answer_parts(answer[:index])[1], "", index)
        self.assertEqual(answer_parts(answer), ("Find two indices.", "Use a hash map."))

    def test_plain_answers_and_code_containing_answer_label_are_not_stripped(self):
        raw = "Use this:\n```python\nprint('Answer: yes')\n```"
        self.assertEqual(answer_parts(raw), ("", raw))
        self.assertEqual(answer_parts("**Answer:** hello"), ("", "hello"))

    def test_code_preserves_operators_indentation_and_markdown_characters(self):
        raw = "## Solution\n```python\ndef f(x):\n    return x ** 2  # `code`\n```\n**Time:** O(n)\n"
        runs = markdown_runs(raw)
        self.assertIn(("Solution\n", "heading"), runs)
        self.assertIn(("PYTHON\n", "code_language"), runs)
        self.assertIn(("    return x ** 2  # `code`\n", "code_block"), runs)
        self.assertIn(("Time:", "strong"), runs)
        self.assertNotIn("```", "".join(text for text, _ in runs))

    def test_partial_fence_remains_code_until_stream_finishes(self):
        self.assertEqual(markdown_runs("```rust\n    let value = 1;"), [
            ("RUST\n", "code_language"), ("    let value = 1;", "code_block")])
        runs = markdown_runs("~~~\n**literal**\n~~~\n- **Done** with `x`\n")
        self.assertIn(("**literal**\n", "code_block"), runs)
        self.assertIn(("•  ", "body"), runs)
        self.assertIn(("x", "code"), runs)

    def test_title_uses_summary_and_bounds_without_losing_original(self):
        question = "A large\n recognized task " * 100
        self.assertLessEqual(len(question_title(question)), 220)
        self.assertEqual(question_title(question, "Question: Two Sum\nAnswer: A"), "Two Sum")
        self.assertEqual(question_title("line one\nline two"), "line one line two")

    def test_html_and_uris_are_inert_text(self):
        text = "<img src='https://example.test/track'>\n[open](file:///private/data)"
        self.assertEqual("".join(part for part, _ in markdown_runs(text)), text)


if __name__ == "__main__":
    unittest.main()
