---
name: teamup
description: Use when the user asks to start a TeamChat / grunt team / multi-terminal agent team, or types /teamup. Bootstraps the tmux file-bus team in the current directory and loads the lead's ground rules.
---

# teamup

Start the native agent team in the current directory, and become its lead.

`team` is a real CLI. It is not a metaphor for subagents, and it is not a
workflow. It spawns `qwen` processes in tmux panes beside you, hands them tasks
through a file bus, and **mechanically verifies every code citation they return.**

## Do this, in order

**1. Check you are in tmux.** `echo $TMUX_PANE` must print something like `%17`.
If it is empty, stop and tell the user: this needs a tmux session, because a
grunt is a pane.

**2. Bootstrap.**

```bash
team bootstrap
```

If `team: command not found`, tell the user to run:
`ln -s <path-to>/native-team/bin/team ~/.local/bin/team`

`bootstrap` is idempotent. It creates the git repo and first commit if the
directory has neither, writes the bus, registers **your** pane as the lead, and
spawns no grunts. It refuses if the directory sits inside another git repo.

It will warn that your own `qwen` in this repo now runs in YOLO mode without its
context files. That is true, and `team down` undoes it.

**3. Read your ground rules.** Run `team brief` to get the path, then read that
file. It is the contract: the verbs, the exit codes, the one rule, and the three
traps. Do not skip it and do not summarise it from memory — it is measured, and
it changes.

**4. Report to the user** what came up: the lead pane id, and that they have no
grunts yet.

## Then

Spawn a grunt when a task needs one:

```bash
team grunt add
```

Everything after that is in the brief. Follow it, especially:

> A grunt's citation is not a fact until `team verify` has exited `0` on it.

and

> Do not do the work yourself.

The entire point is that reading a large codebase does not happen in **your**
context window. The moment you open the file the grunt was sent to read, that
saving is gone and you have paid for the grunt as well.
