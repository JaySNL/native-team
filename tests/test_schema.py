import unittest

from team import schema


GOOD = {"file": "src/A.cs", "line": 36, "symbol": "TryHeal",
        "evidence": "    public bool TryHeal(Character c)"}


class RecordTest(unittest.TestCase):
    def test_good_record_passes(self):
        schema.validate_record(dict(GOOD))

    def test_missing_field_rejected(self):
        for k in ("file", "line", "symbol", "evidence"):
            rec = dict(GOOD)
            del rec[k]
            with self.assertRaises(schema.SchemaError):
                schema.validate_record(rec)

    def test_empty_evidence_rejected(self):
        rec = dict(GOOD, evidence="   ")
        with self.assertRaises(schema.SchemaError):
            schema.validate_record(rec)

    def test_symbol_absent_from_evidence_rejected(self):
        rec = dict(GOOD, symbol="Nonexistent")
        with self.assertRaises(schema.SchemaError):
            schema.validate_record(rec)

    def test_line_must_be_positive_int(self):
        for bad in (0, -3, "36", 1.5):
            with self.assertRaises(schema.SchemaError):
                schema.validate_record(dict(GOOD, line=bad))


class MessageTest(unittest.TestCase):
    def msg(self, **kw):
        base = {"id": "003", "from": "grunt1", "type": "blocked",
                "task": "001", "body": "why?"}
        base.update(kw)
        return base

    def test_good_message_passes(self):
        schema.validate_message(self.msg())

    def test_unknown_type_rejected(self):
        with self.assertRaises(schema.SchemaError):
            schema.validate_message(self.msg(type="idle_notification"))

    def test_oversized_body_rejected(self):
        with self.assertRaises(schema.SchemaError):
            schema.validate_message(self.msg(body="x" * (schema.MAX_BODY + 1)))


if __name__ == "__main__":
    unittest.main()
