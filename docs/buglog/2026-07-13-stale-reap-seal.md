# BUG: lead-side reap-seal captures the *previous* task's `ANSWER.md`

- **Filed:** 2026-07-13
- **Severity:** High — silent data corruption. A task is reported `SEALED` and
  `team answer` returns a *wrong, stale* answer with no error. The grunt's real
  work is orphaned on disk.
- **Component:** `team/wait.py` (`_reap_answer` / lead-side fallback seal), with
  a contributing failure-open in `team/collect.py`.
- **Repro'd in:** `/home/jooshua/teamTest/.team` with grunt1 (local Qwen-30B via MLX).
- **Status:** **FIXED** 2026-07-13 (see Resolution). Report verified accurate on
  every claim against the code before fixing. Affected 007 answer was salvaged.

---

## Summary

When a grunt is slow, the lead's `team wait` fires its fallback **reap-seal**
against the grunt's scratch file `work/<grunt>/ANSWER.md`. That file is a **single
per-grunt scratch file reused across every task and never cleared between tasks**.
So a reap for task *N* can snapshot task *N-1*'s still-resident answer, seal it as
*N*'s result, and report `SEALED` — three minutes before the grunt has even finished
writing *N*'s real answer.

The reap has **no task-binding and no staleness guard**: it seals whatever bytes
happen to be in `ANSWER.md` at that instant, regardless of which task produced them.

## What was observed

Two `--type ask` tasks were dispatched back-to-back to the same grunt:

- **005** — a ~285-word pitch (grunt produced it fine).
- **007** — a much larger brief (900-word screenplay). Grunt took ~3 min.

`team wait --task 007` returned `SEALED`. `team answer 007` returned **005's
285-word text verbatim**, not the screenplay. The screenplay *was* generated — it
just never made it into the result.

## Evidence (byte sizes + mtimes — the race is visible in the timestamps)

```
.team/results/005.json        1764 bytes   2026-07-13 00:48:01
.team/results/007.json        1764 bytes   2026-07-13 00:48:49   <- sealed here
.team/work/grunt1/ANSWER.md   5439 bytes   2026-07-13 00:51:52   <- REAL 007 written here
```

- `005.json` and `007.json` **differ only at byte 16, line 2** (`cmp`) — i.e. the
  JSON header metadata (task id / timestamp). The **`answer` payload is identical**:
  `team answer 005` and `team answer 007` render the same 285-word text.
- `007.json` was sealed at **00:48:49**. The grunt's real 007 answer
  (`ANSWER.md`, 5439 bytes, ~3× larger) wasn't written until **00:51:52** — a
  **~3-minute gap**. The seal beat the write and captured the stale prior answer.
- The genuine 007 output is intact and orphaned in `work/grunt1/ANSWER.md`.

## Root cause

`team/wait.py` documents the fallback in its own comments:

- `wait.py:59-67` — 30B grunts frequently drop the tail `seal` step, so the lead
  seals the answer file directly ("So the lead seals the file directly … answer is
  never sealed mid-write. Everything here is best-effort").
- `wait.py:122-140` / `:127-128` — "The lead seals any ask answer whose grunt wrote
  the file but never ran a seal (see `_reap_answer`). Done before the pending check…"
  Then: `sealed = [t for t in tids if bus.result_path(root, t).exists()]`.

The reap decision is "does an answer file exist for this grunt?" — **not** "does an
answer file for *this task* exist, written *after this task was dispatched*?" The
comment's own claim, *"answer is never sealed mid-write,"* is false here: the file
is not mid-write, it is a **complete file from the previous task**, so the reap
treats it as a finished answer and seals it.

Three compounding factors:

1. **Shared scratch filename.** `work/<grunt>/ANSWER.md` is per-grunt, not per-task.
   Nothing binds its contents to a task id.
2. **Not cleared on dispatch.** The prior task's answer sits in `ANSWER.md` until the
   grunt overwrites it. Between dispatch and first write, it is stale-but-complete.
3. **No staleness guard in reap.** Reap doesn't check `ANSWER.md` mtime against the
   task's dispatch time, nor verify an embedded task id.

### Secondary: `team answer` / `collect.py` fails open

`collect.py:48-52` correctly refuses to read an *unsealed* task
("task {tid} has not sealed…"). But once the reap has (wrongly) sealed 007, the
result file exists, so `collect` happily returns the stale payload. The guard is
correct; it's just downstream of the real defect and so can't catch it.

## Reproduction

1. Dispatch `team send <grunt> --type ask` (task A, quick).
2. Immediately dispatch a second `--type ask` (task B) to the **same grunt** whose
   generation takes longer than the lead's reap patience.
3. `team wait --task B` → `SEALED` prematurely.
4. `team answer B` → returns **task A's** answer. `results/B.json` == A's payload;
   B's real answer is stranded in `work/<grunt>/ANSWER.md`.

## Suggested fixes (for the dev-context session)

Any one of these closes it; (a)+(b) together is the robust combination:

- **(a) Bind the answer file to the task.** Write to `work/<grunt>/ANSWER.<tid>.md`
  (or a per-task staging path) so a reap for B can never see A's file. Cleanest fix.
- **(b) Clear/truncate `ANSWER.md` at task dispatch**, and have `_reap_answer` seal
  only if the file is **non-empty AND mtime > task-dispatch time**.
- **(c) Embed the task id in the answer file** and have reap verify it matches the
  task being sealed before promoting.
- **(d) Distinguish reaped from grunt-sealed** in `team wait` output (e.g.
  `SEALED (reaped)`), so a premature/heuristic seal is at least visible to the lead
  rather than indistinguishable from a genuine grunt seal.

## Recovery for the affected instance

The real 007 answer is **not lost** — it is in
`/home/jooshua/teamTest/.team/work/grunt1/ANSWER.md` (5439 bytes). It can be
re-promoted into `results/007.json` (or simply re-read from that path) without
re-running the grunt. Note this same scratch file will be clobbered by the grunt's
*next* task, so salvage before dispatching more work to grunt1.

## Affected files

- `team/wait.py` — `_reap_answer`, lead-side seal decision (primary).
- `team/collect.py:48-52` — reads sealed result; fails open once falsely sealed.
- `team/bus.py` — `result_path()` / scratch path layout (`ANSWER.md` naming).
- `team/protocol.py` — seal protocol (if task-id binding is added here).

## Resolution (2026-07-13)

Fixed with report options **(b) + (d)**; verified with a regression test that fails
without the fix (reap sealed B with A's answer: `True is not false`) and passes with it.

- **(b) dispatch clears the answer file + a reap staleness fence.**
  `ops.compose_ask_task` now stamps `dispatched_at` on the task and **truncates
  BOTH** reap-candidate `ANSWER.md` paths (worktree + inbox fallback) on dispatch.
  An empty file trips the reap's existing `st_size == 0` skip, so a reap for task
  B can no longer see task A's leftover. `wait._reap_answer` additionally skips any
  candidate whose `mtime < dispatched_at - REAP_SKEW` (2s grace for fs mtime
  resolution / clock skew) — an independent backstop covering the inbox path.
  Chose the fixed filename + truncate over per-task `ANSWER.<tid>.md` (option a):
  30B grunts already fumble the plain path (hence the inbox fallback), and a
  per-task name would lower the reap hit-rate when the grunt writes the wrong file.
- **(d) reaped seals are visible.** A lead-side reap threads `reaped=True` through
  `ops.result_answer`/`result_done`, stamping `"sealed_by": "reap"` on the result;
  `team wait` prints `SEALED (reaped): <tid>` so a heuristic seal is distinguishable
  from a grunt's own.
- **Secondary (`collect.py` fail-open):** left as-is. It is correct downstream of
  the real defect (it cannot know a sealed result is wrong); with the primary race
  closed there is nothing false for it to read.

Salvage of the reported instance: the real 007 answer (911-word screenplay) was
re-promoted into `results/007.json` from `work/grunt1/ANSWER.md` before it could be
clobbered.

Tests: `tests/test_ask_and_blocked.py` (`test_reap_will_not_seal_a_prior_tasks_answer`,
`test_dispatch_clears_a_prior_answer_file`, `test_reaped_seal_is_marked_grunt_seal_is_not`).
518 pass.
