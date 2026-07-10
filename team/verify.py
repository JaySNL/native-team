"""Re-read every cited line. Never trust the grunt.

Records may be adversarial or simply broken (a grunt can bypass
`team result add` and write a staging file by hand). verify_record
must therefore never raise: every record, however malformed, yields
a Verdict.
"""
from dataclasses import dataclass
from pathlib import Path

STATUSES = ("PASS", "OFF_BY", "FABRICATED", "SYMBOL_MISMATCH",
            "NO_FILE", "OUT_OF_TREE", "UNREADABLE", "MALFORMED")

REQUIRED_FIELDS = ("file", "line", "symbol", "evidence")


@dataclass
class Verdict:
    record: dict
    status: str
    detail: str


def _malformed_reason(rec: dict) -> str | None:
    """Return a detail string naming the offending field if rec is
    unusable, else None."""
    for key in REQUIRED_FIELDS:
        if key not in rec:
            return f"record missing field: {key!r}"

    for field in ("file", "symbol", "evidence"):
        if not isinstance(rec[field], str):
            return f"{field} must be a string, got {type(rec[field]).__name__}"

    line = rec["line"]
    # bool is an int subclass — reject True/False explicitly.
    if isinstance(line, bool) or not isinstance(line, int):
        return f"line must be an int, got {type(line).__name__}"
    if line < 1:
        return f"line must be >= 1, got {line}"

    if not rec["symbol"].strip():
        return "symbol is empty after stripping"
    if not rec["evidence"].strip():
        return "evidence is empty after stripping"

    return None


def verify_record(root: Path, rec: dict) -> Verdict:
    """Verify one citation. Never raises: any unexpected filesystem
    or path error collapses to MALFORMED/NO_FILE instead of
    propagating out of a batch."""
    try:
        return _verify_record(root, rec)
    except (OSError, ValueError) as exc:
        return Verdict(rec, "MALFORMED",
                        f"record could not be processed: {exc}")


def _verify_record(root: Path, rec: dict) -> Verdict:
    reason = _malformed_reason(rec)
    if reason is not None:
        return Verdict(rec, "MALFORMED", reason)

    symbol, evidence = rec["symbol"], rec["evidence"]

    if symbol not in evidence:
        return Verdict(rec, "SYMBOL_MISMATCH",
                       f"symbol {symbol!r} absent from quoted evidence")

    file_field = rec["file"]
    if Path(file_field).is_absolute():
        return Verdict(rec, "OUT_OF_TREE",
                       f"absolute path escapes root: {file_field}")

    root_resolved = root.resolve()
    target_resolved = (root / file_field).resolve()
    if not target_resolved.is_relative_to(root_resolved):
        return Verdict(rec, "OUT_OF_TREE",
                       f"path escapes root: {file_field}")

    if not target_resolved.is_file():
        return Verdict(rec, "NO_FILE", f"no such file: {file_field}")

    raw = target_resolved.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Verdict(rec, "UNREADABLE",
                       f"{file_field} is not valid UTF-8; "
                       f"citation could not be verified")

    lines = text.split("\n")
    cited = rec["line"]
    want = evidence.strip()

    if 1 <= cited <= len(lines) and lines[cited - 1].strip() == want:
        return Verdict(rec, "PASS", "")

    matches = [i + 1 for i, line in enumerate(lines) if line.strip() == want]
    if matches:
        actual = matches[0]
        if len(matches) == 1:
            detail = (f"cited {cited}, actual {actual} "
                       f"(off by {actual - cited:+d})")
        else:
            detail = (f"cited {cited}, evidence matches {len(matches)} "
                       f"lines (first: {actual})")
        return Verdict(rec, "OFF_BY", detail)

    return Verdict(rec, "FABRICATED",
                   f"evidence appears nowhere in {file_field}")


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
