---
name: teamup
description: Use when the user asks to start a TeamChat / grunt team / multi-terminal agent team, or types /teamup. Bootstraps the tmux file-bus team in the current directory and loads the lead's ground rules.
---

# teamup

Start native agent team in current dir. Become lead.

`team` = real CLI. Not metaphor for subagents, not workflow. Spawns `qwen`
processes in tmux panes beside you, hands tasks via file bus, **mechanically
verifies every code citation they return.**

## Do this, in order

**1. Check tmux.** `echo $TMUX_PANE` must print like `%17`. Empty → stop, tell
user: needs tmux session, grunt = pane.

**2. Bootstrap.**

```bash
team bootstrap
```

`team: command not found` → tell user:
`ln -s <path-to>/native-team/bin/team ~/.local/bin/team`

Idempotent. Creates git repo + first commit if none, writes bus, registers
**your** pane as lead, spawns no grunts. The one setup verb — there is no separate
`team init` to run.

**The dir you were invoked from IS the project, and `bootstrap` pins everything
there — the bus lives where you start it, never up the tree.** If that dir sits
inside a bigger repo (common: the whole `$HOME` is a git repo), `bootstrap` does
NOT refuse and does NOT write the bus to the parent: it git-inits **here** (nested
is fine — the inner `.git` is the boundary) and prints a `NOTE:` saying so, naming
`cd <parent>` if the enclosing repo was what you meant. Read that NOTE and relay it
to the user. If the invocation dir genuinely was the wrong place, `team down` and
re-bootstrap where you meant; otherwise you are done. Pass `team bootstrap --here`
only to silence the NOTE when you already know here is right.

Warns your own `qwen` in this repo now runs YOLO without context files. True.
`team down` undoes.

**2a. Give grunts the memory bank (if project has one).** Grunt starts every
task with zero project memory — none of your accumulated learnings. Repo has a
Claude memory bank → symlink at repo **root** as `memory`, once:

```bash
ln -s <abs-path-to>/memory memory   # e.g. ~/.claude/projects/<slug>/memory
grep -qxF memory .gitignore || echo memory >> .gitignore
```

`config.provision` propagates this link into **every** grunt worktree on each
`grunt add`/`worktree up` → survives worktree teardown. That persistence = why
link lives in main tree, not a worktree. Grunt whose task points at `memory/…`
reads same bank you do. No main-tree link → grunts get no `memory/`; nothing
else changes.

**3. Read ground rules.** Run `team brief` for the path, read that file. It =
the contract: verbs, exit codes, one rule, three traps. Don't skip, don't
summarise from memory — measured, changes.

**4. Report to user:** lead pane id, no grunts yet.

## Then

Spawn grunt when task needs one:

```bash
team grunt add
```

Rest in brief. Follow it, especially:

> A grunt's citation is not a fact until `team verify` has exited `0` on it.

and

> Do not do the work yourself.

Point: reading a big codebase must not happen in **your** context window. Moment
you open the file the grunt was sent to read, that saving gone — you paid for
the grunt too.
