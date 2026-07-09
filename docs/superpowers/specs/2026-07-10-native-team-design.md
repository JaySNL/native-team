# Native multi-terminal agent team — design

**Date:** 2026-07-10
**Status:** approved design, not yet implemented
**Source of requirements:** `HANDOFF.md` at repo root — nine papercuts observed live in an AionUi
team session on 2026-07-09.

---

## Purpose

Replicate AionUi's team-mode workflow natively in terminals: adjacent tmux panes running a real
`claude` CLI as the lead and long-lived interactive local-model CLIs as grunts, coordinated by a
file-based bus. The lead scopes work, grunts execute bounded lookups, the lead mechanically verifies
every citation before it reaches its own context.

The single strongest reason for the terminal approach: **the lead is a real `claude` process, so
`/compact`, `/clear` and every other slash command work natively.** That was papercut #1, and it had
no in-session remedy inside AionUi.

## Non-goals

- Rebuilding `llm-watch`, `llm-top`, `llc`, or `claude-local`.
- Making grunts smarter. They are 30B models doing bounded lookups. The protocol assumes they are
  wrong and makes that cheap to detect, rather than trying to prevent it.
- Multi-machine orchestration beyond what LAN already provides.
- Any `team_*` MCP surface. See "No MCP" below.

---

## Requirements traceability

Every papercut in `HANDOFF.md` maps to a mechanism in this design.

| # | Papercut | Mechanism |
|---|---|---|
| 1 | Slash commands don't exist | Lead is a real `claude` CLI in a real pane |
| 2 | Qwen can't resolve bare tool names | No MCP. A `team` CLI on `$PATH`, reachable via Bash by any backend |
| 3 | Teammates could not report back | File drop is the primary channel; a missing result is an observable `TIMEOUT`, not a silence |
| 4 | Wake semantics opaque | Delivery is a file that exists or doesn't. `team send` is synchronous and errors loudly |
| 5 | Task board never self-updated | There is no board file. Status is inferred from artifacts: inbox file ⇒ pending, result file ⇒ done |
| 6 | Idle notifications are noise | `team wait` blocks on artifacts, never on turn boundaries |
| 7 | Grunt output cannot be trusted | `evidence` field + `team verify` re-reads every cited line, lead-side |
| 8 | Lead context is the scarce resource | Bodies live in files; the lead reads digests and verdict tables, and chooses what to `team show` |
| 9 | Model labelling was wrong | `roster.json` is written by the script that creates the panes, not guessed at runtime |

### No MCP

Papercut #2 was a Qwen teammate unable to resolve `team_send_message`, resolving only the fully
prefixed `mcp__aionui-team__team_send_message` — and, on failure, concluding it had *no team tools at
all*. Both grunts did this independently.

A CLI on `$PATH` cannot fail that way. A misspelled `team` invocation produces a shell error the
model can read and correct. There is no naming surface to get wrong, and the bus stays a directory
you can `cat`.

---

## Architecture

### Two artifacts, deliberately separate

**The tool** lives in this repo (`~/Projects/native-team/`). One Python 3 package (stdlib only,
matching `llc.py` / `llm-watch.py` / `llm-top.py`), exposing a `team` entrypoint symlinked onto
`~/.local/bin`. Plus a tmux layout script.

**The bus** is `.team/`, created by `team init` inside whatever repo the team is pointed at. It is
per-target-repo, not global: a team session is scoped to one working tree, so `file:line` records
need no absolute paths and `team verify` opens files relative to the repo root.

```
<target-repo>/.team/
  roster.json              # {agent: {pane, backend, cwd}}
  inbox/<agent>/NNN.json   # a task or a reply; the whole contract, self-contained
  inbox/lead/NNN.json      # messages from grunts to the lead
  staging/NNN.json         # result records accumulating; not yet visible as done
  results/NNN.json         # sealed result; its existence means the task is done
  logs/<agent>.log         # tmux pipe-pane tee, for postmortems
  ids/NNN                  # empty marker files; O_EXCL creation claims an id
```

There is no `tasks.json`. The board is `ls`. A single mutable file written by a lead and N grunts is
a write race and a lie waiting to happen; the board that "never self-updated" was a board that needed
updating.

`.team/` is added to the target repo's `.gitignore` by `team init`. It is session state, and
`IFZMods-dist` is a public repo — a bus must never be committed.

### Modules

| Module | Responsibility | Depends on |
|---|---|---|
| `bus.py` | paths, id allocation, atomic write, read | filesystem |
| `schema.py` | validate task / message / result shapes | nothing |
| `verify.py` | records + repo root → verdict table | filesystem (read only) |
| `panes.py` | `exists`, `send_keys`, `pipe_pane` | tmux |
| `cli.py` | argparse, wiring, exit codes | all of the above |

`verify.py` is pure — records in, verdicts out. The highest-value code in the project is also the
easiest to test in isolation.

`panes.py` is the only module that has ever heard of tmux. The bus survives a swap to zellij, or to
no multiplexer at all.

---

## Agent lifecycle

### Grunts are long-lived interactive panes

Each grunt is a real interactive CLI (`qwen`, or `claude-local`) that stays alive across tasks. This
avoids the node-boot tax measured in the `qwen-cli-wiring` memory: a trivial headless `qwen -p` call
took ~100s wall, only ~26s of it API. It also preserves the 2am escape hatch — you can `tmux attach`
and type into a wedged grunt, which is impossible with a supervisor loop occupying the pane.

The cost of a long-lived pane is context accumulation. That is handled explicitly:

**`/clear` fires on a new task, never on a reply.** A new task means the grunt should have no memory;
a reply is a continuation of the question it just asked, and clearing it would discard the context
that produced the question.

### The task file is the entire contract

Because `/clear` precedes every new task, the grunt has no memory of the protocol. Therefore the
protocol ships *with* the task. **`team send` composes the task file** — the lead supplies only
`--question` and `--scope`, and `team send` embeds the result schema, the `team result add` /
`team result done` invocations, and the `team msg --blocked` escape hatch into every task file it
writes. The lead cannot forget the contract, because the lead never authors it.

Having written the file, `team send` sends one short line into the pane:

```
do task .team/inbox/grunt1/001.json
```

The grunt reads its own task file. This avoids `send-keys` quoting and newline-submit hazards
entirely — long strings through `send-keys` are an escaping minefield, and a newline submits early.

It also means no `QWEN.md` / `AGENTS.md` context-file plumbing, and no team protocol polluting the
target repo's own `AGENTS.md`. A grunt that reads only its task file has everything it needs. This
directly inverts papercut #7's "restated the project's own documentation back as a finding": there is
no stale `.md` in its context to regurgitate.

### Scheduling: lead-as-scheduler, zero daemon

`team send` is a CLI the lead runs from Bash. It writes the inbox file, checks the target pane exists,
checks the agent has no open task, then `send-keys` the pointer. Nothing runs that you didn't type.

An agent has an **open task** iff its inbox task has no matching result. Idleness is bookkeeping, not
pty-scraping — `capture-pane` is never parsed to guess whether a model is thinking. The filesystem
already knows.

- `team send <agent> --new-task` refuses when that agent has an open task, unless `--supersede`.
- `team send <agent> --reply <id>` is permitted only when that agent's last message was `blocked` —
  precisely the state in which it is sitting at a prompt.

The honest cost of lead-as-scheduler: nothing schedules while the lead is mid-`/compact`, and a
wedged lead wedges the team. The lead is the orchestrator by definition, so this cost is already paid.

---

## The return path

### Four message types, one directory

Everything a grunt says to the lead is a file in `.team/inbox/lead/NNN.json`, written through
`team msg` so the schema is enforced.

| Type | Meaning | Body |
|---|---|---|
| `result` | Task done, findings attached | records sealed into `results/NNN.json` |
| `note` | Pointer, not payload | e.g. "wrote findings to `docs/x.md`" |
| `blocked` | Needs readvice; grunt is waiting at its prompt | the question |
| `failed` | Could not do it | the reason |

`result` records are structured and mechanically verified. `note` / `blocked` / `failed` carry prose,
which is acceptable because they are not claims about source code. **Prose is allowed only where it
cannot be a lie about the source.**

### Waking the lead

Claude Code already has the wake primitive: a **background Bash** re-invokes the lead when it exits,
including when the lead is idle at its prompt. So the lead backgrounds:

```
team wait --for lead --timeout 3600
```

which blocks until anything lands in `.team/inbox/lead/`, then exits, printing a one-line-per-message
digest. The harness wakes the lead with that digest as the tool result. The lead re-arms the wait each
turn.

This is wake-on-artifact (papercut #6), observable delivery (papercut #4), and zero daemon, built from
machinery that already exists.

**The lead's pane is never `send-keys`-ed into.** Typing into a working Claude Code injects text into
its prompt buffer mid-turn — precisely the "queued behind active turn" opacity of papercut #4,
reproduced with worse failure modes. Grunt panes receive `send-keys`; the lead pane never does.

### Back-and-forth, concretely

1. Grunt hits a wall: `team msg --blocked --task 001 "Is the Zenject container reachable without Harmony?"`
   It then sits at its prompt with context intact.
2. Lead's backgrounded `team wait` exits. Harness re-invokes the lead with the digest.
3. Lead answers: `team send grunt1 --reply 003 "Yes — public static singleton at Foo.Instance. Verify at Foo.cs:88."`
   No `/clear`. The pointer is sent into grunt1's pane; the grunt continues the same task.

### Context discipline

`team msg` caps body length. `team inbox` prints one line per message. Bodies enter the lead's context
only when the lead runs `team show <id>`. The lead chooses what it pays for. This mirrors the existing
`LOCAL_LLM_ROUTING.md` rule: never pipe huge raw output back; write to a file, return digest + path.

---

## The result schema and the verify engine

This is the part that earns the design. Measured grunt accuracy across three rounds was 2-of-5,
0-of-4, then 3-of-4 — and *every* miss was caught by the lead re-reading the cited line. So automate
exactly that, and never let an unverified claim reach the lead's context.

### A record is four fields

```json
{
  "file": "src/TreatmentBed.cs",
  "line": 36,
  "symbol": "TryHeal",
  "evidence": "    public bool TryHeal(Character c, float amount)"
}
```

`evidence` is the **exact source line**, quoted. That single field is what makes verification
mechanical rather than judgmental. A model that greps a symbol name and guesses a line number cannot
produce the matching full source line.

### Verification semantics

`team verify <task>` runs **lead-side, in the lead's repo, and never trusts the grunt.** Comparison is
`actual_line.strip() == evidence.strip()` — indentation drift cannot fabricate a citation, so
tolerating whitespace costs nothing, while content must match exactly.

For each record:

| Condition | Verdict |
|---|---|
| Line at `file:line` matches `evidence` | **PASS** |
| `evidence` matches some other line in the file | **FAIL: off by N** |
| `evidence` matches no line in the file | **FAIL: fabricated** |
| `symbol` is not a substring of `evidence` | **FAIL: symbol/evidence mismatch** |

"Off by N" is the observed `TreatmentBed.cs:43`-for-a-method-at-`:36` papercut, caught automatically.
"Symbol/evidence mismatch" is the observed "reported a grep hit as a verified fact" failure.

The lead's context receives the **table**, not the bodies:

```
result 001: 4 records — 3 PASS, 1 FAIL (line 43: off by 7, actual 36)
```

### Write-side validation

`team result add` validates grunt-side, before anything is written:

- Empty `evidence` → rejected.
- `symbol` not a substring of `evidence` → rejected.

The grunt receives an error it must fix before it can report at all, so the malformed claim never
becomes a file.

Write-side validation and `team verify` overlap deliberately. A grunt can bypass `team result add` by
writing `staging/NNN.json` directly with `Write`, so the lead-side check is the one that actually
guarantees the property. The write-side check exists to fail fast, in the grunt's own context, where
the grunt can still fix it.

### Sealing, write-once, superseding

- `team result add --task 001 ...` appends a record to `.team/staging/001.json`.
- `team result done --task 001` performs two writes, in this order:
  1. atomically renames `staging/001.json` → `results/001.json` (the **seal**);
  2. writes a `result`-type message into `.team/inbox/lead/` (the **announcement**).

  Seal before announce, so the lead can never be woken to read a result file that does not yet exist.
- **The seal is the completion signal**, and the announcement is what wakes the lead. `team wait
  --task <id>` watches `results/`; `team wait --for lead` watches `inbox/lead/`. Both are satisfied by
  the same `team result done`.
- Results are **write-once**. A second seal of the same id is refused.
- `--supersede` marks the old task dead. A late result carrying a dead id is **rejected by id**. That
  is the observed "answered a superseded task" failure, made impossible rather than unlikely.

### The failure that stays non-structural

If a grunt ignores `team result` and writes a scratch `.md` at the repo root (papercut #3), nothing
crashes — the result file simply never appears, `team wait` returns `TIMEOUT: 001 (grunt1)`, and the
task shows pending. We cannot force a model to call a CLI. We can guarantee you find out when it
didn't. The requirement was observability, and observability is what this delivers.

---

## CLI surface

```
team init [--force]                     create .team/, write .gitignore entry
team send <agent> --new-task --question <text> --scope <path>... [--supersede]
team send <agent> --reply <msg-id> <text>
team wait --for lead [--timeout N]
team wait --task <id>... [--timeout N]
team inbox                              one line per message
team show <id>                          full body of one message
team msg --note|--blocked|--failed --task <id> <text>     (grunt-side)
team result add --task <id> --file F --line N --symbol S --evidence E   (grunt-side)
team result done --task <id>                                            (grunt-side)
team verify <task-id> [--show] [--strict]
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | ok (including `verify` with FAILs — a FAIL is information, not an error) |
| 1 | `verify --strict` and at least one FAIL |
| 2 | target pane gone |
| 3 | schema violation |
| 4 | timeout |

Distinct codes let the lead's Bash branch without parsing text.

### Concurrency and safety

- **Atomic writes throughout.** Write a tempfile in the same directory, then `rename`. A poller never
  observes half a JSON document. This is the entire reason a polling bus is safe.
- **Race-safe id allocation.** An id is claimed by `O_EXCL`-creating `.team/ids/NNN`, walking upward
  from the highest existing id until a create succeeds. `O_EXCL` makes the claim atomic; a loser sees
  `EEXIST` and tries the next number. There is no counter file to read-modify-write, because that
  read-modify-write is the race. Done from day one so that multi-grunt fan-out is a later afternoon
  rather than a bus rework.
- **`team wait` accepts a list of task ids** from day one, for the same reason.
- **Polling at 250ms.** Turns take tens of seconds. No inotify, no dependency.
- **Stale bus state is a wrong-answer bug, not an ergonomics gap.** A `.team/` surviving from
  yesterday makes `team wait` return instantly on a stale result, and makes `--new-task` refuse
  because a grunt has an "open task" that died with last night's pane. `team init` therefore refuses
  to run over an existing `.team/` unless `--force`.

---

## The tmux layer

`tmux` is not currently installed. Prerequisite: `pacman -S tmux` (3.7 is in `cachyos-extra-znver4`).

tmux is chosen over zellij for exactly one reason: `send-keys` and `pipe-pane` are the mature
programmatic injection and tee surfaces. Zellij's `action write-chars` is thinner and has no clean
continuous-tee equivalent. The layout is nicer in zellij; the automation is not, and automation is
what this needs.

The layout script creates the session, then for each pane:

```
tmux pipe-pane -o -t <pane> 'cat >> .team/logs/<agent>.log'
```

`pipe-pane` is **phase 1, not an ergonomic extra.** The premise of this system is that grunts are
wrong 30–50% of the time. When a grunt returns a fabricated citation, you need to know whether the
model hallucinated or the task file was ambiguous — and that answer lives in the transcript. Without
`pipe-pane` you have tmux scrollback: bounded, and gone when the pane dies.

`roster.json` is written by the layout script that creates the panes, so it is a record of what was
created rather than a runtime guess. That is the fix for papercut #9.

### `llm-watch` is not part of this

`llm-watch` renders `stream-json` from `claude-local --watch`. An interactive `qwen` pane emits no
such stream — **the pane is the live view.** The handoff's "the visual layer is half-built" held for
headless grunts; with interactive panes the visual layer is free. Keep `llm-watch` for grunts that
happen to run `claude-local --watch`. Do not build a `--team` mode.

---

## Phases

**Phase 1 — the milestone.**
Modules `bus`, `schema`, `verify`, `panes`, `cli`. `roster.json`. `pipe-pane` logging. Race-safe id
allocation. `team init` stale-state guard and `.gitignore` write. Commands: `init`, `send`, `wait`,
`inbox`, `show`, `msg`, `result`, `verify`.

The end-to-end smoke test *is* the milestone: two panes, lead is a real `claude`, grunt1 is an
interactive `claude-local`. Lead sends a lookup task; grunt answers with records; lead verifies and
prints the table. That single loop exercises the bus, the file-drop channel, structural verification,
and context discipline — and proves `/compact` works in the lead pane.

**Phase 2 — ergonomics, no design debt.**
`team board` (`ls .team/results` with columns), `team down` (kill session, remove bus), multi-grunt
fan-out, roster health-checks.

**Phase 3 — deliberately out of scope for now.**
Grunt edits, and the git-worktree isolation they would require. The result schema leaves room for an
`edit` record type; nothing else is designed for it. Verification of an edit is diff review, which is
a judgment call the lead pays context for — the "cheap structural verification" property does not
survive it.

---

## Testing

| Target | Approach |
|---|---|
| `verify.py` | Table-driven over fixture files: exact match, off-by-N, fabricated evidence, symbol/evidence mismatch, whitespace-only difference |
| `schema.py` | Rejection tests: empty evidence, symbol absent from evidence, prose where records belong |
| `bus.py` | Write-once seal; supersede rejects a late id; atomic rename leaves no partial reads; `O_EXCL` id allocation under concurrent callers |
| `panes.py` | Injectable command runner, tested against a fake; one integration test against a throwaway real tmux session |
| End-to-end | The phase-1 milestone loop, run by hand |

---

## Risks and validation items

These are assumptions this design rests on. Each is checked during phase 1, before the code that
depends on it is finished.

1. **`send-keys` into an Ink-based TUI.** Both `qwen` and `claude` render with Ink. Injection likely
   needs `tmux send-keys -t <pane> -l "<text>"` followed by a separate `send-keys Enter`, and may
   need bracketed-paste handling. Verify against a live pane before building `panes.py` around it.
2. **`/clear` exists and resets context in both grunt CLIs.** Assumed for `qwen` and `claude-local`.
   Confirm by sending `/clear` and probing for prior-turn recall.
3. **Background Bash re-invokes an idle lead.** Documented harness behavior ("re-invokes you when it
   exits"). Confirm with a trivial `sleep && echo` before relying on it for wake-on-artifact.
4. **Grunts will sometimes ignore the CLI.** Accepted and unfixable. Mitigated by `TIMEOUT` being
   observable, and by the transcript in `.team/logs/<agent>.log` explaining why.
5. **Lead-as-scheduler stalls when the lead stalls.** Accepted. The lead is the orchestrator.

---

## Related

- `HANDOFF.md` (repo root) — the observed papercuts, verbatim.
- `~/.claude/LOCAL_LLM_ROUTING.md` — "I am overview, local is grunt"; the prefill-throughput
  constraint that makes bounded lookups the right grunt task.
- Memory `qwen-cli-wiring` — headless `-p` cost, context-file loading, `computerUse` prefill tax.
- Memory `claude-local-harness-limit` — why the light harness exists.
