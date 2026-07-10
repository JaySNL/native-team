# tests/test_protocol.py
import unittest

from team import protocol


class TaskBodyTest(unittest.TestCase):
    def test_scope_paths_are_rendered_as_a_bulleted_list(self):
        body = protocol.task_body("007", "where is X?", ["a.py", "b/c.py"])
        self.assertIn("  - a.py\n  - b/c.py", body)

    def test_empty_scope_falls_back_to_none_given(self):
        """Without the fallback, an empty scope renders as a blank line and the
        grunt reads the section as truncated rather than deliberately empty.
        """
        body = protocol.task_body("007", "where is X?", [])
        self.assertIn("  (none given)", body)

    def test_question_is_stripped(self):
        """A question pasted with a trailing newline would otherwise inject a
        blank line into the middle of the rendered template.
        """
        padded = protocol.task_body("007", "  where is X?\n\n", ["a.py"])
        clean = protocol.task_body("007", "where is X?", ["a.py"])
        self.assertEqual(padded, clean)

    def test_task_id_appears_in_the_body(self):
        self.assertIn("007", protocol.task_body("007", "q", ["a.py"]))

    def test_body_is_a_string_and_mentions_the_question(self):
        body = protocol.task_body("012", "where does aggro clamp?", ["s.cs"])
        self.assertIsInstance(body, str)
        self.assertIn("where does aggro clamp?", body)

    def test_body_directs_the_grunt_to_grep_for_line_numbers(self):
        """Measured: a grunt told only to cite a line reads the file and
        estimates the number, quoting the source correctly and citing it 4 to
        228 lines off. Told to use `grep -n`, the same grunt on the same
        question cites both lines exactly. The instruction is load-bearing.
        """
        body = protocol.task_body("007", "q", ["a.py"])
        self.assertIn("grep -n", body)

    def test_template_renders_the_literal_braces_it_documents(self):
        """TEMPLATE is a .format() string, so a `{` in the prose must be
        escaped. An unescaped one raises ValueError for every task ever sent.
        """
        body = protocol.task_body("007", "q", ["a.py"])
        self.assertIn("trailing `;` or `{`", body)
        self.assertNotIn("{{", body)


if __name__ == "__main__":
    unittest.main()
