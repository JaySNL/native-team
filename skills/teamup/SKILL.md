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

**2. Bootstrap.** First settle the grunt's model provider (one-time consent).

Grunts run a local CLI (qwen), and its config lives IN THIS PROJECT'S `.qwen/`, not
your global `~/.qwen`. If `~/.qwen` has a provider configured, **ASK the user**:
copy it into this project so grunts are self-contained (recommended — it then lives
in the project; the user can retarget the model by editing the project `.qwen`), or
skip and configure the project `.qwen` themselves. Bootstrap with the matching flag:

```bash
team bootstrap --copy-provider   # yes: copy your ~/.qwen provider into the project
# or
team bootstrap --skip-copy       # no: you will configure the project .qwen yourself
```

A bare `team bootstrap` (no flag) **REFUSES** when a `~/.qwen` provider exists — it
will not guess consent. If `~/.qwen` has no provider at all, bootstrap prints
`SETUP NEEDED`: tell the user to configure their CLI first (`qwen` once, set a
model/provider), then re-run.

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

Warns your own `qwen` in this repo now runs YOLO grunt mode without context files.
True. The project `.qwen` is project-owned and **lives in the project** — edit it to
retarget the grunt model. `team down` tears down the bus runtime (logs, work, inbox,
ids) so the next bootstrap spins fresh grunts + ids, and **leaves the project
`.qwen` in place** (it no longer restores a global). A pre-existing user `.qwen` is
snapshotted once to `.qwen/settings.json.team-backup` for manual recovery.

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
