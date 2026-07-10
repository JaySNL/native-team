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


if __name__ == "__main__":
    unittest.main()
