# Native Multi-Terminal Agent Team — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `team` CLI and tmux layer so a real `claude` lead can task a long-lived interactive `qwen` grunt over a file-based bus, and mechanically verify every citation the grunt returns.

**Architecture:** A `.team/` directory in the target repo is the entire bus — no daemon, no MCP. The lead runs `team send`, which composes a self-contained task file and `tmux send-keys` a pointer into the grunt's pane. The grunt reports through `team result add` / `team result done`. Sealing a result announces it into the lead's inbox; the lead's backgrounded `team wait` exits and the harness wakes it. `team verify` re-reads every cited line before any claim reaches the lead's context.

**Tech Stack:** Python 3.14 (stdlib only), `unittest` (pytest is not installed), tmux 3.7b, qwen-code CLI v0.19.8.

**Spec:** `docs/superpowers/specs/2026-07-10-native-team-design.md`

## Global Constraints

- **Stdlib only.** No third-party runtime dependencies. Tests use `unittest`, not pytest.
- **Python 3.14.6**, invoked as `python3`.
- **Exit codes are contract**, used by the lead's Bash to branch:
  `0` ok · `1` verify FAIL under `--strict` · `2` pane gone · `3` refused (schema violation or invalid state) · `4` timeout.
- **Atomic writes everywhere.** Write a tempfile in the same directory, then `os.replace`. A poller must never observe half a JSON document.
- **Task ids are zero-padded 3-digit strings** (`"001"`), allocated by `O_EXCL` creation of `.team/ids/NNN`.
- **`evidence` comparison is `actual.strip() == evidence.strip()`.** Whitespace drift tolerated; content must match exactly.
- **Grunt reference backend is `qwen`.** `panes.py` never assumes a backend.
- **Never commit a bus.** `.team/` and `.qwen/` go into the target repo's `.gitignore`.
- **`team down` must restore `.qwen/settings.json`.** `team init` mutates the target repo's qwen config; a crashed session must not leave a repo hijacked.
- Tests run from the repo root: `python3 -m unittest discover -s tests -t . -v`

---

### Task 1: Scaffold, and close the two remaining unknowns

The spec's validation table records seven assumptions already measured. Two remain, and both are cheap. Do them before any code depends on them.

**Files:**
- Create: `team/__init__.py`
- Create: `tests/__init__.py`
- Create: `docs/validation-phase1.md`
- Create: `.gitignore` (modify: add `__pycache__/`)

**Interfaces:**
- Consumes: nothing.
- Produces: an importable `team` package; `docs/validation-phase1.md` recording two verdicts that later tasks cite.

- [ ] **Step 1: Create the package skeleton**

```bash
mkdir -p team tests docs
touch team/__init__.py tests/__init__.py
```

- [ ] **Step 2: Probe — does a background Bash re-invoke an idle lead?**

This underwrites the entire wake-on-artifact design. Run from a Claude Code session, as a background Bash:

```bash
sleep 20 && echo "WAKE_PROBE_FIRED"
```

Expected: the harness delivers a completion notification carrying `WAKE_PROBE_FIRED` without the user typing anything.
Record PASS/FAIL in `docs/validation-phase1.md`.

If FAIL: stop. `team wait` must instead be run in the foreground and the lead must poll, which changes Task 9's contract.

- [ ] **Step 3: Probe — does qwen support a command-scoped shell allowlist?**

If `run_shell_command(team)` is honored, the grunt's shell can be restricted to `team` only, and read-only becomes airtight rather than denylist-shaped.

```bash
D=$(mktemp -d); cd "$D" && git init -q . && mkdir -p .qwen
cat > .qwen/settings.json <<'EOF'
{ "context": { "fileName": ["NOPE.md"] },
  "tools": { "approvalMode": "yolo", "computerUse": { "enabled": false },
             "coreTools": ["run_shell_command(team)"] } }
EOF
tmux kill-session -t allowprobe 2>/dev/null
tmux new-session -d -s allowprobe -x 200 -y 50 -c "$D" 'qwen'
for i in $(seq 1 40); do tmux capture-pane -p -t allowprobe | grep -q '>' && break; sleep 1; done
sleep 2
tmux send-keys -t allowprobe -l 'Run the shell command: echo SHELL_RAN'; sleep 0.3
tmux send-keys -t allowprobe Enter
for i in $(seq 1 20); do sleep 5; tmux capture-pane -p -t allowprobe | grep -qaE '^[[:space:]]*◆' && break; done
tmux capture-pane -p -t allowprobe | grep -av '^[[:space:]]*$' | tail -12
tmux kill-session -t allowprobe 2>/dev/null
```

Expected if allowlist works: the `echo` is **refused or not run** (it is not `team`).
Expected if unsupported: `SHELL_RAN` appears, meaning `coreTools` was ignored.

Record the verdict. Either way Task 7 ships `excludeTools`; the allowlist is an *additional* lock only if honored.

- [ ] **Step 4: Write the findings doc**

```markdown
# Phase-1 validation results

| # | Question | Verdict | Evidence |
|---|---|---|---|
| A | Background Bash re-invokes an idle lead | <PASS/FAIL> | notification carried WAKE_PROBE_FIRED |
| B | qwen honours `coreTools: ["run_shell_command(team)"]` | <PASS/FAIL> | `echo SHELL_RAN` was <refused/executed> |

If B is FAIL, `run_shell_command` stays unrestricted. Read-only is enforced by
`excludeTools` alone, and a grunt could still mutate files via shell (e.g. `sed -i`).
This is an accepted, recorded risk — see the spec's "Still unverified" section.
```

- [ ] **Step 5: Commit**

```bash
git add team tests docs/validation-phase1.md .gitignore
git commit -m "chore: scaffold package; record phase-1 validation probes"
```

---

### Task 2: `bus.py` — paths, atomic writes, race-safe ids

**Files:**
- Create: `team/bus.py`
- Test: `tests/test_bus.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `repo_root(start: Path | None = None) -> Path` — walks up for `.git`; raises `BusError` if none.
  - `team_dir(root: Path) -> Path` — `root / ".team"`.
  - `atomic_write(path: Path, data: str) -> None`
  - `write_json(path: Path, obj: dict) -> None` / `read_json(path: Path) -> dict`
  - `alloc_id(root: Path) -> str` — `"001"`-style, `O_EXCL`.
  - `task_path(root, agent, tid) -> Path` · `result_path(root, tid) -> Path` · `staging_path(root, tid) -> Path` · `lead_inbox(root) -> Path` · `dead_path(root, tid) -> Path`
  - `open_task(root: Path, agent: str) -> str | None` — id of a task with no result and no dead marker.
  - `mark_dead(root, tid) -> None` / `is_dead(root, tid) -> bool`
  - `class BusError(Exception)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bus.py
import json, os, tempfile, unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from team import bus


class BusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".git").mkdir()
        (self.root / ".team" / "ids").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_repo_root_walks_up(self):
        deep = self.root / "a" / "b"
        deep.mkdir(parents=True)
        self.assertEqual(bus.repo_root(deep), self.root)

    def test_repo_root_raises_without_git(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(bus.BusError):
                bus.repo_root(Path(d))

    def test_atomic_write_leaves_no_partial_file(self):
        p = self.root / "sub" / "x.json"
        bus.write_json(p, {"a": 1})
        self.assertEqual(bus.read_json(p), {"a": 1})
        self.assertEqual([q.name for q in p.parent.iterdir()], ["x.json"])

    def test_alloc_id_is_zero_padded_and_sequential(self):
        self.assertEqual(bus.alloc_id(self.root), "001")
        self.assertEqual(bus.alloc_id(self.root), "002")

    def test_alloc_id_is_race_safe(self):
        with ThreadPoolExecutor(max_workers=8) as ex:
            ids = list(ex.map(lambda _: bus.alloc_id(self.root), range(40)))
        self.assertEqual(len(set(ids)), 40)

    def test_open_task_none_when_result_sealed(self):
        bus.write_json(bus.task_path(self.root, "grunt1", "001"),
                       {"id": "001", "kind": "task"})
        self.assertEqual(bus.open_task(self.root, "grunt1"), "001")
        bus.write_json(bus.result_path(self.root, "001"), {"task": "001"})
        self.assertIsNone(bus.open_task(self.root, "grunt1"))

    def test_open_task_ignores_replies_and_dead_tasks(self):
        bus.write_json(bus.task_path(self.root, "grunt1", "005"),
                       {"id": "005", "kind": "reply"})
        self.assertIsNone(bus.open_task(self.root, "grunt1"))
        bus.write_json(bus.task_path(self.root, "grunt1", "006"),
                       {"id": "006", "kind": "task"})
        bus.mark_dead(self.root, "006")
        self.assertTrue(bus.is_dead(self.root, "006"))
        self.assertIsNone(bus.open_task(self.root, "grunt1"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python3 -m unittest tests.test_bus -v`
Expected: `ModuleNotFoundError: No module named 'team.bus'`

- [ ] **Step 3: Implement `team/bus.py`**

```python
"""Filesystem bus primitives. Knows nothing about tmux or schemas."""
import json
import os
import tempfile
from pathlib import Path

TEAM = ".team"


class BusError(Exception):
    pass


def repo_root(start: Path | None = None) -> Path:
    cur = (start or Path.cwd()).resolve()
    for cand in [cur, *cur.parents]:
        if (cand / ".git").exists():
            return cand
    raise BusError(f"not inside a git repository: {cur}")


def team_dir(root: Path) -> Path:
    return root / TEAM


def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_json(path: Path, obj: dict) -> None:
    atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def alloc_id(root: Path) -> str:
    ids = team_dir(root) / "ids"
    ids.mkdir(parents=True, exist_ok=True)
    taken = [int(p.name) for p in ids.iterdir() if p.name.isdigit()]
    n = max(taken, default=0) + 1
    while True:
        try:
            fd = os.open(ids / f"{n:03d}", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return f"{n:03d}"
        except FileExistsError:
            n += 1


def task_path(root: Path, agent: str, tid: str) -> Path:
    return team_dir(root) / "inbox" / agent / f"{tid}.json"


def lead_inbox(root: Path) -> Path:
    return team_dir(root) / "inbox" / "lead"


def result_path(root: Path, tid: str) -> Path:
    return team_dir(root) / "results" / f"{tid}.json"


def staging_path(root: Path, tid: str) -> Path:
    return team_dir(root) / "staging" / f"{tid}.json"


def dead_path(root: Path, tid: str) -> Path:
    return team_dir(root) / "dead" / tid


def mark_dead(root: Path, tid: str) -> None:
    p = dead_path(root, tid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def is_dead(root: Path, tid: str) -> bool:
    return dead_path(root, tid).exists()


def open_task(root: Path, agent: str) -> str | None:
    box = team_dir(root) / "inbox" / agent
    if not box.is_dir():
        return None
    for p in sorted(box.glob("*.json")):
        obj = read_json(p)
        if obj.get("kind") != "task":
            continue
        tid = obj["id"]
        if result_path(root, tid).exists() or is_dead(root, tid):
            continue
        return tid
    return None
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_bus -v`
Expected: `Ran 7 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add team/bus.py tests/test_bus.py
git commit -m "feat(bus): atomic writes, O_EXCL id allocation, open-task inference"
```

---

### Task 3: `schema.py` — reject a malformed claim before it becomes a file

**Files:**
- Create: `team/schema.py`
- Test: `tests/test_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class SchemaError(Exception)`
  - `MAX_BODY: int = 1000`
  - `MESSAGE_TYPES: frozenset = frozenset({"result", "note", "blocked", "failed"})`
  - `validate_record(rec: dict) -> None`
  - `validate_message(msg: dict) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schema.py
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
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python3 -m unittest tests.test_schema -v`
Expected: `ModuleNotFoundError: No module named 'team.schema'`

- [ ] **Step 3: Implement `team/schema.py`**

```python
"""Shape validation. Pure: no filesystem, no tmux."""

MAX_BODY = 1000
MESSAGE_TYPES = frozenset({"result", "note", "blocked", "failed"})
RECORD_FIELDS = ("file", "line", "symbol", "evidence")
MESSAGE_FIELDS = ("id", "from", "type", "task", "body")


class SchemaError(Exception):
    pass


def validate_record(rec: dict) -> None:
    for key in RECORD_FIELDS:
        if key not in rec:
            raise SchemaError(f"record missing field: {key}")
    line = rec["line"]
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        raise SchemaError(f"line must be a positive int, got {line!r}")
    if not str(rec["evidence"]).strip():
        raise SchemaError("evidence is empty; quote the exact source line")
    if str(rec["symbol"]) not in str(rec["evidence"]):
        raise SchemaError(
            f"symbol {rec['symbol']!r} does not appear in evidence "
            f"{rec['evidence']!r} — a grep hit is not a verified citation"
        )


def validate_message(msg: dict) -> None:
    for key in MESSAGE_FIELDS:
        if key not in msg:
            raise SchemaError(f"message missing field: {key}")
    if msg["type"] not in MESSAGE_TYPES:
        raise SchemaError(
            f"unknown message type {msg['type']!r}; expected one of {sorted(MESSAGE_TYPES)}"
        )
    if len(str(msg["body"])) > MAX_BODY:
        raise SchemaError(
            f"body is {len(str(msg['body']))} chars; max {MAX_BODY}. "
            f"Write to a file and send a pointer."
        )
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_schema -v`
Expected: `Ran 8 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add team/schema.py tests/test_schema.py
git commit -m "feat(schema): reject empty evidence and symbol/evidence mismatch at write time"
```

---

### Task 4: `verify.py` — the engine that never trusts the grunt

The highest-value module. Pure: records in, verdicts out.

**Files:**
- Create: `team/verify.py`
- Test: `tests/test_verify.py`

**Interfaces:**
- Consumes: nothing (reads files by path only).
- Produces:
  - `@dataclass Verdict: record: dict; status: str; detail: str`
  - `STATUSES = ("PASS", "OFF_BY", "FABRICATED", "SYMBOL_MISMATCH", "NO_FILE")`
  - `verify_record(root: Path, rec: dict) -> Verdict`
  - `verify_records(root: Path, records: list[dict]) -> list[Verdict]`
  - `render_table(task_id: str, verdicts: list[Verdict]) -> str`
  - `any_failed(verdicts: list[Verdict]) -> bool`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_verify.py
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
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python3 -m unittest tests.test_verify -v`
Expected: `ModuleNotFoundError: No module named 'team.verify'`

- [ ] **Step 3: Implement `team/verify.py`**

```python
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

    lines = path.read_text(errors="replace").splitlines()
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
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_verify -v`
Expected: `Ran 8 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add team/verify.py tests/test_verify.py
git commit -m "feat(verify): classify citations as PASS/OFF_BY/FABRICATED/SYMBOL_MISMATCH"
```

---

### Task 5: `log.py` — turn a screen recording into a transcript

Measured: 341 KB of raw `pipe-pane` output per four qwen turns, 13.9 KB unique. Ink redraws the frame twice a second.

**Files:**
- Create: `team/log.py`
- Test: `tests/test_log.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `render(raw: str) -> str` — strips ANSI/OSC escapes, splits on `\r`, drops spinner frames and blank lines, removes duplicate lines globally, preserves first-occurrence order.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_log.py
import unittest

from team import log


class LogTest(unittest.TestCase):
    def test_strips_ansi_sgr_and_cursor_codes(self):
        self.assertEqual(log.render("\x1b[32mhello\x1b[0m\x1b[2K"), "hello")

    def test_strips_osc_title_sequences(self):
        self.assertEqual(log.render("\x1b]0;title\x07kept"), "kept")

    def test_drops_spinner_frames(self):
        raw = ("... I'll be back... with an answer. (7.5s · esc to cancel)\n"
               "◆ PINEAPPLE\n")
        self.assertEqual(log.render(raw), "◆ PINEAPPLE")

    def test_dedupes_repeated_redraw_lines(self):
        raw = "> prompt\n> prompt\n> prompt\nanswer\n"
        self.assertEqual(log.render(raw), "> prompt\nanswer")

    def test_carriage_returns_become_line_breaks(self):
        self.assertEqual(log.render("a\rb\r"), "a\nb")

    def test_blank_lines_dropped(self):
        self.assertEqual(log.render("a\n\n   \nb\n"), "a\nb")

    def test_preserves_first_occurrence_order(self):
        self.assertEqual(log.render("b\na\nb\n"), "b\na")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python3 -m unittest tests.test_log -v`
Expected: `ModuleNotFoundError: No module named 'team.log'`

- [ ] **Step 3: Implement `team/log.py`**

```python
"""Render a raw tmux pipe-pane tee into something readable.

Ink redraws the entire frame on every tick, so the raw tee is ~96% escape
codes and spinner frames. Measured: 341KB in, 13.9KB of unique content.
"""
import re

ANSI = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]"      # CSI (SGR, cursor, erase)
    r"|\x1b\][^\x07]*\x07"          # OSC (window title), BEL-terminated
    r"|\x1b[()][AB012]"             # charset selection
    r"|\x1b[=>]"                    # keypad mode
)
SPINNER = re.compile(r"\(\d+(?:\.\d+)?s\s*·\s*esc to cancel\)")


def render(raw: str) -> str:
    text = ANSI.sub("", raw).replace("\r", "\n")
    seen: set[str] = set()
    out: list[str] = []
    for line in text.split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        if SPINNER.search(line):
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return "\n".join(out)
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_log -v`
Expected: `Ran 7 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add team/log.py tests/test_log.py
git commit -m "feat(log): strip ANSI, drop spinner frames, dedupe Ink redraws"
```

---

### Task 6: `panes.py` — the only module that knows tmux

Two measured facts drive this module. `send-keys -l` followed by a separate `send-keys Enter` delivers exact literal text. And typing a leading `/` opens qwen's 70-entry command palette, where `Enter` selects the highlighted completion — so every send must dismiss palette state first, and `/clear` must verify its postcondition.

**Files:**
- Create: `team/panes.py`
- Test: `tests/test_panes.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class PaneError(Exception)`
  - `class Panes` with `__init__(self, runner=default_runner, sleep=time.sleep)`
  - `Panes.exists(target: str) -> bool`
  - `Panes.capture(target: str) -> str`
  - `Panes.send_line(target: str, text: str) -> None`
  - `Panes.clear_context(target: str, timeout: float = 10.0) -> None`
  - `Panes.pipe_pane(target: str, logfile: Path) -> None`
  - `default_runner(argv: list[str]) -> subprocess.CompletedProcess`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_panes.py
import subprocess, unittest
from pathlib import Path

from team import panes


class FakeRunner:
    """Records argv; replays queued (returncode, stdout) pairs."""

    def __init__(self, replies=None):
        self.calls: list[list[str]] = []
        self.replies = list(replies or [])

    def __call__(self, argv):
        self.calls.append(argv)
        rc, out = self.replies.pop(0) if self.replies else (0, "")
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")


class PanesTest(unittest.TestCase):
    def mk(self, replies=None):
        runner = FakeRunner(replies)
        return runner, panes.Panes(runner=runner, sleep=lambda _s: None)

    def test_send_line_sends_escape_then_literal_then_enter(self):
        runner, p = self.mk()
        p.send_line("team:0.1", "do task .team/inbox/grunt1/001.json")
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(runner.calls[0][-1], "Escape")
        self.assertIn("-l", runner.calls[1])
        self.assertEqual(runner.calls[1][-1], "do task .team/inbox/grunt1/001.json")
        self.assertEqual(runner.calls[2][-1], "Enter")

    def test_send_line_never_shell_interpolates(self):
        runner, p = self.mk()
        p.send_line("team:0.1", 'weird; rm -rf / "$(x)"')
        self.assertEqual(runner.calls[1][-1], 'weird; rm -rf / "$(x)"')

    def test_exists_false_when_has_session_fails(self):
        runner, p = self.mk(replies=[(1, "")])
        self.assertFalse(p.exists("team:0.1"))

    def test_exists_true_when_pane_listed(self):
        runner, p = self.mk(replies=[(0, ""), (0, "%3\n")])
        self.assertTrue(p.exists("team:0.1"))

    def test_capture_raises_on_tmux_failure(self):
        runner, p = self.mk(replies=[(1, "")])
        with self.assertRaises(panes.PaneError):
            p.capture("team:0.1")

    def test_clear_context_succeeds_once_palette_closes(self):
        # 3 send-keys calls, then capture shows palette, then capture is clean
        runner, p = self.mk(replies=[(0, ""), (0, ""), (0, ""),
                                     (0, "  (1/70)\n> clear"), (0, "> ready")])
        p.clear_context("team:0.1", timeout=5.0)
        self.assertEqual(runner.calls[1][-1], "/clear")

    def test_clear_context_raises_if_palette_never_closes(self):
        replies = [(0, ""), (0, ""), (0, "")] + [(0, "  (1/70)")] * 50
        runner, p = self.mk(replies=replies)
        with self.assertRaises(panes.PaneError):
            p.clear_context("team:0.1", timeout=0.0)

    def test_pipe_pane_targets_logfile(self):
        runner, p = self.mk()
        p.pipe_pane("team:0.1", Path("/tmp/x.log"))
        self.assertIn("pipe-pane", runner.calls[0])
        self.assertIn("/tmp/x.log", runner.calls[0][-1])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python3 -m unittest tests.test_panes -v`
Expected: `ModuleNotFoundError: No module named 'team.panes'`

- [ ] **Step 3: Implement `team/panes.py`**

```python
"""The only module that knows tmux exists.

Measured against qwen v0.19.8 + tmux 3.7b:
  * `send-keys -l TEXT` then a separate `send-keys Enter` delivers exact
    literal text into an Ink TUI. No escaping, no bracketed paste.
  * A leading `/` opens qwen's command palette. `Enter` then selects the
    *highlighted completion*, not the typed line. So dismiss palette state
    with Escape before every send, and verify `/clear` actually landed.
"""
import re
import subprocess
import time
from pathlib import Path

PALETTE = re.compile(r"\(\d+/\d+\)")


class PaneError(Exception):
    pass


def default_runner(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True)


class Panes:
    def __init__(self, runner=default_runner, sleep=time.sleep):
        self.runner = runner
        self.sleep = sleep

    def _tmux(self, *args: str) -> subprocess.CompletedProcess:
        proc = self.runner(["tmux", *args])
        if proc.returncode != 0:
            raise PaneError(f"tmux {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc

    def exists(self, target: str) -> bool:
        session = target.split(":", 1)[0]
        if self.runner(["tmux", "has-session", "-t", session]).returncode != 0:
            return False
        proc = self.runner(["tmux", "list-panes", "-t", target, "-F", "#{pane_id}"])
        return proc.returncode == 0 and bool(proc.stdout.strip())

    def capture(self, target: str) -> str:
        return self._tmux("capture-pane", "-p", "-t", target).stdout

    def send_line(self, target: str, text: str) -> None:
        self._tmux("send-keys", "-t", target, "Escape")
        self.sleep(0.15)
        self._tmux("send-keys", "-t", target, "-l", text)
        self.sleep(0.15)
        self._tmux("send-keys", "-t", target, "Enter")

    def clear_context(self, target: str, timeout: float = 10.0) -> None:
        self.send_line(target, "/clear")
        deadline = time.monotonic() + timeout
        while True:
            if not PALETTE.search(self.capture(target)):
                return
            if time.monotonic() >= deadline:
                raise PaneError(
                    f"{target}: command palette still open after /clear; "
                    f"pane may have selected the wrong command"
                )
            self.sleep(0.25)

    def pipe_pane(self, target: str, logfile: Path) -> None:
        self._tmux("pipe-pane", "-o", "-t", target, f"cat >> {logfile}")
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_panes -v`
Expected: `Ran 8 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add team/panes.py tests/test_panes.py
git commit -m "feat(panes): tmux adapter with palette-safe send and /clear postcondition"
```

---

### Task 7: `team init` / `team down` — mutate the target repo, and be able to undo it

`team init` writes `.qwen/settings.json` into the target repo. That suppresses context-file autoloading (measured: qwen loads every `AGENTS.md`/`CLAUDE.md` up to the git root, and `/clear` does not drop them), removes mutation tools, and prevents the approval wedge. Because it mutates the repo, `team down` must restore it.

**Files:**
- Create: `team/config.py`
- Create: `team/__main__.py`
- Create: `bin/team`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `bus.repo_root`, `bus.team_dir`, `bus.write_json`, `bus.read_json`, `bus.BusError`
- Produces:
  - `GRUNT_SETTINGS: dict` — the qwen settings payload.
  - `init(root: Path, force: bool = False) -> list[str]` — creates the bus, installs settings, updates `.gitignore`; returns warning lines. Raises `StateError` on a stale bus.
  - `down(root: Path) -> list[str]` — restores settings, removes the bus; returns action lines.
  - `class StateError(Exception)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
import json, tempfile, unittest
from pathlib import Path

from team import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".git").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def qwen(self):
        return self.root / ".qwen" / "settings.json"

    def test_init_creates_bus_dirs(self):
        config.init(self.root)
        for sub in ("inbox/lead", "results", "staging", "logs", "ids", "dead"):
            self.assertTrue((self.root / ".team" / sub).is_dir(), sub)

    def test_init_writes_grunt_settings(self):
        config.init(self.root)
        got = json.loads(self.qwen().read_text())
        self.assertEqual(got["tools"]["approvalMode"], "yolo")
        self.assertIn("write_file", got["tools"]["excludeTools"])
        self.assertEqual(got["context"]["fileName"], ["TEAM_GRUNT_CONTEXT.md"])

    def test_init_backs_up_existing_settings(self):
        self.qwen().parent.mkdir(parents=True)
        self.qwen().write_text('{"mine": true}')
        config.init(self.root)
        backup = self.root / ".qwen" / "settings.json.team-backup"
        self.assertEqual(json.loads(backup.read_text()), {"mine": True})

    def test_init_appends_gitignore_entries_once(self):
        config.init(self.root)
        config.down(self.root)
        config.init(self.root)
        text = (self.root / ".gitignore").read_text()
        self.assertEqual(text.count(".team/"), 1)
        self.assertEqual(text.count(".qwen/"), 1)

    def test_init_refuses_stale_bus_without_force(self):
        config.init(self.root)
        with self.assertRaises(config.StateError):
            config.init(self.root)
        config.init(self.root, force=True)  # must not raise

    def test_down_restores_backup(self):
        self.qwen().parent.mkdir(parents=True)
        self.qwen().write_text('{"mine": true}')
        config.init(self.root)
        config.down(self.root)
        self.assertEqual(json.loads(self.qwen().read_text()), {"mine": True})
        self.assertFalse((self.root / ".team").exists())

    def test_down_removes_settings_it_created(self):
        config.init(self.root)
        config.down(self.root)
        self.assertFalse(self.qwen().exists())

    def test_init_returns_hijack_warning(self):
        warnings = config.init(self.root)
        self.assertTrue(any("YOLO" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python3 -m unittest tests.test_config -v`
Expected: `ModuleNotFoundError: No module named 'team.config'`

- [ ] **Step 3: Implement `team/config.py`**

```python
"""Bus lifecycle, and the target repo's qwen configuration.

`team init` mutates the target repo. Everything it touches is recorded in
.team/init.json so `team down` can put it back.
"""
import json
import shutil
from pathlib import Path

from team import bus

BUS_SUBDIRS = ("inbox/lead", "results", "staging", "logs", "ids", "dead")
GITIGNORE_ENTRIES = (".team/", ".qwen/")

GRUNT_SETTINGS = {
    "context": {"fileName": ["TEAM_GRUNT_CONTEXT.md"]},
    "tools": {
        "approvalMode": "yolo",
        "computerUse": {"enabled": False},
        "excludeTools": ["write_file", "replace", "edit", "save_memory", "web_fetch"],
    },
}


class StateError(Exception):
    pass


def _qwen_settings(root: Path) -> Path:
    return root / ".qwen" / "settings.json"


def _backup(root: Path) -> Path:
    return root / ".qwen" / "settings.json.team-backup"


def _update_gitignore(root: Path) -> None:
    path = root / ".gitignore"
    existing = path.read_text().splitlines() if path.exists() else []
    missing = [e for e in GITIGNORE_ENTRIES if e not in existing]
    if not missing:
        return
    lines = existing + missing
    bus.atomic_write(path, "\n".join(lines) + "\n")


def init(root: Path, force: bool = False) -> list[str]:
    team = bus.team_dir(root)
    if team.exists() and not force:
        raise StateError(
            f"{team} already exists. A stale bus makes `team wait` return "
            f"instantly on yesterday's results. Run `team down`, or pass --force."
        )
    # Read provenance BEFORE the rmtree destroys it.
    prior = {}
    if team.exists():
        if (team / "init.json").exists():
            prior = bus.read_json(team / "init.json")
        shutil.rmtree(team)

    for sub in BUS_SUBDIRS:
        (team / sub).mkdir(parents=True, exist_ok=True)
    bus.write_json(team / "roster.json", {})

    settings, backup = _qwen_settings(root), _backup(root)
    settings.parent.mkdir(parents=True, exist_ok=True)

    # Provenance must survive a re-init. On a second `--force` run, settings.json
    # is OUR generated file, so re-deriving `created` from its existence would
    # flip the flag (leaving our YOLO settings behind on `down`) and copy our
    # file over the user's backup, destroying their original config. Carry the
    # recorded answer forward, and never overwrite a backup that already exists.
    if "created_qwen_settings" in prior:
        created = prior["created_qwen_settings"]
    else:
        created = not settings.exists()
    if not created and not backup.exists():
        shutil.copy2(settings, backup)
    bus.write_json(settings, GRUNT_SETTINGS)

    bus.write_json(team / "init.json", {"created_qwen_settings": created})
    _update_gitignore(root)

    return [
        f"bus ready at {team}",
        f"wrote {settings} (grunt: no context files, no write tools, approvalMode=YOLO)",
        "WARNING: while this session is live, your own `qwen` in this repo loses "
        "CLAUDE.md context and runs in YOLO mode. `team down` restores it.",
    ]


def down(root: Path) -> list[str]:
    team = bus.team_dir(root)
    actions = []

    meta = {}
    if (team / "init.json").exists():
        meta = bus.read_json(team / "init.json")

    settings, backup = _qwen_settings(root), _backup(root)
    if backup.exists():
        shutil.move(str(backup), str(settings))
        actions.append(f"restored {settings} from backup")
    elif meta.get("created_qwen_settings") and settings.exists():
        settings.unlink()
        actions.append(f"removed {settings}")

    if team.exists():
        shutil.rmtree(team)
        actions.append(f"removed {team}")
    return actions
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_config -v`
Expected: `Ran 8 tests ... OK`

- [ ] **Step 5: Add the entrypoint shim**

```python
# team/__main__.py
import sys

from team.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

```bash
# bin/team
#!/usr/bin/env bash
# `team` — file-based bus for the native multi-terminal agent team.
exec python3 -m team "$@"
```

Note: `bin/team` fails until Task 9 creates `team/cli.py`. That is expected; it is committed now so the shim exists for later tasks.

```bash
chmod +x bin/team
```

- [ ] **Step 6: Commit**

```bash
git add team/config.py team/__main__.py bin/team tests/test_config.py
git commit -m "feat(config): init installs grunt qwen settings; down restores them"
```

---

### Task 8: Task composition, messaging, and result sealing

**Files:**
- Create: `team/protocol.py`
- Create: `team/ops.py`
- Test: `tests/test_ops.py`

**Interfaces:**
- Consumes: `bus.*`, `schema.validate_record`, `schema.validate_message`, `panes.Panes`, `config.StateError`
- Produces:
  - `protocol.task_body(tid: str, question: str, scope: list[str]) -> str` — the contract text embedded in every task file.
  - `ops.compose_task(root, agent, question, scope, supersede=False) -> str` — allocates the id, writes `inbox/<agent>/NNN.json`, returns the task id. Raises `StateError` if the agent has an open task and `supersede` is False.
  - `ops.reply(root, agent, msg_id, text) -> str` — writes a `kind: "reply"` file. Raises `StateError` unless that agent's last message was `blocked`.
  - `ops.post_message(root, sender, mtype, task, body) -> str` — writes `inbox/lead/NNN.json`, returns msg id.
  - `ops.result_add(root, tid, rec) -> None` — validates, appends to `staging/NNN.json`.
  - `ops.result_done(root, tid, agent) -> str` — validates all, seals `staging → results`, then announces. Returns msg id. Raises `StateError` on a dead or already-sealed task.
  - `ops.last_message_from(root, agent) -> dict | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ops.py
import tempfile, unittest
from pathlib import Path

from team import bus, config, ops, schema

REC = {"file": "a.py", "line": 1, "symbol": "x", "evidence": "x = 1"}


class OpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".git").mkdir()
        config.init(self.root)
        (bus.team_dir(self.root) / "inbox" / "grunt1").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_compose_task_embeds_protocol_and_scope(self):
        tid = ops.compose_task(self.root, "grunt1", "where is TryHeal?", ["src/A.cs"])
        task = bus.read_json(bus.task_path(self.root, "grunt1", tid))
        self.assertEqual(task["kind"], "task")
        self.assertEqual(task["scope"], ["src/A.cs"])
        self.assertIn("team result add", task["protocol"])
        self.assertIn("team msg --blocked", task["protocol"])
        self.assertIn(tid, task["protocol"])

    def test_compose_task_refuses_second_open_task(self):
        ops.compose_task(self.root, "grunt1", "q1", [])
        with self.assertRaises(config.StateError):
            ops.compose_task(self.root, "grunt1", "q2", [])

    def test_supersede_kills_old_task_and_allows_new(self):
        old = ops.compose_task(self.root, "grunt1", "q1", [])
        new = ops.compose_task(self.root, "grunt1", "q2", [], supersede=True)
        self.assertTrue(bus.is_dead(self.root, old))
        self.assertNotEqual(old, new)

    def test_result_done_rejected_for_superseded_task(self):
        old = ops.compose_task(self.root, "grunt1", "q1", [])
        ops.compose_task(self.root, "grunt1", "q2", [], supersede=True)
        ops.result_add(self.root, old, dict(REC))
        with self.assertRaises(config.StateError):
            ops.result_done(self.root, old, "grunt1")

    def test_result_add_validates(self):
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        with self.assertRaises(schema.SchemaError):
            ops.result_add(self.root, tid, dict(REC, evidence="  "))

    def test_result_done_seals_then_announces(self):
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        ops.result_add(self.root, tid, dict(REC))
        mid = ops.result_done(self.root, tid, "grunt1")
        self.assertTrue(bus.result_path(self.root, tid).exists())
        self.assertFalse(bus.staging_path(self.root, tid).exists())
        msg = bus.read_json(bus.lead_inbox(self.root) / f"{mid}.json")
        self.assertEqual(msg["type"], "result")
        self.assertEqual(msg["task"], tid)

    def test_result_done_is_write_once(self):
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        ops.result_add(self.root, tid, dict(REC))
        ops.result_done(self.root, tid, "grunt1")
        ops.result_add(self.root, tid, dict(REC))
        with self.assertRaises(config.StateError):
            ops.result_done(self.root, tid, "grunt1")

    def test_reply_requires_prior_blocked_message(self):
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        with self.assertRaises(config.StateError):
            ops.reply(self.root, "grunt1", "001", "an answer")
        mid = ops.post_message(self.root, "grunt1", "blocked", tid, "why?")
        rid = ops.reply(self.root, "grunt1", mid, "because")
        obj = bus.read_json(bus.task_path(self.root, "grunt1", rid))
        self.assertEqual(obj["kind"], "reply")

    def test_post_message_validates_body_size(self):
        with self.assertRaises(schema.SchemaError):
            ops.post_message(self.root, "grunt1", "note", "001", "x" * 2000)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python3 -m unittest tests.test_ops -v`
Expected: `ModuleNotFoundError: No module named 'team.protocol'`

- [ ] **Step 3: Implement `team/protocol.py`**

```python
"""The contract that ships inside every task file.

`/clear` fires before each new task, so the grunt has no memory of the
protocol. Therefore the protocol travels with the task. The lead never
authors this text — `team send` embeds it — so the lead cannot forget it.
"""

TEMPLATE = """\
You are a grunt on a code-lookup team. Answer ONLY from files you actually open.

TASK {tid}
QUESTION:
{question}

SCOPE (open these; do not wander):
{scope}

HOW TO REPORT — you have no other channel. Prose is not a report.

For every finding, run exactly one command per citation:

    team result add --task {tid} --file <path> --line <n> \\
        --symbol <name> --evidence '<the exact source line, copied verbatim>'

`--evidence` must be the FULL source line as it appears in the file. It is
re-read and compared. A guessed line number will be detected and rejected.
`--symbol` must appear inside `--evidence`.

When every citation is added:

    team result done --task {tid}

If you cannot proceed, do not guess and do not write a scratch file:

    team msg --blocked --task {tid} "your question here"

Do not edit any file. You have no write tools.
"""


def task_body(tid: str, question: str, scope: list[str]) -> str:
    scope_text = "\n".join(f"  - {s}" for s in scope) or "  (none given)"
    return TEMPLATE.format(tid=tid, question=question.strip(), scope=scope_text)
```

- [ ] **Step 4: Implement `team/ops.py`**

```python
"""Bus operations: compose tasks, exchange messages, seal results."""
import json
from pathlib import Path

from team import bus, protocol, schema
from team.config import StateError


def _messages(root: Path) -> list[dict]:
    box = bus.lead_inbox(root)
    return [bus.read_json(p) for p in sorted(box.glob("*.json"))]


def last_message_from(root: Path, agent: str) -> dict | None:
    mine = [m for m in _messages(root) if m["from"] == agent]
    return mine[-1] if mine else None


def compose_task(root: Path, agent: str, question: str,
                 scope: list[str], supersede: bool = False) -> str:
    open_tid = bus.open_task(root, agent)
    if open_tid and not supersede:
        raise StateError(
            f"{agent} already has open task {open_tid}. "
            f"Pass --supersede to kill it, or wait for its result."
        )
    if open_tid:
        bus.mark_dead(root, open_tid)

    tid = bus.alloc_id(root)
    bus.write_json(bus.task_path(root, agent, tid), {
        "id": tid,
        "kind": "task",
        "to": agent,
        "from": "lead",
        "question": question,
        "scope": scope,
        "protocol": protocol.task_body(tid, question, scope),
    })
    return tid


def reply(root: Path, agent: str, msg_id: str, text: str) -> str:
    last = last_message_from(root, agent)
    if last is None or last["type"] != "blocked":
        raise StateError(
            f"{agent}'s last message is "
            f"{'nothing' if last is None else last['type']!r}, not 'blocked'. "
            f"Only a blocked agent is idle at its prompt and safe to send to."
        )
    rid = bus.alloc_id(root)
    bus.write_json(bus.task_path(root, agent, rid), {
        "id": rid,
        "kind": "reply",
        "to": agent,
        "from": "lead",
        "in_reply_to": msg_id,
        "task": last["task"],
        "body": text,
    })
    return rid


def post_message(root: Path, sender: str, mtype: str, task: str, body: str) -> str:
    mid = bus.alloc_id(root)
    msg = {"id": mid, "from": sender, "type": mtype, "task": task, "body": body}
    schema.validate_message(msg)
    bus.write_json(bus.lead_inbox(root) / f"{mid}.json", msg)
    return mid


def result_add(root: Path, tid: str, rec: dict) -> None:
    schema.validate_record(rec)
    path = bus.staging_path(root, tid)
    records = bus.read_json(path)["records"] if path.exists() else []
    records.append(rec)
    bus.write_json(path, {"task": tid, "records": records})


def result_done(root: Path, tid: str, agent: str) -> str:
    if bus.is_dead(root, tid):
        raise StateError(f"task {tid} was superseded; its result is rejected")
    if bus.result_path(root, tid).exists():
        raise StateError(f"task {tid} is already sealed; results are write-once")

    staging = bus.staging_path(root, tid)
    if not staging.exists():
        raise StateError(f"task {tid} has no staged records; nothing to seal")

    payload = bus.read_json(staging)
    for rec in payload["records"]:
        schema.validate_record(rec)
    payload["agent"] = agent

    # Seal before announce: the lead must never wake to a result that
    # does not yet exist on disk.
    bus.write_json(bus.result_path(root, tid), payload)
    staging.unlink()

    count = len(payload["records"])
    return post_message(root, agent, "result", tid,
                        f"{count} record(s) sealed; run `team verify {tid}`")
```

- [ ] **Step 5: Run the tests**

Run: `python3 -m unittest tests.test_ops -v`
Expected: `Ran 9 tests ... OK`

- [ ] **Step 6: Commit**

```bash
git add team/protocol.py team/ops.py tests/test_ops.py
git commit -m "feat(ops): self-contained task files, seal-then-announce, supersede-by-id"
```

---

### Task 9: `cli.py` — argparse, `wait`, and the exit-code contract

`team wait` is what the lead backgrounds. Its exit is the wake signal.

**Files:**
- Create: `team/cli.py`
- Create: `team/wait.py`
- Test: `tests/test_wait.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `bus.*`, `ops.*`, `config.*`, `verify.*`, `log.render`, `panes.Panes`
- Produces:
  - `wait.for_lead(root, timeout, poll=0.25, now=time.monotonic, sleep=time.sleep) -> list[dict]` — returns messages that appeared after the call began; empty list on timeout.
  - `wait.for_tasks(root, tids, timeout, poll=0.25, ...) -> tuple[list[str], list[str]]` — `(sealed, missing)`.
  - `cli.main(argv: list[str]) -> int` — exit codes `0/1/2/3/4`.

- [ ] **Step 1: Write the failing test for `wait`**

```python
# tests/test_wait.py
import tempfile, unittest
from pathlib import Path

from team import bus, config, ops, wait


class FakeClock:
    """Deterministic clock: every sleep advances time; a hook fires once."""

    def __init__(self, on_tick=None, fire_after=1):
        self.t = 0.0
        self.ticks = 0
        self.on_tick = on_tick
        self.fire_after = fire_after

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        self.ticks += 1
        if self.on_tick and self.ticks == self.fire_after:
            self.on_tick()


class WaitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".git").mkdir()
        config.init(self.root)
        (bus.team_dir(self.root) / "inbox" / "grunt1").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_for_lead_ignores_preexisting_messages(self):
        ops.post_message(self.root, "grunt1", "note", "001", "stale")
        clock = FakeClock()
        got = wait.for_lead(self.root, timeout=1.0, now=clock.now, sleep=clock.sleep)
        self.assertEqual(got, [])

    def test_for_lead_returns_only_new_messages(self):
        ops.post_message(self.root, "grunt1", "note", "001", "stale")
        clock = FakeClock(on_tick=lambda: ops.post_message(
            self.root, "grunt1", "blocked", "001", "fresh"))
        got = wait.for_lead(self.root, timeout=10.0, now=clock.now, sleep=clock.sleep)
        self.assertEqual([m["body"] for m in got], ["fresh"])

    def test_for_tasks_reports_sealed_and_missing(self):
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        ops.result_add(self.root, tid, {"file": "a.py", "line": 1,
                                        "symbol": "x", "evidence": "x = 1"})
        clock = FakeClock(on_tick=lambda: ops.result_done(self.root, tid, "grunt1"))
        sealed, missing = wait.for_tasks(self.root, [tid, "999"], timeout=2.0,
                                         now=clock.now, sleep=clock.sleep)
        self.assertEqual(sealed, [tid])
        self.assertEqual(missing, ["999"])

    def test_for_tasks_treats_dead_task_as_resolved(self):
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        bus.mark_dead(self.root, tid)
        clock = FakeClock()
        sealed, missing = wait.for_tasks(self.root, [tid], timeout=1.0,
                                         now=clock.now, sleep=clock.sleep)
        self.assertEqual((sealed, missing), ([], []))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python3 -m unittest tests.test_wait -v`
Expected: `ModuleNotFoundError: No module named 'team.wait'`

- [ ] **Step 3: Implement `team/wait.py`**

```python
"""Blocking waits. The lead backgrounds these; their exit is the wake signal.

Polling, not inotify: turns take tens of seconds, and a stdlib-only poll
loop has no dependency and no partial-read hazard (all writes are atomic).
"""
import time
from pathlib import Path

from team import bus

POLL = 0.25


def _lead_files(root: Path) -> set[str]:
    box = bus.lead_inbox(root)
    return {p.name for p in box.glob("*.json")} if box.is_dir() else set()


def for_lead(root: Path, timeout: float, poll: float = POLL,
             now=time.monotonic, sleep=time.sleep) -> list[dict]:
    before = _lead_files(root)
    deadline = now() + timeout
    while now() < deadline:
        sleep(poll)
        fresh = sorted(_lead_files(root) - before)
        if fresh:
            return [bus.read_json(bus.lead_inbox(root) / name) for name in fresh]
    return []


def _resolved(root: Path, tid: str) -> bool:
    return bus.result_path(root, tid).exists() or bus.is_dead(root, tid)


def for_tasks(root: Path, tids: list[str], timeout: float, poll: float = POLL,
              now=time.monotonic, sleep=time.sleep) -> tuple[list[str], list[str]]:
    deadline = now() + timeout
    while True:
        pending = [t for t in tids if not _resolved(root, t)]
        if not pending or now() >= deadline:
            break
        sleep(poll)
    sealed = [t for t in tids if bus.result_path(root, t).exists()]
    missing = [t for t in tids if not _resolved(root, t)]
    return sealed, missing
```

- [ ] **Step 4: Run the wait tests**

Run: `python3 -m unittest tests.test_wait -v`
Expected: `Ran 4 tests ... OK`

- [ ] **Step 5: Write the failing CLI test**

```python
# tests/test_cli.py
import io, tempfile, unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from team import bus, cli, config, ops

SRC = "x = 1\ny = 2\n"


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".git").mkdir()
        (self.root / "a.py").write_text(SRC)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = cli.main(["--root", str(self.root), *args])
        return code, out.getvalue()

    def test_init_then_stale_init_exits_3(self):
        self.assertEqual(self.run_cli("init")[0], 0)
        self.assertEqual(self.run_cli("init")[0], 3)
        self.assertEqual(self.run_cli("init", "--force")[0], 0)

    def test_verify_pass_and_strict_fail(self):
        self.run_cli("init")
        (bus.team_dir(self.root) / "inbox" / "grunt1").mkdir(parents=True)
        tid = ops.compose_task(self.root, "grunt1", "q", ["a.py"])
        ops.result_add(self.root, tid, {"file": "a.py", "line": 1,
                                        "symbol": "x", "evidence": "x = 1"})
        ops.result_done(self.root, tid, "grunt1")
        code, out = self.run_cli("verify", tid)
        self.assertEqual(code, 0)
        self.assertIn("1 PASS", out)

        bad = bus.read_json(bus.result_path(self.root, tid))
        bad["records"][0]["line"] = 2
        bus.write_json(bus.result_path(self.root, tid), bad)
        code, out = self.run_cli("verify", tid, "--strict")
        self.assertEqual(code, 1)
        self.assertIn("OFF_BY", out)

    def test_result_add_schema_violation_exits_3(self):
        self.run_cli("init")
        (bus.team_dir(self.root) / "inbox" / "grunt1").mkdir(parents=True)
        tid = ops.compose_task(self.root, "grunt1", "q", ["a.py"])
        code, out = self.run_cli("result", "add", "--task", tid, "--file", "a.py",
                                 "--line", "1", "--symbol", "zzz",
                                 "--evidence", "x = 1")
        self.assertEqual(code, 3)
        self.assertIn("does not appear in evidence", out)

    def test_wait_task_timeout_exits_4(self):
        self.run_cli("init")
        code, out = self.run_cli("wait", "--task", "001", "--timeout", "0")
        self.assertEqual(code, 4)
        self.assertIn("TIMEOUT: 001", out)

    def test_log_renders_stripped_transcript(self):
        self.run_cli("init")
        logfile = bus.team_dir(self.root) / "logs" / "grunt1.log"
        logfile.write_text("\x1b[32m◆ HI\x1b[0m\n... (1.0s · esc to cancel)\n")
        code, out = self.run_cli("log", "grunt1")
        self.assertEqual(code, 0)
        self.assertIn("◆ HI", out)
        self.assertNotIn("esc to cancel", out)

    def test_down_is_clean(self):
        self.run_cli("init")
        code, _ = self.run_cli("down")
        self.assertEqual(code, 0)
        self.assertFalse(bus.team_dir(self.root).exists())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 6: Run it to make sure it fails**

Run: `python3 -m unittest tests.test_cli -v`
Expected: `ModuleNotFoundError: No module named 'team.cli'`

- [ ] **Step 7: Implement `team/cli.py`**

```python
"""Argument parsing, wiring, and the exit-code contract.

  0 ok · 1 verify FAIL under --strict · 2 pane gone
  3 refused (schema violation or invalid state) · 4 timeout
"""
import argparse
import json
import sys
from pathlib import Path

from team import bus, config, log, ops, panes, verify, wait
from team.config import StateError
from team.schema import SchemaError

OK, VERIFY_FAIL, PANE_GONE, REFUSED, TIMEOUT = 0, 1, 2, 3, 4


def _roster(root: Path) -> dict:
    return bus.read_json(bus.team_dir(root) / "roster.json")


def _pane_for(root: Path, agent: str) -> str:
    entry = _roster(root).get(agent)
    if not entry:
        raise StateError(f"no agent {agent!r} in roster.json")
    return entry["pane"]


def _digest(msg: dict) -> str:
    body = msg["body"].replace("\n", " ")
    if len(body) > 80:
        body = body[:77] + "..."
    return f"{msg['type']:<8} {msg['id']} from {msg['from']} task {msg['task']}: {body}"


def cmd_init(args, root):
    for line in config.init(root, force=args.force):
        print(line)
    return OK


def cmd_down(args, root):
    for line in config.down(root):
        print(line)
    return OK


def cmd_send(args, root):
    p = panes.Panes()
    pane = _pane_for(root, args.agent)
    if not p.exists(pane):
        print(f"pane {pane} for {args.agent} is gone", file=sys.stderr)
        return PANE_GONE

    if args.reply:
        rid = ops.reply(root, args.agent, args.reply, args.text)
        p.send_line(pane, f"do task {bus.task_path(root, args.agent, rid).relative_to(root)}")
        print(f"replied {rid} to {args.agent}")
        return OK

    tid = ops.compose_task(root, args.agent, args.question, args.scope or [],
                           supersede=args.supersede)
    p.clear_context(pane)
    p.send_line(pane, f"do task {bus.task_path(root, args.agent, tid).relative_to(root)}")
    print(f"sent task {tid} to {args.agent}")
    return OK


def cmd_wait(args, root):
    if args.for_target == "lead":
        msgs = wait.for_lead(root, timeout=args.timeout)
        if not msgs:
            print(f"TIMEOUT: no message for lead within {args.timeout}s")
            return TIMEOUT
        for m in msgs:
            print(_digest(m))
        return OK

    sealed, missing = wait.for_tasks(root, args.task, timeout=args.timeout)
    for tid in sealed:
        print(f"SEALED: {tid}")
    for tid in missing:
        print(f"TIMEOUT: {tid}")
    return TIMEOUT if missing else OK


def cmd_inbox(args, root):
    for path in sorted(bus.lead_inbox(root).glob("*.json")):
        print(_digest(bus.read_json(path)))
    return OK


def cmd_show(args, root):
    print(bus.read_json(bus.lead_inbox(root) / f"{args.msg_id}.json")["body"])
    return OK


def cmd_log(args, root):
    path = bus.team_dir(root) / "logs" / f"{args.agent}.log"
    if not path.exists():
        print(f"no log for {args.agent}", file=sys.stderr)
        return REFUSED
    lines = log.render(path.read_text(errors="replace")).splitlines()
    print("\n".join(lines[-args.tail:] if args.tail else lines))
    return OK


def cmd_msg(args, root):
    mtype = "note" if args.note else "blocked" if args.blocked else "failed"
    mid = ops.post_message(root, args.agent, mtype, args.task, args.text)
    print(f"posted {mtype} {mid}")
    return OK


def cmd_result(args, root):
    if args.result_cmd == "add":
        ops.result_add(root, args.task, {
            "file": args.file, "line": args.line,
            "symbol": args.symbol, "evidence": args.evidence,
        })
        print(f"staged record for {args.task}")
        return OK
    mid = ops.result_done(root, args.task, args.agent)
    print(f"sealed {args.task}, announced as {mid}")
    return OK


def cmd_verify(args, root):
    payload = bus.read_json(bus.result_path(root, args.task))
    verdicts = verify.verify_records(root, payload["records"])
    print(verify.render_table(args.task, verdicts))
    if args.show:
        print(json.dumps(payload["records"], indent=2))
    failed = verify.any_failed(verdicts)
    return VERIFY_FAIL if (failed and args.strict) else OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="team")
    ap.add_argument("--root", default=None, help="repo root (default: discover via .git)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init"); p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    sub.add_parser("down").set_defaults(fn=cmd_down)

    p = sub.add_parser("send")
    p.add_argument("agent")
    p.add_argument("--new-task", dest="new_task", action="store_true")
    p.add_argument("--question", default="")
    p.add_argument("--scope", nargs="*")
    p.add_argument("--supersede", action="store_true")
    p.add_argument("--reply", metavar="MSG_ID")
    p.add_argument("text", nargs="?", default="")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("wait")
    p.add_argument("--for", dest="for_target", choices=["lead"], default=None)
    p.add_argument("--task", nargs="*", default=[])
    p.add_argument("--timeout", type=float, default=3600.0)
    p.set_defaults(fn=cmd_wait)

    sub.add_parser("inbox").set_defaults(fn=cmd_inbox)

    p = sub.add_parser("show"); p.add_argument("msg_id"); p.set_defaults(fn=cmd_show)

    p = sub.add_parser("log")
    p.add_argument("agent"); p.add_argument("--tail", type=int, default=0)
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("msg")
    p.add_argument("--agent", default="grunt1")
    p.add_argument("--note", action="store_true")
    p.add_argument("--blocked", action="store_true")
    p.add_argument("--failed", action="store_true")
    p.add_argument("--task", required=True)
    p.add_argument("text")
    p.set_defaults(fn=cmd_msg)

    p = sub.add_parser("result")
    rsub = p.add_subparsers(dest="result_cmd", required=True)
    a = rsub.add_parser("add")
    a.add_argument("--task", required=True)
    a.add_argument("--file", required=True)
    a.add_argument("--line", type=int, required=True)
    a.add_argument("--symbol", required=True)
    a.add_argument("--evidence", required=True)
    d = rsub.add_parser("done")
    d.add_argument("--task", required=True)
    d.add_argument("--agent", default="grunt1")
    p.set_defaults(fn=cmd_result)

    p = sub.add_parser("verify")
    p.add_argument("task")
    p.add_argument("--show", action="store_true")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(fn=cmd_verify)

    return ap


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = Path(args.root).resolve() if args.root else bus.repo_root()
        return args.fn(args, root)
    except SchemaError as exc:
        print(f"schema violation: {exc}", file=sys.stderr)
        return REFUSED
    except (StateError, bus.BusError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return REFUSED
    except panes.PaneError as exc:
        print(f"pane error: {exc}", file=sys.stderr)
        return PANE_GONE
```

`--for` takes a value (`choices=["lead"]`) rather than being a `store_const` flag. A `store_const` `--for` would leave the bare word `lead` as an unrecognized positional and `team wait --for lead` would exit 2 from argparse.

- [ ] **Step 8: Run the CLI tests**

Run: `python3 -m unittest tests.test_cli -v`
Expected: `Ran 6 tests ... OK`

- [ ] **Step 9: Verify both `wait` shapes parse**

```bash
python3 -m team --root . wait --for lead --timeout 0; echo "exit=$?"
```
Expected: `TIMEOUT: no message for lead within 0.0s` and `exit=4`
(run inside a repo where `team init` has been executed)

- [ ] **Step 10: Run the whole suite**

Run: `python3 -m unittest discover -s tests -t . -v`
Expected: `Ran 65 tests ... OK`

- [ ] **Step 11: Commit**

```bash
git add team/cli.py team/wait.py tests/test_cli.py tests/test_wait.py
git commit -m "feat(cli): wait/verify/log commands with the 0-4 exit-code contract"
```

---

### Task 10: `team-up` layout script and the end-to-end milestone

**Files:**
- Create: `bin/team-up`
- Modify: `README.md` (create)

**Interfaces:**
- Consumes: `team init` (Task 7), `roster.json` written here.
- Produces: a tmux session `team` with pane `team:0.0` running `claude` and `team:0.1..N` running `qwen`; `roster.json` mapping each agent to its pane and backend.

- [ ] **Step 1: Write `bin/team-up`**

```bash
#!/usr/bin/env bash
# team-up [n-grunts]  — build the tmux session and write roster.json.
#
# roster.json is written by the script that CREATES the panes, so it records
# what exists rather than guessing (papercut #9).
set -euo pipefail

GRUNTS="${1:-1}"
SESSION="team"
ROOT="$(git rev-parse --show-toplevel)"
TEAM="$ROOT/.team"

[ -d "$TEAM" ] || { echo "no $TEAM — run 'team init' first" >&2; exit 3; }
tmux has-session -t "$SESSION" 2>/dev/null && { echo "session $SESSION exists" >&2; exit 3; }

tmux new-session -d -s "$SESSION" -c "$ROOT" 'claude'
tmux pipe-pane -o -t "$SESSION:0.0" "cat >> $TEAM/logs/lead.log"

roster='{"lead":{"pane":"'"$SESSION"':0.0","backend":"claude"}}'
for i in $(seq 1 "$GRUNTS"); do
  tmux split-window -t "$SESSION:0" -c "$ROOT" 'qwen'
  tmux select-layout -t "$SESSION:0" tiled
  pane="$SESSION:0.$i"
  tmux pipe-pane -o -t "$pane" "cat >> $TEAM/logs/grunt$i.log"
  roster=$(printf '%s' "$roster" | python3 -c "
import json,sys
r=json.load(sys.stdin)
r['grunt$i']={'pane':'$pane','backend':'qwen'}
json.dump(r,sys.stdout)")
done

printf '%s\n' "$roster" | python3 -m json.tool > "$TEAM/roster.json"
echo "session $SESSION up with $GRUNTS grunt(s). Attach: tmux attach -t $SESSION"
```

```bash
chmod +x bin/team-up
```

- [ ] **Step 2: Install the CLI on PATH**

```bash
ln -sf "$PWD/bin/team" ~/.local/bin/team
ln -sf "$PWD/bin/team-up" ~/.local/bin/team-up
team --help
```

Expected: argparse usage listing `init down send wait inbox show log msg result verify`.

- [ ] **Step 3: Run the milestone by hand**

In a scratch git repo containing one small source file:

```bash
cd /tmp && rm -rf milestone && mkdir milestone && cd milestone && git init -q
printf 'class Bed:\n    def try_heal(self, c):\n        return True\n' > bed.py
git add -A && git -c user.name=t -c user.email=t@t commit -qm init
team init
team-up 1
tmux attach -t team   # detach with C-b d
```

From the lead pane (a real `claude`), confirm `/compact` is accepted — that is papercut #1, the reason for the whole exercise. Then, from any shell:

```bash
team send grunt1 --new-task --question "Where is try_heal defined?" --scope bed.py
team wait --task 001 --timeout 600
team verify 001
```

Expected:
```
result 001: 1 records — 1 PASS, 0 FAIL
  PASS             bed.py:2 try_heal
```

- [ ] **Step 4: Force a failure and confirm it is caught**

```bash
python3 - <<'EOF'
import json, pathlib
p = pathlib.Path(".team/results/001.json")
d = json.loads(p.read_text())
d["records"][0]["line"] = 3
p.write_text(json.dumps(d))
EOF
team verify 001 --strict; echo "exit=$?"
```

Expected: `OFF_BY ... cited 3, actual 2 (off by -1)` and `exit=1`.

- [ ] **Step 5: Confirm teardown restores the repo**

```bash
team down
test ! -e .team && test ! -e .qwen/settings.json && echo "clean"
tmux kill-session -t team
```

Expected: `clean`

- [ ] **Step 6: Write the README and commit**

```markdown
# native-team

A file-based bus for running a `claude` lead and interactive `qwen` grunts in adjacent tmux panes.

    team init          # create .team/, install grunt qwen settings
    team-up 1          # tmux session: lead + 1 grunt
    team send grunt1 --new-task --question "..." --scope src/A.cs
    team wait --task 001 --timeout 600     # background this from the lead
    team verify 001                        # re-reads every cited line
    team down          # restore .qwen/settings.json, remove the bus

Design: `docs/superpowers/specs/2026-07-10-native-team-design.md`
```

```bash
git add bin/team-up README.md
git commit -m "feat(layout): team-up builds the session and writes roster.json"
```

---

## Self-review

**Spec coverage.** Every phase-1 deliverable maps to a task: `bus` (2), `schema` (3), `verify` (4), `log` (5), `panes` (6), `roster.json` (10), `pipe-pane` (10), race-safe ids (2), `init` stale guard + `.gitignore` + `.qwen/settings.json` (7), `down` restore (7), and commands `init/down/send/wait/inbox/show/log/msg/result/verify` (7, 8, 9). The end-to-end milestone is Task 10.

**Known gaps, deliberate.**
- `team board` and multi-grunt fan-out are phase 2 per the spec. `wait --task` already accepts a list and id allocation is already race-safe, so neither creates debt.
- `cmd_send` requires `roster.json` to have an entry for the agent, which `team-up` (Task 10) writes. `config.init` writes an empty `roster.json`, so Tasks 8–9's tests construct the bus directly and never call `cmd_send`. Ordering holds.
- `bin/team` is committed in Task 7 but cannot run until Task 9 creates `team/cli.py`. Called out inline there.
- `run_shell_command` remains unrestricted unless Task 1's probe B passes. A grunt could mutate files via shell despite `excludeTools`. Recorded in the spec, not silently accepted.

**Type consistency.** `verify.Verdict.status` values are the same five strings in `verify.py`, `tests/test_verify.py`, and `cli.cmd_verify`. `ops.result_done(root, tid, agent)` matches its call in `cmd_result`. `panes.Panes.send_line`/`clear_context`/`exists` match their calls in `cmd_send`. `bus.task_path(root, agent, tid)` is called with the same three-arg shape everywhere.
