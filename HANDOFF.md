# Native multi-terminal agent team — handoff

**Status:** idea + observed requirements. Nothing built. No code exists.
**Author of this doc:** Claude (Opus 4.8), 2026-07-09, written from a real AionUi team session.
**For:** a fresh Claude with no context on that session.

---

## The ask

Replicate AionUi's team-mode workflow **natively in terminals**: adjacent panes running
`claude` (lead) + `qwen agent 1` + `agent 2` + `agent 3` …, with the same live visual feedback
AionUi gives. The maintainer likes the *working setup* — lead scopes work, grunts execute bounded slices,
lead synthesizes — and wants it without the Electron app.

## The core insight, before you design anything

**The panes are the easy part. The missing piece is coordination.**

tmux or zellij gives you the layout in a config file. What AionUi supplies that a bare terminal
does not:

1. A **task board** (create / assign / update / list).
2. A **message bus** between agents (lead → grunt, grunt → lead, broadcast).
3. **Idle/wake notification** — the lead is told when a grunt's turn ends.

If you start at the window manager, you will get four terminals that cannot talk to each other.
Start at the bus.

---

## Papercuts observed in a real AionUi session

These are not speculation. Each was hit, live, on 2026-07-09 while running a 2-grunt team on an
Infection Free Zone modding task. Any replacement must not reproduce them.

### 1. Slash commands don't exist
`/compact` typed by the user arrived at the lead **as plain text**. There is no TUI to intercept it.
The session is also not visible to the Claude CLI, so the user cannot compact from there either.
Lead context grew to ~193k tokens, re-billed every turn, with no in-session remedy. The only escape
was "start a new conversation."

**Requirement:** the lead pane must be a real `claude` CLI process, so `/compact`, `/clear` and every
other slash command work natively. This is the single strongest argument for the terminal approach.

### 2. Qwen teammates can't resolve bare tool names
Qwen-backed teammates could not call `team_send_message`. Only the fully-prefixed
`mcp__aionui-team__team_send_message` resolved. Worse: when the bare name missed, the model concluded
it had **no team tools at all** and reported that to the user, rather than that it had spelled one
wrong. Both grunts did this independently.

**Requirement:** tool names must be uniform across backends, or the bus must be reachable by
something a model cannot misspell — a file write, a CLI call.

### 3. Teammates could not report back
Even after being handed the correct prefixed name and the lead's slot ID, one grunt still failed to
message the lead and instead wrote its findings to a scratch `.md` at the repo root, which the user
had to notice and relay by hand.

**Requirement:** a **file-drop fallback** is not a workaround, it is the primary channel. Agents
write results to a known path. The lead watches that path. A model cannot fail to `Write` a file.

### 4. Wake semantics are opaque
`team_send_message` returned `{"status":"queued","delivery":"wake_recorded","reason":"behind_active_turn"}`.
Whether the message would ever be delivered, and when, was not knowable from the lead's side.
Messages sent to a busy agent silently queued.

**Requirement:** delivery must be observable. A file in an inbox directory either exists or doesn't.

### 5. Task board never self-updated
Both tasks sat at `pending` on the board through their entire lifecycle, including after the grunts
had finished and reported. The lead had to close them by hand.

**Requirement:** either agents update their own status, or the bus infers status from artifacts
(result file present ⇒ done).

### 6. Idle notifications are noise
The lead received a stream of `idle_notification` messages carrying no information — "a turn ended."
They fired after every turn, including turns that produced nothing.

**Requirement:** notify on *artifact*, not on *turn end*.

### 7. Grunt output cannot be trusted, and verification is the lead's job
Accuracy across three rounds: **2-of-5, then 0-of-4, then 3-of-4** — and the one wrong answer in the
last round was the single question the whole design depended on (it claimed a Zenject container was
unreachable without Harmony; it was reachable via a public static singleton).

Failure modes seen:
- Hallucinated line numbers (cited `TreatmentBed.cs:43` for a method at `:36`).
- Reported grep hits as verified facts.
- **Restated the project's own documentation back as a finding**, having read a `.md` in context and
  never opened the source file it was asked about.
- Answered a superseded task after being re-tasked.

Every single miss was caught by the lead re-reading the cited line.

**Requirement:** the protocol must make verification *cheap and structural*. Grunt results should be
`file:line + signature` records, never prose. The lead should be able to mechanically re-read every
citation. Consider a verify step that automatically `sed`s each cited line and flags mismatches
before the lead ever sees the claim.

### 8. Lead context is the scarce resource
Grunt reports land in the lead's context as tool results. Long reports directly consume the lead's
budget — which is exactly the resource the whole "offload to grunts" strategy exists to protect.

**Requirement:** grunts write to files; the lead reads *pointers and digests*, not bodies. Cap
result size. This mirrors the existing `~/.claude/LOCAL_LLM_ROUTING.md` rule — "never pipe huge raw
output back; have it write to a file, return digest+path."

### 9. Model labelling was wrong
`team_members` reported the **lead** (a Claude backend) as running model `qwen3-coder-256k:latest`.
Cosmetic, but it means the roster was not a reliable source of truth about what was running where.

---

## What already exists — do not rebuild

From `~/.claude/LOCAL_LLM_ROUTING.md` and `~/.claude/CLAUDE.md`:

| Tool | What it does |
|---|---|
| `llc` (`~/.claude/tools/llc.py`, on PATH) | Slice-delegate. `llc File.cs:1840-1920 "task"`. Bounded slice + one task, no agent harness. Seconds. |
| `claude-local` / `lll` | Whole agent run against the local model. Zero Anthropic tokens. `--watch` streams to `llm-watch`. |
| `llm-watch` | Claude-Code-style **live view** of a `claude-local --watch` run — tools, diffs, holding. |
| `llm-top` | Live gauge: server + model + MCP/llc jobs. |
| `local_delegate` MCP | Tier-2 orchestration against the local Ollama. |

**`llm-watch` already solves the "visual feedback" half of the ask.** The local machine
runs `qwen3-coder-256k` and `qwen3-thinking-256k`, both Qwen3-30B-A3B MoE @ 256k ctx, ~90-97 tok/s.

**The routing rule that governs all of this:** *"I am overview, local is grunt."* The 30B does not
choke on context size (256k fits anything) — it chokes on **prefill throughput** (~400 tok/s). So
never make the local model navigate. The lead greps and scopes; the grunt receives exact lines.

---

## Design sketch (a starting point, not a decision)

Three shapes were considered. Recommendation is **#2**.

**1. Claude Code's own subagents.** `Agent` / `SendMessage` / `Task*` already exist and do real
parallel fan-out with wake-on-complete. Zero build. But they render as *background tasks*, not
visible panes — a notification, not a live view. And they'd all be Claude, not Qwen. Fails the ask.

**2. tmux + a file-based bus.** ← recommended
Each pane runs a real CLI (`claude` for the lead, `claude-local` or a Qwen CLI for grunts). A watched
directory carries state:

```
.team/
  tasks.json              # the board
  inbox/<agent>/*.json    # messages to that agent
  results/<task-id>.json  # grunt output: file:line records, never prose
```

Agents poll their inbox. The lead watches `results/`. Reuses `llm-watch` for the visual layer.
Simple, debuggable, no daemon, and **the bus is a directory you can `cat`**. Latency is polling-shaped,
which is fine — these turns take tens of seconds.

Critically: the lead is a real `claude` process in a real terminal, so **`/compact` works** (papercut #1).

**3. A daemon speaking the `team_*` MCP surface.** Panes attach as MCP clients. Most faithful to
AionUi, most work, and it would inherit the design decisions that produced papercuts #2, #4 and #6.

---

## Non-goals

- Rebuilding `llm-watch`, `llm-top`, `llc`, or `claude-local`.
- Making grunts smarter. They are 30B models doing bounded lookups. The protocol should assume they
  are wrong and make that cheap to detect (papercut #7), not try to prevent it.
- Multi-machine orchestration beyond what a LAN/SSH setup already gives.

---

## Suggested first milestone

The smallest thing that proves the concept and kills the worst papercut:

> Two tmux panes. Lead is a real `claude`. Grunt is `claude-local`. Lead writes a task to
> `.team/inbox/grunt/001.json`; grunt executes it and writes `.team/results/001.json` containing
> `{file, line, symbol, access}` records; lead reads the result file, `sed`s each cited line, and
> reports which citations verified.

That single loop exercises the bus, the file-drop fallback, the structural verification, and the
context discipline — and it proves `/compact` works in the lead pane.

---

## Before you build

**Run `superpowers:brainstorming` first.** This is a build, not a bug fix, and this doc is a
requirements dump, not an approved design. The first design question to put to the maintainer:

> What carries `team_*` semantics between panes — a polled directory, a small daemon, or Claude
> Code's existing `Agent`/`SendMessage` tools (accepting that they render as background tasks rather
> than visible panes)?

Related memory: `native-multi-terminal-team`, `aionui-qwen-teammate-mcp-prefix`.
