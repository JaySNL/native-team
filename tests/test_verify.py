import os, tempfile, unittest
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
        # This is deterministic: "nope" appears nowhere in the file.
        self.assertEqual(v.status, "FABRICATED")

    # ---- Finding 1: empty/whitespace evidence must not rubber-stamp a phantom line ----

    def test_empty_symbol_and_evidence_is_malformed(self):
        """record {"symbol":"","evidence":""} citing line 3 of a 2-line file
        (trailing '' from split("\\n")) must never PASS — must be MALFORMED."""
        content = "a = 1\nb = 2\n"
        (self.root / "src" / "Test.cs").write_text(content)
        v = verify.verify_record(self.root, {
            "file": "src/Test.cs",
            "line": 3,
            "symbol": "",
            "evidence": ""
        })
        self.assertEqual(v.status, "MALFORMED")

    def test_whitespace_only_evidence_is_malformed(self):
        content = "a = 1\nb = 2\n"
        (self.root / "src" / "Test.cs").write_text(content)
        v = verify.verify_record(self.root, {
            "file": "src/Test.cs",
            "line": 3,
            "symbol": "x",
            "evidence": "   "
        })
        self.assertEqual(v.status, "MALFORMED")

    # ---- Finding 2: path escape must not give a false PASS on arbitrary files ----

    def test_absolute_path_escape_is_out_of_tree(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_file = Path(outside.name) / "secret.txt"
        outside_file.write_text("top secret line\n")
        v = verify.verify_record(self.root, {
            "file": str(outside_file),
            "line": 1,
            "symbol": "top",
            "evidence": "top secret line"
        })
        self.assertEqual(v.status, "OUT_OF_TREE")

    def test_relative_traversal_escape_is_out_of_tree(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_file = Path(outside.name) / "secret.txt"
        outside_file.write_text("top secret line\n")
        rel = os.path.relpath(outside_file, self.root)
        self.assertTrue(rel.startswith(".."), "test fixture must actually traverse out")
        v = verify.verify_record(self.root, {
            "file": rel,
            "line": 1,
            "symbol": "top",
            "evidence": "top secret line"
        })
        self.assertEqual(v.status, "OUT_OF_TREE")

    # ---- Finding 3: non-UTF-8 files must be refused, not falsely accused ----

    def test_non_utf8_file_is_unreadable_not_fabricated(self):
        (self.root / "src" / "Cafe.cs").write_bytes(
            "// café notes\n".encode("latin-1"))
        v = verify.verify_record(self.root, {
            "file": "src/Cafe.cs",
            "line": 1,
            "symbol": "café",
            "evidence": "// café notes"
        })
        self.assertEqual(v.status, "UNREADABLE")

    # ---- Finding 4: a malformed record must not abort the batch ----

    def test_missing_symbol_field_is_malformed(self):
        rec = {"file": "src/TreatmentBed.cs", "line": 4,
                "evidence": "    public bool TryHeal(Character c, float amount) {"}
        v = verify.verify_record(self.root, rec)
        self.assertEqual(v.status, "MALFORMED")

    def test_line_as_string_is_malformed(self):
        v = verify.verify_record(self.root, self.rec(line="1"))
        self.assertEqual(v.status, "MALFORMED")

    def test_line_as_bool_true_is_malformed(self):
        v = verify.verify_record(self.root, self.rec(line=True))
        self.assertEqual(v.status, "MALFORMED")

    def test_line_zero_is_malformed(self):
        v = verify.verify_record(self.root, self.rec(line=0))
        self.assertEqual(v.status, "MALFORMED")

    def test_batch_with_malformed_record_returns_all_verdicts_in_order(self):
        good = self.rec()
        bad = {"file": "src/TreatmentBed.cs", "line": 4}  # missing symbol/evidence
        vs = verify.verify_records(self.root, [good, bad, good])
        self.assertEqual(len(vs), 3)
        self.assertEqual(vs[0].status, "PASS")
        self.assertEqual(vs[1].status, "MALFORMED")
        self.assertEqual(vs[2].status, "PASS")

    # ---- OFF_BY detail should report match count, not just the first line ----

    def test_off_by_detail_reports_match_count_when_evidence_recurs(self):
        content = "}\n}\n}\nother\n"
        (self.root / "src" / "Braces.cs").write_text(content)
        v = verify.verify_record(self.root, {
            "file": "src/Braces.cs",
            "line": 4,
            "symbol": "}",
            "evidence": "}"
        })
        self.assertEqual(v.status, "OFF_BY")
        self.assertIn("evidence matches 3 lines", v.detail)


if __name__ == "__main__":
    unittest.main()
