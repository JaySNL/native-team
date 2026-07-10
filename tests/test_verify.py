import tempfile, unittest
from pathlib import Path

from team import verify

SRC = (
    "using System;\n"
    "\n"
    "public class TreatmentBed {\n"
    "    public bool TryHeal(Character c, float amount) {\n"
    "        return true;\n"
    "    }\n"
    "}\n"
)


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "TreatmentBed.cs").write_text(SRC)

    def tearDown(self):
        self.tmp.cleanup()

    def rec(self, **kw):
        base = {"file": "src/TreatmentBed.cs", "line": 4, "symbol": "TryHeal",
                "evidence": "    public bool TryHeal(Character c, float amount) {"}
        base.update(kw)
        return base

    def test_exact_match_passes(self):
        self.assertEqual(verify.verify_record(self.root, self.rec()).status, "PASS")

    def test_whitespace_drift_still_passes(self):
        v = verify.verify_record(
            self.root,
            self.rec(evidence="public bool TryHeal(Character c, float amount) {"))
        self.assertEqual(v.status, "PASS")

    def test_off_by_n_detected_and_reports_actual_line(self):
        v = verify.verify_record(self.root, self.rec(line=6))
        self.assertEqual(v.status, "OFF_BY")
        self.assertIn("actual 4", v.detail)

    def test_fabricated_evidence_detected(self):
        v = verify.verify_record(
            self.root,
            self.rec(symbol="Heal", evidence="    public void HealEverything() {"))
        self.assertEqual(v.status, "FABRICATED")

    def test_symbol_not_in_evidence_detected(self):
        v = verify.verify_record(self.root, self.rec(symbol="Nonexistent"))
        self.assertEqual(v.status, "SYMBOL_MISMATCH")

    def test_missing_file_detected(self):
        v = verify.verify_record(self.root, self.rec(file="src/Ghost.cs"))
        self.assertEqual(v.status, "NO_FILE")

    def test_line_beyond_eof_is_off_by_not_crash(self):
        v = verify.verify_record(self.root, self.rec(line=9999))
        self.assertEqual(v.status, "OFF_BY")

    def test_render_table_counts_and_any_failed(self):
        vs = verify.verify_records(self.root, [self.rec(), self.rec(line=6)])
        table = verify.render_table("001", vs)
        self.assertIn("2 records", table)
        self.assertIn("1 PASS", table)
        self.assertIn("1 FAIL", table)
        self.assertTrue(verify.any_failed(vs))

    def test_line_separator_char_in_string_literal(self):
        """A LINE SEPARATOR (U+2028) in a string should not confuse line counting.
        splitlines() incorrectly splits on U+2028, causing off-by-one.
        This test must pass with split("\\n") but fails with splitlines()."""
        content = (
            '  s = "hello' + ' ' + 'world";\n'  # LINE SEPARATOR in string
            '  return x;\n'
        )
        (self.root / "src" / "Test.cs").write_text(content)
        v = verify.verify_record(self.root, {
            "file": "src/Test.cs",
            "line": 2,
            "symbol": "return",
            "evidence": "  return x;"
        })
        self.assertEqual(v.status, "PASS")

    def test_form_feed_in_content(self):
        """A form feed (\\f) in content should not confuse line counting.
        splitlines() incorrectly splits on \\f, causing off-by-one.
        This test must pass with split("\\n") but fails with splitlines()."""
        content = (
            '  x = 1;\f\n'  # form feed before newline
            '  return x;\n'
        )
        (self.root / "src" / "Test.cs").write_text(content)
        v = verify.verify_record(self.root, {
            "file": "src/Test.cs",
            "line": 2,
            "symbol": "return",
            "evidence": "  return x;"
        })
        self.assertEqual(v.status, "PASS")

    def test_crlf_file(self):
        """CRLF line endings should be handled correctly.
        split("\\n") + strip() handles CRLF naturally."""
        content = "a = 1\r\nb = 2\r\n"
        (self.root / "src" / "Test.cs").write_text(content)
        v = verify.verify_record(self.root, {
            "file": "src/Test.cs",
            "line": 2,
            "symbol": "b",
            "evidence": "b = 2"
        })
        self.assertEqual(v.status, "PASS")

    def test_file_no_trailing_newline(self):
        """A file without a trailing newline should still cite lines correctly."""
        content = "a = 1\nb = 2"  # no trailing newline
        (self.root / "src" / "Test.cs").write_text(content)
        v = verify.verify_record(self.root, {
            "file": "src/Test.cs",
            "line": 2,
            "symbol": "b",
            "evidence": "b = 2"
        })
        self.assertEqual(v.status, "PASS")

    def test_cite_line_one_past_last_real_line_with_trailing_newline(self):
        """Citing a line number one past the last real line should NOT pass.
        A file "a\\nb\\n" has 2 real lines; line 3 should fail.
        After split("\\n"), this produces ['a', 'b', ''], so len=3.
        Line 3 (index 2) is the empty string, not a real line."""
        content = "a = 1\nb = 2\n"  # trailing newline
        (self.root / "src" / "Test.cs").write_text(content)
        v = verify.verify_record(self.root, {
            "file": "src/Test.cs",
            "line": 3,
            "symbol": "nope",
            "evidence": "nope"
        })
        # After split("\\n"), line 3 is an empty string.
        # evidence.strip() is "nope", "".strip() is "", they don't match.
        # So we expect either OFF_BY (if evidence found elsewhere) or FABRICATED.
        self.assertIn(v.status, ("OFF_BY", "FABRICATED"))


if __name__ == "__main__":
    unittest.main()
