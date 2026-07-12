# local_job — async local delegate with a file-bus + exit-code wake

**Date:** 2026-07-11
**Status:** approved design, not yet built
**Code target:** `~/.claude/tools/` (NOT this repo — spec lives here because it is a
direct sibling of native-team's file-bus and reuses its semantics; the implementation
edits `ollama-mcp.mjs` and adds `job-wait`).
**Related:** `README.md` (native-team bus), `team/bus.py`, `team/wait.py`,
memory `mlx-serve-backend`, `~/.claude/LOCAL_LLM_ROUTING.md`.

## Problem

`~/.claude/tools/ollama-mcp.mjs` exposes `local_delegate` (and 6 sibling tools) as
**synchronous** MCP calls: `tools/call` → `await chat()` against mlx-serve → reply.
When the MLX generation is slow or the server wedges, the MCP call **blocks the lead
(Claude) until the MCP client's own timeout** — a dead wait with no liveness signal.

We want the AionUi "team method" property without its coupling: a local job that, when
it **finishes or hangs**, surfaces status to the lead promptly, so the lead never sits
out a full timeout. AionUi does this with a shared bus both sides connect to as clients
(the lead is woken, it does not poll). native-team already realises that natively — a
directory bus where `team wait`'s **exit code is the wake signal**, plus seal-then-announce
and a `blocked`/`failed` fast-wake. This design borrows those semantics for the generic
MCP-tool context (no git repo, no tmux, no qwen pane).

## Scope decisions (settled in brainstorming)

1. **Opt-in, not a rewrite.** Add a new `local_job` tool. The 7 existing sync tools
   (`local_delegate`, `local_summarize`, `local_draft`, `local_complete`,
   `local_code_review`, `local_commit_msg`, `local_status`) stay inline and unchanged.
   Quick delegates keep their fast inline return; only jobs explicitly launched async
   pay for the bus.
2. **Dedicated sink `~/.claude/jobs/`**, not native-team's `.team/`. native-team's
   `bus_root` requires a git repo + roster + panes; `local_job` runs from any project.
3. **Wake = exit code of a `job-wait` bin**, backgrounded by the lead — the harness
   re-invokes Claude when the backgrounded process exits. No reverse MCP, no daemon,
   no dependence on a `Monitor` tool call.
4. **Concurrency is model-aware** (measured 2026-07-11, see Appendix): coder-30B is
   pure-attn `qwen3_moe` and mlx-serve **batches concurrent same-model requests** — three
   fired together started and finished in the same millisecond. So: same model → allow up
   to `LOCAL_JOB_CAP=3` concurrent; **different model while any job is active → queue**
   (cross-model co-residence is the real wedge; the `llc-36` 35B also serializes
   internally). Concurrency buys latency-hiding across independent tasks, not throughput
   (aggregate +15%, per-stream ~2.6× slower at 3-way) — the cap reflects that plus KV
   pressure from N concurrent contexts.

## Architecture

Three units, each independently testable:

### A. The bus (`~/.claude/jobs/`)
```
~/.claude/jobs/
  <id>/
    status.json    # state machine + heartbeat + digest + pointers
    result.txt     # full worker output; written BEFORE status flips to done
```
No status registry file, no lock file the sweeper must hold — **state is the set of
`status.json` files on disk**, exactly as native-team infers state from inbox/result
file presence. An MCP-server restart re-adopts every in-flight job by rescanning the dir.

`status.json`:
```json
{ "id": "j_ab12cd", "state": "running",
  "model": "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit-dwq-v2",
  "task_head": "first ~120 chars of the task, for humans",
  "pid": 48213, "started": "2026-07-11T18:04:12Z",
  "heartbeat": "2026-07-11T18:04:18Z", "seq": 42,
  "result_path": "~/.claude/jobs/j_ab12cd/result.txt",
  "digest": null, "err": null }
```
- **States:** `queued → running → done | failed`.
- **`hung` is observer-inferred, never self-written** — a wedged worker cannot write.
  Rule: `state == "running" AND (now − heartbeat) > LOCAL_JOB_HANG_STALE (30s)` ⇒ hung.
- All writes are **atomic** (temp + `os.rename`/`fs.renameSync`), matching `bus.atomic_write`,
  so a reader never sees a torn file and needs no lock.
- **Seal-then-announce:** the worker writes `result.txt`, fsyncs, *then* flips status to
  `done` with the digest. A lead woken by `done` always finds the result on disk.

### B. The worker (`ollama-mcp.mjs --worker <id>`)
A detached child of the MCP server (`spawn(..., {detached:true, stdio:'ignore'})`),
re-entering the same file so it reuses `chat()`, `pickModel`, routing, and usage logging.
Lifecycle:
1. Write status `running` + own `pid`.
2. Start a **heartbeat timer** (every 2s) that bumps `heartbeat`+`seq`, *independent of
   token flow* — so cold model load / long prefill still count as alive, not hung.
3. `await chat(system, user, {model, temp})`.
4. Success → write `result.txt`, fsync, seal status `done` + `digest` (head ~400 chars).
   Error (MLX 5xx, throw) → seal `failed` + `err`.
5. Clear the heartbeat timer; exit.

### C. The gate + sweeper (inside the long-lived MCP server)
`local_job` (tool) resolves the model, then the **gate** decides admit-vs-queue by
counting live jobs from the bus:
- no active job → `running`;
- same model & active `< CAP` → `running`;
- same model & active `≥ CAP` → `queued`;
- **different model & any active → `queued`.**
Admit ⇒ spawn the detached worker. Either way return immediately.

A **sweeper** (`setInterval`, ~1s) run by the MCP server:
- reaps dead jobs: `state==running` & pid not alive & heartbeat stale ⇒ seal `failed`
  (`err: "worker died"`); this is the crash counterpart to observer-inferred `hung`;
- promotes `queued → running` (spawns their worker) whenever the gate rules allow,
  honouring the cap and the cross-model exclusion;
- is idempotent and stateless across restarts (reads the dir each tick).

### D. The wake bin (`~/.claude/tools/job-wait`)
Mirrors `team/wait.py`: poll the bus (0.25s), **exit code is the wake**, print a
compact digest + pointer, never the full body (routing-rule context discipline).
```
job-wait <id> [--timeout 600]
  exit 0  done     — prints digest + result_path
  exit 1  failed   — prints err
  exit 2  hung     — running but heartbeat stale > HANG_STALE
  exit 3  refused  — unknown id / unreadable status
  exit 4  timeout  — still running at deadline (not hung, just slow)
```
The lead runs this via Bash `run_in_background`; the harness re-invokes Claude on exit.
Companion read-only bins: `job-list` (one line per job: id/state/model/age) and
`job-status <id>` (print the status.json digest).

## Data flow (happy path)
```
lead: local_job(task) ──▶ MCP gate: admit ──▶ spawn worker (detached) ──▶ return {id,…} (~50ms)
lead: bg  job-wait <id>                         worker: running→heartbeat→chat()→result.txt→seal done
        └───────────────── harness wakes lead on job-wait exit 0 ◀── worker exits
lead: reads digest; opens result.txt only if it needs the full body
```
Hang path: worker/model wedges → heartbeat goes stale → `job-wait` hits exit 2 at ~30s,
lead is woken with "hung", no dead wait to an MCP timeout.

## Error handling
- **Worker crash** (killed, OOM): pid dies, heartbeat stops → sweeper seals `failed`.
- **MLX 5xx / exception:** worker catches, seals `failed` + `err`.
- **MCP-server restart mid-job:** detached workers keep running and keep writing; the
  new server's sweeper re-adopts by scanning the dir. Queued jobs (no worker yet) are
  re-promoted by the sweeper.
- **Cross-model wedge avoidance:** the gate never admits a second model concurrently, so
  the mlx-serve co-residence wedge cannot be triggered by `local_job`.
- **`job-wait` on an already-terminal job:** returns immediately with the sealed state.
- **Fail-open on bus I/O errors** in `job-wait` (exit 3, never hang), matching route-guard.

## Testing
Unit (stdlib, mirror native-team's per-module tests):
- state transitions queued→running→done/failed; atomic write leaves no torn file;
- seal ordering: `result.txt` exists whenever status reads `done`;
- hang inference: stale heartbeat ⇒ exit 2; fresh heartbeat under load ⇒ still running;
- dead-pid reap ⇒ failed;
- gate: same-model admits to CAP then queues; **cross-model queues while one is active**;
- sweeper promotes queued when a slot frees; idempotent across a simulated restart.

Integration (live mlx-serve, skip if unreachable):
- fire one `local_job`; `job-wait` blocks then exits 0 with a digest + path;
- fire 3 same-model jobs → all reach `running` (batched), all seal;
- fire a different-model job while one runs → it stays `queued`, then promotes on drain.

## Non-goals
- No reverse MCP (Qwen never calls the lead).
- No daemon; the only long-lived process is the MCP server that already exists.
- No native-team coupling (`.team/`, tmux, roster, worktrees).
- Not making the 7 sync tools async.
- Not a general job scheduler — single host, single mlx-serve, bounded by `CAP`.

## Appendix — the concurrency measurement (2026-07-11)
Live coder-30B (`state:ready`, 17.18 GB resident), `max_tokens` fixed, `temp 0`:

| run | wall | per-stream tok/s |
|---|---|---|
| single | 1.73 s | 115 |
| 3 concurrent (same model) | all started +1 ms, all ended +4.52 s (within 1 ms) | 44 each |

Clean serialization would stagger completions by ~1.73 s; instead all three ended in the
same millisecond ⇒ mlx-serve batches concurrent requests to one loaded `qwen3_moe` model.
Source (`server.zig:859`) forces `max_concurrent=1` for **hybrid/MoE/encoder** archs — this
targets hybrid `qwen3_5_moe` (the 35B `llc-36`), not pure-attn `qwen3_moe` (coder-30B),
consistent with both the measurement and memory `mlx-serve-backend`.
