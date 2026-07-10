"""Re-read every cited line. Never trust the grunt."""
from dataclasses import dataclass
from pathlib import Path

STATUSES = ("PASS", "OFF_BY", "FABRICATED", "SYMBOL_MISMATCH", "NO_FILE")


@dataclass
class Verdict:
    record: dict
    status: str
    detail: str


def verify_record(root: Path, rec: dict) -> Verdict:
    symbol, evidence = str(rec["symbol"]), str(rec["evidence"])

    if symbol not in evidence:
        return Verdict(rec, "SYMBOL_MISMATCH",
                       f"symbol {symbol!r} absent from quoted evidence")

    path = root / rec["file"]
    if not path.is_file():
        return Verdict(rec, "NO_FILE", f"no such file: {rec['file']}")

    text = path.read_text(errors="replace")
    lines = text.split("\n")
    cited = rec["line"]
    want = evidence.strip()

    if 1 <= cited <= len(lines) and lines[cited - 1].strip() == want:
        return Verdict(rec, "PASS", "")

    matches = [i + 1 for i, line in enumerate(lines) if line.strip() == want]
    if matches:
        actual = matches[0]
        return Verdict(rec, "OFF_BY",
                       f"cited {cited}, actual {actual} (off by {actual - cited:+d})")

    return Verdict(rec, "FABRICATED",
                   f"evidence appears nowhere in {rec['file']}")


def verify_records(root: Path, records: list[dict]) -> list[Verdict]:
    return [verify_record(root, rec) for rec in records]


def any_failed(verdicts: list[Verdict]) -> bool:
    return any(v.status != "PASS" for v in verdicts)


def render_table(task_id: str, verdicts: list[Verdict]) -> str:
    passed = sum(1 for v in verdicts if v.status == "PASS")
    failed = len(verdicts) - passed
    head = (f"result {task_id}: {len(verdicts)} records — "
            f"{passed} PASS, {failed} FAIL")
    rows = []
    for v in verdicts:
        loc = f"{v.record['file']}:{v.record['line']}"
        row = f"  {v.status:<16} {loc} {v.record['symbol']}"
        if v.detail:
            row += f" — {v.detail}"
        rows.append(row)
    return "\n".join([head, *rows])
