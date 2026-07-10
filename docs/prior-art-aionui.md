# Prior art: how AionUi actually implements team messaging

Investigated 2026-07-10, before building the message layer, to avoid reinventing a wheel — and to
check whether the papercuts in `HANDOFF.md` were accidents or consequences of the design.

## Where the code lives

Not in the Electron app. `/opt/AionUi/resources/app.asar` (367 MB) contains no `team_send_message`
string at all — only a renderer-side regex `/^mcp__aionui-team-|^team_/` that decides whether a tool
call is team-related.

The runtime is **`/opt/AionUi/resources/bundled-aioncore/linux-x64/aioncore`**, a 67 MB ELF binary.
Persistence is **sqlite** at `~/.config/AionUi/aionui/aionui-backend.db`.

This is precisely design option #3 from `HANDOFF.md`: a daemon speaking a `team_*` MCP surface.

## The tool surface

From the binary's string table: `team_run`, `team_spawn_agent`, `team_shutdown_agent`,
`team_send_message`, `team_child_turn`, `team_tasks`, `team_tasks_new`, `team_task_list`,
`team_task_update`, `team_members`, `team_list_models`, `team_list_assistants`,
`team_mcp_stdio_config`, `team_conversation_adapters`.

Requests include `SendTeamMessageRequest`, `SendAgentMessageRequest`, `CancelTeamChildTurnRequest`,
`CancelTeamRunRequest`, `PauseTeamSlotRequest`.

## Papercut #4 (opaque delivery) is in the type system

Sending a message returns a value from **two** enums:

- wake result: `wake_recorded` · `wake_suppressed` · `not_recorded`
- queue state: `queued_for_idle` · `behind_starting_turn` · `behind_active_turn` ·
  `suppressed_by_pause` · `no_active_team_run` · `target_disconnected` · `internal_error` ·
  `queued` · `disconnected` · `unhealthy`

Thirteen outcomes, and not one of them answers "will this message be delivered, and when." The
observed `{"status":"queued","delivery":"wake_recorded","reason":"behind_active_turn"}` is not a bug
in AionUi — it is the API working as designed.

**Our equivalent:** `team send` writes a file and exits. `0` means the file exists. `2` means the
pane is gone. There is no third state, because there is no queue.

## Papercut #5 (board never self-updated) is in the schema

```sql
CREATE TABLE "team_tasks" (
    id TEXT PRIMARY KEY NOT NULL,
    team_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
           CHECK (status IN ('pending', 'in_progress', 'completed', 'deleted')),
    owner TEXT,
    blocked_by TEXT NOT NULL DEFAULT '[]',
    blocks TEXT NOT NULL DEFAULT '[]',
    metadata TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
```

`status` is denormalized state that some agent must remember to `UPDATE` through a separate
`team_task_update` tool. In the live database from the observed session, both rows still read
`pending` — long after the tasks completed. The schema permits the transitions and drives none of
them.

**Our equivalent:** there is no status column and no board file. Inbox file exists ⇒ pending; result
file exists ⇒ done. `team board` is `ls`. A derived board cannot drift, because there is nothing to
update.

`blocked_by` / `blocks` encode a task dependency DAG. We have no such need. YAGNI.

## What is worth stealing: cancellation

`CancelTeamChildTurnRequest` lets the lead **stop a grunt's in-flight turn**. Our design has no
equivalent — `--supersede` marks a task dead and rejects its late result, but the spec never claimed
to stop the work.

It turns out we get this for free, and the mechanism was already in front of us: qwen's own spinner
renders `(7.5s · esc to cancel)`. **`Escape` cancels an in-flight qwen turn.**

`panes.send_line` sends `Escape` before every line, which was added to dismiss the command palette.
That same keystroke cancels a running turn. Two consequences:

1. **`--supersede` genuinely halts the superseded work.** `clear_context` → `send_line("/clear")` →
   leading `Escape` → the grunt's in-flight turn is cancelled, then its context is cleared. Stronger
   than the spec's claim that only the *result* is rejected.
2. **The `blocked`-only guard on `--reply` is load-bearing twice.** `--reply` also sends `Escape`
   first. It is safe only because we refuse to reply unless the agent's last message was `blocked`,
   i.e. it is idle at its prompt. If that guard is ever loosened, a reply will silently cancel a
   working grunt's turn.

Both belong in `panes.py`'s docstring and in a test. Neither requires a design change.

## What we are not taking

- **MCP tool surface.** Papercut #2 was a Qwen teammate unable to resolve `team_send_message`,
  resolving only `mcp__aionui-team__team_send_message`, and concluding it had *no team tools at all*.
  A CLI on `$PATH` has no naming surface to get wrong.
- **A daemon.** `aioncore` is 67 MB of Go that must be running, healthy, and interrogated. Our bus is
  a directory you can `cat`.
- **`team_spawn_agent` / `team_shutdown_agent`.** Dynamic roster management. `team-up N` is static
  and adequate for 1–3 grunts. Phase 2 at the earliest.
- **A `messages` table with `status`, `hidden`, and `position` columns** — transport state and
  presentation state in one row. Our messages are files; the lead decides what to read.
