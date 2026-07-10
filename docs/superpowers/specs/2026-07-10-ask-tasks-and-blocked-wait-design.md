# Ask tasks + non-deadlocking wait — design

**Status:** implemented; verified end-to-end through the MCP layer.

**Goal:** make TeamChat a general delegation bus. Code lookup with mechanical
verification stays as one *mode*; it stops being the contract.

---

## What the demo exposed

A live run: *"spin up a grunt, have it ELI5 E=mc², feed it back."* Sent as a
`find` task. The grunt answered it correctly and completely, then:

1. **could not seal** — a `find` task seals citations, and there is no source in
   scope to cite, so `result_done` refused ("no staged records");
2. **stranded the answer** — the good prose sat in the pane, and TEAMCHAT forbids
   reading panes; there was no channel for a non-citation answer;
3. **deadlocked the lead** — the grunt posted `team msg --blocked` and went idle,
   and `wait.for_tasks` watched only for a seal or a dead marker, so the lead
   slept its full 600 s with the answer already in its inbox.

Only (1) is arguably working as designed. (2) and (3) are the tool being narrower
than the bus underneath it.

## The frame

The user's words: *"this should be a generic chat… the work has been done
already; Claude only has to render the output, not grab tokens and read it
again, unless factually incorrect or the case asks for follow-up."*

So a third task kind, symmetric with `find` and `build`:

| kind | grunt produces | seals on | `verify` |
|---|---|---|---|
| `find` | citations | ≥1 record | re-opens each file |
| `build` | code in a worktree | worktree diff | diff + citations |
| `ask` | prose | an answer | **nothing** — no claim to check |

## The one design hazard, and the fence that closes it

An earlier version of this idea was rejected for a reason that still applies: a
non-citing task is a way to launder an unverifiable answer *about the codebase*
past `verify` — the same reason `--lenient` was refused. "Does `Foo` call `Bar`?"
answered from weights, sealed green, is a trap.

The fence is mechanical, not a rule to remember: **an ask task takes no
`--scope`.** Naming a file is a claim about that file, and a claim about a file
is checkable — which is a `find` task. `api.send` rejects `kind=ask` with a scope
before it composes anything. So the only questions that can be `ask` are the ones
with no file to be wrong about.

`verify` on an ask task prints `NOTHING TO VERIFY — 0 citations`, never `PASS`.
`PASS` is reserved for a citation that survived re-reading a file; printing it
over prose would teach the lead that the prose was checked.

## The vacuous-PASS hole this opens, and closes

`verify.any_failed([])` is `False`. Today the only thing stopping a zero-citation
`find` result from reporting a vacuous PASS is `result_done` refusing to seal an
empty one. Once ask tasks *can* seal with no records, that guard is no longer the
sole line of defence. So `VerifyResult.ok` now fails closed on an empty `find`
(`not self.verdicts → False`), independently of the seal-time guard. Two
defences, because a staging file can be hand-written past the first.

## The answer travels as a file, and rides back in the wait

A grunt types its commands into a shell inside a TUI. A multi-paragraph answer
with a quote or a newline, passed as an argv string, truncates at the first one —
silently, the failure mode this project exists to refuse. So:

```
grunt:  <writes ANSWER.md in its own worktree>
        team result answer --task 007 --from ANSWER.md
        team result done   --task 007
```

`--from` reads a file; `result_answer` never takes prose as an argument. Same
rule as `--build-cmd` taking argv, never a shell string.

Coming back, the asymmetry *is* the design: `team_wait` returns an ask task's
prose in its result (`answers[tid]`), so the lead renders it with no second call
and no re-read — the user's "only render it" point. A `find` task's records are
deliberately **not** carried back; keeping the decompile out of the lead's
context is the entire purpose of a find grunt.

## The blocked-wait fix (all three modes)

Independent of ask tasks. `wait.for_tasks` now also resolves a task when a
`blocked` or `failed` message names it, and returns the messages themselves
(the lead needs the id to `--reply`). `cmd_wait` prints the spelled-out
`team send --reply` line and exits **`5` (BLOCKED)** — a new code, because
`0`–`4` all mean something else and a lead keys its control flow on them.

A blocked grunt is idle at its prompt: returning early is not just faster, it is
the only way the `--reply` handshake (which sends `Escape` first, and so must
never hit a working grunt) is safe.

## Surface added

- `team send --type ask` (no `--scope`)
- `team result answer --task N --from FILE`
- `team answer N` — print a sealed ask answer
- exit code `5` = BLOCKED
- `team_send` MCP `kind` enum gains `ask`; `team_wait` returns `blocked` +
  `answers`; `team_verify` returns `verifiable:false` for ask
- `protocol.ask_body` — the contract a grunt reads for an ask task

## The refusal messages

The grunt is a 30B model with one shot at recovering from a refusal. Measured:
told only "no staged records; nothing to seal", it searched the whole repo for
its subject three times, then blocked. So `_nothing_to_seal` now names the exact
next command for each kind, and the empty-find message says *do not go looking
elsewhere* — block instead.

## Verified

End-to-end through `mcp_server.serve`, driven as a client:

```
team_wait  -> ok:true,  answers[001]='Mass and energy are the same stuff…'
team_verify-> kind:ask, ok:true, verifiable:false, citations:[]  (not isError)
             "ask 001: nothing to verify (0 citations)…"
blocked    -> ok:false, blocked:['003'], "reply with team_send agent=grunt1 reply=004"
```

442 tests. Still untested in a live tmux session: the `--reply` round trip end to
end (the demo's blocked grunt is the case that will exercise it).
