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
- Multi-machine orchestration beyond what a LAN/SSH setup already provides.
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
  logs/<agent>.log         # raw tmux pipe-pane tee (ANSI, redraw-heavy); read via `team log`
  ids/NNN                  # empty marker files; O_EXCL creation claims an id
```

Outside the bus, `team init` also touches the target repo at `.qwen/settings.json` (backing up any
existing file to `.qwen/settings.json.team-backup`) and appends `.team/` and `.qwen/` to `.gitignore`.
`team down` reverses both.

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

### Grunts are long-lived interactive `qwen` panes

The reference grunt is the **`qwen` CLI**, already routed to a local ollama server. One to three of them,
spawned as needed. `claude-local` remains a valid `backend` value in `roster.json` — `panes.py` does
not care what runs in a pane — but qwen is what the design targets.

Each grunt stays alive across tasks. This avoids the node-boot tax measured in the `qwen-cli-wiring`
memory: a trivial headless `qwen -p` call took ~100s wall, only ~26s of it API. It also preserves the
2am escape hatch — you can `tmux attach` and type into a wedged grunt, which is impossible with a
supervisor loop occupying the pane.

The cost of a long-lived pane is context accumulation. That is handled explicitly:

**`/clear` fires on a new task, never on a reply.** A new task means the grunt should have no memory;
a reply is a continuation of the question it just asked, and clearing it would discard the context
that produced the question. Measured: `/clear` does reset qwen's conversational context (a planted
token was not recalled afterwards).

### Grunt configuration: `.qwen/settings.json`

**Measured, not assumed.** Inside a git repo, qwen's context loader walks from cwd to the project root
and loads **every** `QWEN.md`, `AGENTS.md`, and `CLAUDE.md` it finds. `/clear` does not drop them —
they are re-injected context, not conversation. A grunt in `IFZMods-dist` would therefore receive that
repo's prose on every turn, which is papercut #7's worst failure ("restated the project's own
documentation back as a finding") promoted from *likely* to *guaranteed*.

Additionally, qwen's default `approvalMode` is `default` ("Ask permissions"). A grunt calling
`team result add` through `run_shell_command` would raise an approval prompt and hang forever; the
lead would observe only a `TIMEOUT`.

`team init` therefore writes `<repo>/.qwen/settings.json`:

```json
{
  "context": { "fileName": ["TEAM_GRUNT_CONTEXT.md"] },
  "tools": {
    "approvalMode": "yolo",
    "computerUse": { "enabled": false },
    "excludeTools": ["write_file", "replace", "edit", "save_memory", "web_fetch"]
  }
}
```

- `context.fileName` points at a filename that does not exist, so **no** context file loads. Project
  settings *override* the global array rather than merging with it (measured).
- `excludeTools` removes every mutation tool. Phase-1 grunts are read-only **by construction**, not by
  promise. `run_shell_command` is retained solely so the grunt can call `team`.
- `approvalMode: "yolo"` prevents the approval wedge. Safe precisely because the mutation tools are
  gone.
- `computerUse: false` strips 35 of 56 tool schemas — the single largest prefill tax
  (memory `qwen-cli-wiring`).

**This mutates the target repo's configuration.** Consequences, handled explicitly:

- Any pre-existing `.qwen/settings.json` is moved to `.qwen/settings.json.team-backup` by `team init`.
- `team down` restores it. **`team down` is therefore phase 1, not phase 2** — a crashed session must
  not leave a repo hijacked.
- `team init` adds both `.team/` and `.qwen/` to the target repo's `.gitignore`.
- While a session is live, a manual `qwen` run by the user in that repo loses its `CLAUDE.md` context
  and runs in YOLO. `team init` prints this warning.

`--safe-mode` was evaluated and rejected. It suppresses context files with no repo side effects
(measured: ollama routing survives, model stays `qwen3-coder-256k:latest`), but it disables *all*
customizations — including `excludeTools`. That leaves a YOLO grunt holding `write_file`, which
phase 1 forbids.

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

Combined with the `context.fileName` suppression above, a grunt that reads only its task file has
everything it needs and *nothing else*. This directly inverts papercut #7's "restated the project's
own documentation back as a finding": there is no stale `.md` in its context to regurgitate, because
the loader has been pointed at a file that does not exist.

**Injection mechanics, measured.** `tmux send-keys -t <pane> -l "<text>"` followed by a separate
`send-keys Enter` delivers exact literal text into an Ink TUI — no escaping damage, no bracketed-paste
handling needed.

But typing a leading `/` opens qwen's **command palette**, and `Enter` then selects the *highlighted
completion* rather than submitting the line. `/clear` works only because `clear` happens to be
highlighted. `panes.py` therefore:

1. sends `Escape` first, to dismiss any palette or stale input state;
2. sends the literal text;
3. sends `Enter`;
4. for `/clear`, verifies the postcondition by scraping the footer.

The footer renders `N% context used`. That is a free, scrapeable health signal: a successful `/clear`
drops it to the harness baseline, and a grunt whose context is climbing is a grunt about to degrade.

### Scheduling: lead-as-scheduler, zero daemon

`team send` is a CLI the lead runs from Bash. It writes the inbox file, checks the target pane exists,
checks the agent has no open task, then `send-keys` the pointer. Nothing runs that you didn't type.

An agent has an **open task** iff its inbox task has no matching result. Idleness is bookkeeping, not
pty-scraping — `capture-pane` is never parsed to guess whether a model is thinking. The filesystem
already knows.

- `team send <agent>` refuses when that agent has an open task, unless `--supersede`.
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

Lines are split on `\n` only. **Never `str.splitlines()`**, which also breaks on `\v`, `\f`,
`\x1c`–`\x1e`, `\x85`, U+2028 and U+2029. A source file containing any of those would make `verify`
disagree with `sed -n 'Np'` — and a verifier that disagrees with the tool a human checks it with is
worthless. Measured: a two-newline file containing one U+2028 made `splitlines()` report three lines,
grading a *correct* citation as `OFF_BY`.

Eight verdicts, evaluated in this order. Anything that is not `PASS` is a failure.

| # | Verdict | Condition |
|---|---|---|
| 1 | `MALFORMED` | The record is unusable: not a dict; a missing field; `file`/`symbol`/`evidence` not a `str`; `line` not a positive `int` (`bool` excluded); `symbol` or `evidence` empty after `.strip()` |
| 2 | `SYMBOL_MISMATCH` | `symbol` is not a substring of `evidence` |
| 3 | `OUT_OF_TREE` | The cited path is absolute, or escapes `root` after `resolve()` |
| 4 | `NO_FILE` | The path does not exist, or is not a regular file |
| 5 | `UNREADABLE` | The file's bytes are not valid UTF-8 |
| 6 | `PASS` | The line at `file:line` matches `evidence` |
| 7 | `OFF_BY` | `evidence` matches some other line in the file |
| 8 | `FABRICATED` | `evidence` matches no line in the file |

`OFF_BY` is the observed `TreatmentBed.cs:43`-for-a-method-at-`:36` papercut, caught automatically.
`SYMBOL_MISMATCH` is the observed "reported a grep hit as a verified fact" failure. Its duplication
with `schema.validate_record` is deliberate: a grunt can write a staging file directly, so the
lead-side check is the only real guarantee.

**Three verdicts exist because the naive implementation was demonstrably exploitable**, each found by
review and reproduced before being fixed:

- `MALFORMED` — a record of `{"symbol": "", "evidence": ""}` citing a phantom line past EOF returned
  **PASS**, because `"" in ""` is true, `"".strip()` matches a blank line, and splitting a
  newline-terminated file yields a trailing empty element. A wholly vacuous citation, graded verified.
- `OUT_OF_TREE` — `root / rec["file"]` discards `root` when the record's path is absolute
  (`Path("/repo") / "/etc/passwd"` is `Path("/etc/passwd")`). A grunt could cite `/etc/passwd:1` with
  that file's real first line and get **PASS**.
- `UNREADABLE` — reading with `errors="replace"` substitutes U+FFFD for undecodable bytes, so a
  byte-accurate citation into a latin-1 source was graded **FABRICATED**. `verify` must never accuse
  an honest grunt. When it cannot decode a file it says so, and `any_failed` counts that as a
  failure: *the lead must never treat "could not verify" as "verified."*

**`verify_record` never raises.** A grunt-authored staging file may contain anything — `null`, an int,
a truncated object. Every record yields a `Verdict`; one bad record must not abort the batch and take
the good records with it. `render_table` is likewise total, rendering `?` placeholders for fields a
malformed record lacks.

`OFF_BY` reports the first matching line *and the match count*: a lone `}` that matches fifty lines
makes a bare "actual 36" misleading.

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
team init [--force]                     create .team/, write .qwen/settings.json (backing up
                                        any existing), append .team/ and .qwen/ to .gitignore
team down                               restore .qwen/settings.json, kill session, remove .team/
team send <agent> --question <text> --scope <path>... [--supersede]
team send <agent> --reply <msg-id> <text>
team wait --for lead [--timeout N]
team wait --task <id>... [--timeout N]
team inbox                              one line per message
team show <id>                          full body of one message
team log <agent> [--tail N]             de-ANSI'd, de-duped, spinner-stripped transcript
team msg --note|--blocked|--failed --task <id> <text>     (grunt-side)
team result add --task <id> --file F --line N --symbol S --evidence E   (grunt-side)
team result done --task <id>                                            (grunt-side)
team verify <task-id> [--show] [--lenient]
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | ok (a `verify` with FAILs only exits 0 under `--lenient`) |
| 1 | `verify` found at least one FAIL — it fails closed |
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
  yesterday makes `team wait` return instantly on a stale result, and makes `team send` refuse
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

**But raw `pipe-pane` output is a screen recording, not a transcript.** Measured on a four-turn qwen
probe: 341 KB of tee'd bytes, 274 KB after stripping ANSI, and only **13.9 KB of unique non-blank
lines**. Ink redraws the whole frame twice a second, so the log is ~96% spinner
(`I'll be back... with an answer. (7.5s · esc to cancel)`).

`team log <agent>` is therefore a **phase-1 deliverable**, not an ergonomic extra: it strips ANSI
escapes, collapses consecutive duplicate frames, and drops spinner lines, turning 341 KB into
something a human — or a lead paying for context — can actually read.

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
Modules `bus`, `schema`, `verify`, `panes`, `log`, `cli`. `roster.json`. `pipe-pane` logging plus the
`team log` renderer. Race-safe id allocation. `team init` stale-state guard, `.qwen/settings.json`
write + backup, `.gitignore` write. `team down` restore. Commands: `init`, `down`, `send`, `wait`,
`inbox`, `show`, `log`, `msg`, `result`, `verify`.

`team down` is phase 1 because `team init` mutates the target repo's qwen configuration. A crashed
session must not leave a repo hijacked.

The end-to-end smoke test *is* the milestone: two panes, lead is a real `claude`, grunt1 is an
interactive `qwen`. Lead sends a lookup task; grunt answers with records; lead verifies and prints the
table. That single loop exercises the bus, the file-drop channel, structural verification, and context
discipline — and proves `/compact` works in the lead pane.

**Phase 2 — ergonomics, no design debt.**
`team board` (`ls .team/results` with columns), multi-grunt fan-out beyond the first grunt, roster
health-checks (including the `% context used` footer scrape).

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

## Validation results

Measured 2026-07-10 against `qwen` v0.19.8 and `tmux` 3.7b, before any implementation code was
written. Environment: local ollama reachable, `qwen3-coder-256k:latest`.

| # | Assumption | Result |
|---|---|---|
| 1 | `send-keys -l` + `Enter` reaches an Ink TUI | **Confirmed.** Exact literal text; no escaping or bracketed-paste handling needed |
| 2 | `/clear` resets qwen's context | **Confirmed.** A planted token was not recalled after `/clear` |
| 3 | Typing `/clear` is safe | **Refuted.** It opens a 70-entry command palette; `Enter` selects the highlighted completion. Mitigation: `Escape` first, verify postcondition |
| 4 | The task file is the grunt's only context | **Refuted.** In a git repo, qwen auto-loads every `QWEN.md` / `AGENTS.md` / `CLAUDE.md` up to the project root, and `/clear` does not drop them. Mitigation: `context.fileName` override |
| 5 | Project settings override the global `context.fileName` | **Confirmed.** Override, not merge |
| 6 | `--safe-mode` suppresses context files without repo writes | **Confirmed** — and rejected, because it also disables `excludeTools` |
| 7 | `raw pipe-pane` yields a usable transcript | **Refuted.** 341 KB / 4 turns, 13.9 KB unique. ~96% spinner redraw. Mitigation: `team log` renderer |
| 8 | A grunt cannot wedge on tool approval | **Refuted.** Default is `Ask permissions`. Mitigation: `tools.approvalMode: "yolo"` + `excludeTools` |
| 9 | `settings.tools.approvalMode` accepts `default`/`plan`/`yolo`/`auto_edit` | **Confirmed** from the bundle's string table |

### Still unverified — checked in Task 1 before code depends on them

- **Command-scoped shell allowlist.** Gemini-CLI supports `run_shell_command(<prefix>)` in the tool
  allowlist, which would restrict the grunt's shell to `team` only. The qwen bundle is minified and
  the grep was inconclusive. If unsupported, `run_shell_command` stays unrestricted and read-only is
  enforced by `excludeTools` alone — the grunt could still `sed -i` via shell. Accepted risk, recorded
  here rather than hidden.
- **The grep-tool's canonical name** (`search_file_content` vs something else). Only matters if we
  ever move to an allowlist rather than a denylist.
- **Background Bash re-invokes an idle lead.** Documented harness behavior ("re-invokes you when it
  exits"). Confirm with a trivial `sleep && echo` before relying on it for wake-on-artifact.

### Accepted, unfixable

- **Grunts will sometimes ignore the CLI** (papercut #3). Mitigated by `TIMEOUT` being observable and
  by `team log` explaining why.
- **Lead-as-scheduler stalls when the lead stalls.** The lead is the orchestrator by definition.
- **A live team session hijacks manual `qwen` use in the target repo.** `team init` warns; `team down`
  restores.

---

## Related

- `HANDOFF.md` (repo root) — the observed papercuts, verbatim.
- `~/.claude/LOCAL_LLM_ROUTING.md` — "I am overview, local is grunt"; the prefill-throughput
  constraint that makes bounded lookups the right grunt task.
- Memory `qwen-cli-wiring` — headless `-p` cost, context-file loading, `computerUse` prefill tax.
- Memory `claude-local-harness-limit` — why the light harness exists.
