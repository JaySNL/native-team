# Route guard — design

**Status:** implemented; verified live. Not yet installed.

**Goal:** make "do not do the work yourself" enforced instead of merely written
down.

`TEAMCHAT.md` ends that section with a sentence I wrote and did not like:

> Nothing enforces this rule. It is on you.

A lead whose grunt returns `FABRICATED` is one `grep` away from the answer. It
will take that grep. Every time it does, the grunt's whole purpose — keeping a
16 MB decompile out of the lead's context — is spent, and the lead has paid for
the grunt as well.

---

## The mechanism

A Claude Code **PreToolUse hook**. Contract, copied from the user's existing
`~/.claude/hooks/route-guard.py` rather than guessed:

- JSON on stdin: `tool_name`, `tool_input`, `cwd`.
- JSON on stdout:
  `{"hookSpecificOutput": {"hookEventName": "PreToolUse",
    "permissionDecision": "allow"|"deny", "permissionDecisionReason": str,
    "additionalContext": str}}`
- Exit 0 always. **Fail open** on any error: a broken guard must never break a
  tool call.

Registered globally, matcher `Read|Grep|Glob|Bash`. It is a no-op in every
directory that does not contain a `.team/` bus with an **open task**, so it costs
one `stat` in any other project. That is why it can be installed once rather than
written into each repo's `.claude/settings.json` — `team init` already mutates
`.qwen/settings.json`, and mutating a second config file it would have to restore
is a liability for no gain.

---

## What is guarded

An **in-flight scope**: for every task file in `.team/inbox/<agent>/<tid>.json`
that is neither sealed (`.team/results/<tid>.json`) nor dead
(`.team/dead/<tid>`), take its `scope` list.

Build tasks carry an empty scope and guard nothing. Their output lives in a
worktree the lead cannot usefully read anyway, and `collect` is the sanctioned
path.

The guard lifts the moment the task seals. No cleanup, no state of its own: the
bus already knows.

## The decision table

`target` is a path from the tool call, resolved against the bus root.

| Case | Decision |
|---|---|
| `TEAM_ROUTE_GUARD=0` | allow, silent |
| no bus in any ancestor of `cwd` | allow, silent |
| no open task | allow, silent |
| `Read` / `Grep` / `Glob` whose target is **inside or equal to** a scope path | **deny** |
| `Bash` with any argument resolving inside a scope path | **deny** |
| `Bash` whose first word is `team` | allow, silent |
| a target that is an **ancestor** of a scope path (e.g. repo-wide `Grep`) | allow + nudge |
| anything else | allow, silent |

The ancestor case is the one that decides whether this tool is usable. A
repo-wide `Grep` does technically cover the scope, and denying it would block the
lead's ordinary work — reading its own source while a grunt reads the decompile.
Denying only when the lead reaches *into* the scope catches the actual cheat
(opening the file the grunt was sent to read) and leaves everything else alone.
This follows the existing route-guard's rule: hard-deny the clear case, warn on
ambiguity, never break legitimate work.

`Bash` needs the `team` allowlist or the guard eats itself: `team send grunt1
--scope src/foo` contains `src/foo` as an argument, and would be denied by its
own in-flight task.

## The deny message

The reason string is the only thing the lead sees, so it carries the recovery:

```
grunt1 is reading src/combat/ right now (task 007). Reading it yourself spends
the grunt: the file lands in your context and you have paid for both.

    team wait --task 007 --timeout 600
    team verify 007

If 007 failed, re-ask with a correction. Measured: a grunt whose two citations
both failed returned 2/2 PASS after one correction naming what was wrong.

Genuinely need it? TEAM_ROUTE_GUARD=0, or `team grunt rm grunt1`.
```

---

## Explicitly not in this spec

- Guarding the grunt. A grunt is `qwen`; this hook is a Claude Code hook and will
  never see it.
- Guarding `.team/logs/`. "Never read a grunt's pane" is a real rule with a
  sanctioned verb (`team log`), but reading a log costs no context worth
  protecting and denying it would be theatre.
- Blocking repo-wide `Grep`. See above.
- Blocking `Write`/`Edit`. The lead is supposed to write code; the grunt's
  containment is a worktree, not the lead's tool list.
- Any state of the guard's own. Everything it needs is already in the bus.

---

## Test plan

Pure function `decide(payload, root_finder) -> (decision, reason)`, so the tests
never spawn Claude. Plus one live check: pipe a real payload into the script.

| Test | Kills |
|---|---|
| no `.team` anywhere → allow | a guard that fires in unrelated repos |
| bus, but no open task → allow | a guard that never lifts |
| sealed task → allow | forgetting `results/` |
| dead (superseded) task → allow | forgetting `dead/` |
| `Read` of a file inside scope → deny | the whole point |
| `Read` of a file outside scope → allow | over-blocking |
| `Grep` with `path` inside scope → deny | |
| `Grep` with `path` = repo root, scope beneath → allow + nudge | breaking ordinary work |
| `Glob` whose literal prefix is inside scope → deny | |
| `Bash` `grep -n X src/scoped/F.cs` → deny | the obvious bypass |
| `Bash` `team send … --scope src/scoped` → allow | the guard eating itself |
| `Bash` `ls /elsewhere` → allow | |
| `TEAM_ROUTE_GUARD=0` → allow | no escape hatch |
| malformed stdin → allow | fail-closed by accident |
| scope path escaping the root (`../../etc`) → allow, ignored | a scope that guards the filesystem |
| build task (empty scope) → allow | guarding a task with nothing to guard |

---

## Self-check

**Open risk (must be settled live, not by reading).** The user already runs a
`PreToolUse` hook matching `Bash|Read`. When two hooks match the same call and
one says `allow` while the other says `deny`, which wins? If the answer is "the
last one to speak", this guard is decorative in exactly the repo it matters in.

Not guessable. The plan: implement, register, and *try to read a scoped file in a
live session with a task in flight*. If deny does not win, the fallback is to
call this module **from** the existing route-guard, so there is one hook and one
decision. The module is written as `decide(payload) -> (decision, reason, ctx)`
precisely so that fallback costs one import.

**Two smaller ones.**

`Grep` with no `path` defaults to the cwd, which is the bus root — an ancestor of
every scope. Under the table above that is "allow + nudge", which is intended,
but it means the single most natural cheat (`Grep` for the symbol, repo-wide)
gets a nudge rather than a deny. Accepted: the alternative blocks the lead from
grepping its own source while any task is open, which would get the guard
switched off within a day. A nudge that names the in-flight task is the honest
trade.

The `team` allowlist is first-word only. `cd src && team send …` would be denied
by its own scope. Acceptable: `team` needs no `cd`, and the deny message says how
to proceed.


---

## The open risk, settled

**Deny wins.** Measured, not read: a scratch repo with an open task scoped to
`src/`, a project `.claude/settings.json` registering this guard, and the user's
existing global route-guard also matching `Read`. A headless `claude -p "Use the
Read tool on src/A.cs"` came back with:

> Blocked by TEAM_ROUTE_GUARD hook, not read: "grunt1 is reading src right now
> (task 001)..." — suggests `team wait --task 001 --timeout 600` then
> `team verify 001`; override needs `TEAM_ROUTE_GUARD=0`.

It never saw the file. A second hook returning `allow` does not override a
`deny`, so the fallback (calling `decide()` from the existing guard) is not
needed. The whole reason string reached the model, recovery commands included.

Full matrix, live, against a real bus:

| call | task open | task sealed |
|---|---|---|
| `Read src/A.cs` | **deny** | allow |
| `Bash grep -n secret src/A.cs` | **deny** | allow |
| `Bash team verify 001` | allow | allow |
| `Grep secret .` | allow + nudge | allow |
| `Read README.md` | allow | allow |

The guard lifts the instant the task seals, with no cleanup: `results/<tid>.json`
appearing is the whole mechanism.

## Installing it

Not installed by this commit. It belongs in `~/.claude/settings.json`, which is
the user's global config across every project, so it is their call:

```json
{
  "matcher": "Read|Grep|Glob|Bash",
  "hooks": [{"type": "command",
             "command": "python3 /home/user/Projects/native-team/hooks/team_route_guard.py",
             "timeout": 5,
             "statusMessage": "team-route-guard"}]
}
```

Added as a second entry in the existing `PreToolUse` array. In any directory
without a `.team/` bus holding an open task it costs one `stat` and returns
`allow`.
