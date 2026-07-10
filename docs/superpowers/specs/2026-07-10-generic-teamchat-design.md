# Generic TeamChat — design

**Status:** implemented and run live, 2026-07-10.

**Goal:** make `team` usable in any git repo, from a tmux session the user already
started, with grunt panes created on demand by the lead rather than fixed at
startup.

Today `bin/team-up` *creates* the session, refuses to run if one exists, fixes
the grunt count at launch, and addresses panes by index. None of that survives
contact with "I opened tmux, I started claude, now spawn me a grunt".

---

## Measured

tmux 3.7b, this machine, 2026-07-10. Every row was run, not recalled.

| Question | Answer |
|---|---|
| `split-window -P -F '#{pane_id}'` returns the new pane's id | **Yes** — `%505` |
| `split-window -e VAR=val` sets env in the new pane only | **Yes** — pane printed `ENV=hello` |
| `$TMUX_PANE` inside a pane is that pane's id | **Yes** |
| `send-keys -t %bogus` | rc **1**, `can't find pane`. Never misdelivered. |
| `display-message -p -t %bogus '#{pane_id}'` | rc **0**, **empty stdout** |
| `list-panes -t <pane-id>` | lists **every pane in that pane's window**, not the target |
| Kill pane index 1 of `0=%506 1=%507 2=%508` | → `0=%506 1=%508`. **Indices renumber; ids never do.** |
| `send-keys` into a pane whose process died (`remain-on-exit on`) | rc **0**. Silently accepted. |
| `display-message -p -t <dead pane> '#{pane_dead}'` | `1` |

### Three bugs these facts expose in the code as it stands

**B1 — the roster addresses panes by index.** `roster.json` stores
`"grunt1": {"pane": "team:0.1"}`. Kill grunt1 in a three-grunt session and
grunt2's pane inherits index 1. `team send grunt1` then types a task into
grunt2's pane. Nothing detects it: both panes run qwen.

**B2 — `Panes.exists` asks the wrong question.** It runs
`list-panes -t <target>`, which lists the target's *window*. A window with any
pane alive answers yes for a pane that is gone.

**B3 — a dead pane accepts keystrokes.** `team-up` sets `remain-on-exit on` for
every grunt (so `pane-died` fires and the corpse stays visible). `send-keys` into
that corpse returns 0. So `team send` prints `sent task 007 to grunt1`, exits 0,
and no process will ever read it. The lead then blocks in `team wait` until its
timeout. The `pane-died` hook does post a `failed` message, so a lead waiting on
its inbox recovers — but `send` itself reports a success that did not happen.

B1 and B3 are live correctness bugs independent of this feature. They are fixed
here because this feature rewrites the code that carries them.

---

## Design

### Panes are identified by tmux pane id

`roster.json` v2:

```json
{
  "lead":   {"pane": "%504", "backend": "claude", "cwd": "/home/u/p"},
  "grunt1": {"pane": "%505", "backend": "qwen",   "cwd": "/home/u/p/.team/work/grunt1"}
}
```

A pane id is stable for the life of the pane and unique across the server, so a
roster entry can never come to mean a different pane. `panes.py` already passes
`target` straight to `tmux -t`, and every verb it uses (`send-keys`,
`capture-pane`, `pipe-pane`, `set-option -p`, `set-hook -p`) accepts a pane id.

`cwd` is recorded for diagnostics only. Nothing reads it back.

No migration path. A roster is created by `team up` and destroyed by `team down`;
one that predates this change belongs to a session that is already gone.

### `Panes.exists` becomes a real probe

```python
def exists(self, target: str) -> bool:
    proc = self.runner(["tmux", "display-message", "-p", "-t", target,
                        "#{pane_id} #{pane_dead}"])
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        return False          # no such pane; tmux exits 0 with empty stdout
    return out.split()[-1] == "0"   # a dead pane is not a pane you can send to
```

Empty stdout is the bogus-target signal, because tmux does not set a non-zero
exit for it. The `pane_dead` check is what makes `send` stop lying: a grunt whose
qwen died reports `PANE_GONE` (exit 2) at `send` time instead of after a
timeout.

`send_line` and `clear_context` keep their existing behaviour. `cmd_send` already
calls `exists()` before composing a task, so a dead grunt now costs no task id.

### `team grunt add [name]`

Creates one grunt: worktree, pane, log, death hook, roster entry. In that order,
because a pane must be launched *in* its worktree (Amendment 1) and so the
worktree has to exist first.

```
team grunt add            # names it grunt<N>, the lowest free N
team grunt add scout      # or name it yourself
```

Preconditions, each refusing with exit `3`:

- a bus exists (`bus_root()`, as every non-`init` verb)
- `$TMUX` is set, or `--window <target>` names one. There is no way to guess
  which window the lead means, and adding a pane to the wrong one is worse than
  refusing.
- the name is not already in the roster
- `qwen` is on `PATH`

Then:

1. `worktrees.add(root, name)` + `config.provision(work)`. If `HEAD` is unborn,
   print a warning and fall back to `cwd = root` — `find` tasks work in a repo
   with no commits; `send --type build` will refuse later, on its own terms.
2. `split-window -P -F '#{pane_id}' -t <window> -c <cwd> -e PATH=<bin>:$PATH qwen`
   The `-e` is what makes a grunt able to run `team result add` in a repo that is
   not this one: `<bin>` is this package's `bin/` directory, holding the `team`
   shim. Verified: `-e` reaches the new pane and nothing else.
3. `select-layout tiled`
4. `pipe-pane -o` → `.team/logs/<name>.log`
5. `set-option -p remain-on-exit on`, `set-hook -p pane-died <script>`
6. roster read-modify-write, via `bus.write_json` (atomic).

The pane-died hook script moves from `bin/team-up` into `panes.py`, which is
where tmux knowledge is allowed to live. It is written to a `mkdtemp` directory,
never under `.team/`: `team down` deletes `.team` while panes may still be alive,
and a hook whose script has vanished fails noisily inside tmux.

The script is generated with `shlex.quote` on every interpolated path, and calls
`<bin>/team --root <root> msg --agent <name> --failed --task pane-died …`. It
does not rely on `PATH`, because a tmux hook runs under `sh -c` with the server's
environment.

### `team grunt rm <name> [--force]`

Symmetric, and refuses before it destroys:

1. If the worktree is dirty and not `--force`: refuse, naming a file. Same rule
   and same words as `team down`. A grunt's output is untracked files.
2. `kill-pane -t <id>` if the pane still exists (a grunt killed by hand is not an
   error).
3. `worktrees.remove`, `worktree prune`.
4. Drop the roster entry.

`--force` discards the worktree, exactly as `down --force` does.

### `team up [n]` replaces `bin/team-up`

A Python verb, so that pane creation has one owner.

- **Inside tmux** (`$TMUX` set): the lead is *the pane you are in* —
  `$TMUX_PANE`, verified to be that pane's id. Register it as `lead`, pipe its
  log, and add `n` grunts to the current window. No session is created. This is
  the flow the user asked for: `cd ~/Projects/TeamChat && tmux`, start claude,
  and the lead spawns its own grunts.
- **Outside tmux**: `new-session -d -s $TEAM_SESSION -c <root> claude`, register
  that pane as lead, add `n` grunts, print the attach line.

`n` defaults to **0**. The whole point is that grunts are spawned on demand; a
lead that wants one runs `team grunt add`. `team up 2` still works for the old
flow.

`bin/team-up` becomes a three-line shim (`exec team up "$@"`) so muscle memory
and the existing docs keep working.

### `bin/team`

```sh
#!/bin/sh
exec python3 -m team "$@"     # with PYTHONPATH set to this repo, resolved from $0
```

Today `python3 -m team` only resolves because `PYTHONPATH` happens to be
exported in the shell that ran `team-up`, and the grunt panes inherit it. That is
an accident of the dogfood setup, not a design. The shim resolves the package
from its own location, and `grunt add` injects `<bin>` into each grunt pane's
`PATH` with `split-window -e`.

The user puts `bin/` on their `PATH` once, or symlinks `bin/team` into
`~/.local/bin`. `team brief` already resolves `TEAMCHAT.md` from the module, so a
lead in any repo can read its ground rules.

---

## What a generic TeamChat still requires

- **A git repo.** `init` locates the root by `.git` and `_assert_safe_to_delete`
  re-derives it independently before removing anything. Relaxing that guard to
  support non-git directories is not worth what it protects against.
- **At least one commit**, for build tasks only. `worktree add` cannot check out
  an unborn HEAD. `find` tasks are unaffected, and `grunt add` says so rather
  than failing.
- **`qwen` on `PATH`**, checked at `grunt add` rather than discovered as a dead
  pane.

---

## Explicitly not in this spec

- Backends other than `claude` (lead) and `qwen` (grunt). The roster records
  `backend`; nothing branches on it yet, and inventing that seam before a second
  grunt backend exists is speculation.
- Re-attaching to grunts that outlive the lead.
- Choosing the split direction, pane size, or layout. `tiled`, as today.
- A `team grunt ls` verb. `team inbox`, `tmux list-panes`, and `cat
  .team/roster.json` all already answer it.
- Rewriting `roster.json` when a pane dies. The `pane-died` hook reports to the
  lead's inbox; the entry stays, and `send` now fails closed against `pane_dead`.

---

## Test plan

`panes.py` is tested with a fake runner, as today, so none of this needs tmux to
run in CI.

| Test | Kills the mutant |
|---|---|
| `exists()` is False on empty stdout with rc 0 | the bogus-target probe |
| `exists()` is False when `pane_dead` is `1` | B3 — the silent send |
| `exists()` is True only on `<id> 0` | — |
| `grunt add` refuses outside tmux with no `--window` | guessing a window |
| `grunt add` refuses a name already in the roster | clobbering a live grunt |
| `grunt add` creates worktree before pane | a pane rooted in the main tree |
| `grunt add` records the id returned by `-P -F`, not an index | B1 |
| `grunt add` on an unborn HEAD warns, falls back to root, still registers | |
| `grunt rm` refuses a dirty worktree without `--force` | discarding grunt work |
| `grunt rm` on an already-dead pane still cleans up | |
| `up` inside tmux registers `$TMUX_PANE` as lead and creates no session | |
| `up` outside tmux creates a session | |
| the hook script quotes every interpolated path | the space-in-path hazard |

Plus one live run: `cd ~/Projects/TeamChat`, `tmux`, `claude`, `team up`,
`team grunt add`, send a `find` task, verify, `team grunt rm grunt1`, `team down`.

---

## Self-check

Four defects in the design above. One blocks.

**D1 (blocking) — `grunt add` then `send` is a race.** A pane created at T+0 runs
qwen, which takes **~6 seconds** to draw its prompt. `cmd_send` immediately calls
`clear_context`, which types `/clear` and polls for the command palette to close.
Keystrokes sent before the TUI is listening are dropped on the floor: no error,
no task, and a lead that blocks in `team wait` until timeout. `team-up` hid this
because a human took seconds to attach and read the output before sending
anything. On-demand spawning removes the human.

Measured, a fresh qwen pane at ~6s and again at ~14s, both show:

```
>   Type your message or @path/to/file
```

That placeholder is the empty-prompt hint. It is drawn when qwen is listening and
disappears the moment text is typed — so it is a readiness signal *and* an idle
signal, in one string.

Fix: `panes.wait_ready(target, timeout=60.0)` polls `capture-pane` for
`Type your message`, raising `PaneError` on timeout (exit `4`, `TIMEOUT`).

- `grunt add` calls it before returning, so the verb's success means "this grunt
  can be sent to".
- `cmd_send` calls it before `clear_context`. Cheap insurance, and it also covers
  a grunt that is mid-turn on a superseded task.

Like `PALETTE`, this couples us to one qwen version's chrome. It is a module
constant next to `PALETTE`, in the one module allowed to know about TUIs, and it
fails loudly rather than silently when it drifts.

**D2 — `grunt rm` orphans an open task.** Killing a grunt with a task in flight
leaves `tasks/<agent>/<tid>.json` open forever: `compose_task` will later refuse
to dispatch to a re-added grunt of the same name (`already has open task`), and
`wait --task` on it never returns. `grunt rm` marks any open task dead
(`bus.mark_dead`), exactly as `--supersede` does.

**D3 — `team down` leaves grunt panes running in deleted worktrees.** `down`
removes the worktrees and then `rmtree`s `.team`. Every grunt pane's cwd is now a
directory that does not exist; qwen keeps running in it. `down` must kill the
grunt panes first — never the lead's, which is where the person typing `down`
is sitting. `config.py` may not import `panes`, so this belongs in `cmd_down`,
which already holds both.

Ordering: kill panes → drop worktrees (which can still refuse on dirty) → rmtree.
A refusal after killing the panes is survivable; the panes are the cheap thing.

Correction to that ordering: `_drop_worktrees` refuses on a dirty worktree, and
that refusal is the whole reason `down` is safe. Killing panes before it means a
refused `down` has already destroyed the grunts. So: **check dirty, kill panes,
drop worktrees, rmtree.** `_drop_worktrees` gains no new behaviour; `cmd_down`
asks `wt.dirty` itself first, and lets `config.down` re-check.

**D4 — `$TMUX` alone does not identify the lead's pane.** The lead runs
`team up` through its own shell, so `$TMUX_PANE` is set and correct. But a user
running `team up` by hand from a *different* pane would register that pane as
lead. Require **both** `$TMUX` and `$TMUX_PANE`, and accept `--lead-pane <id>` to
override. Refuse (exit `3`) if `$TMUX` is set and `$TMUX_PANE` is not, rather than
guess.

### Revised order of work

1. `panes.py`: `exists()` probe, `wait_ready()`, `split()`, `kill()`, hook-script
   writer. Fake-runner tests. (Fixes B2, B3, D1.)
2. `bin/team` shim; `roster` helpers move behind one accessor.
3. `team grunt add` / `team grunt rm`. (Fixes B1, D2.)
4. `team up` in Python; `bin/team-up` becomes a shim. (D4.)
5. `cmd_down` kills grunt panes. (D3.)
6. Live run in a scratch repo.

---

## The live run, and the two bugs it found

A throwaway git repo, a tmux session started by hand, a fake lead in pane 0.
`team up --lead-pane %594` registered the lead and created no session;
`team grunt add` spawned a real qwen in `.team/work/grunt1` in 1.3s; the footer
read `➜ grunt1 · git:(1449043)` and **YOLO mode**, proving it loaded the
provisioned `.qwen/settings.json` from inside the worktree.

`team send --scope bed.py` → the grunt read the file, ran `team result add`
through the PATH-injected shim with **no `PYTHONPATH` set anywhere**, and
`team verify 002` reported `1 PASS`. `--allow-dirty` and the stale-scope refusal
both behaved. `down` refused while a worktree held uncollected work, killed both
grunt panes on `--force`, and left the lead's pane alive.

Two defects, neither of which any unit test could have found.

**L1 — `send` handed the grunt a path relative to the main root.** The grunt's
cwd is its worktree, which has no `.team/` in it: the bus lives once, in the main
tree. `do task .team/inbox/grunt1/001.json` named nothing. The grunt guessed the
absolute path, produced a malformed tool call, and the task died silently. This
was collateral damage from Amendment 1 -- under the old design the pane's cwd was
the main root and the relative path resolved. `send` now sends an absolute path.

**L2 — the readiness marker was the input placeholder, and qwen rotates it.**
`Type your message or @path/to/file` is a *ghost hint*, and qwen cycles it
through suggestions. A healthy idle pane sat showing `post comments`; another
showed `team result done --task 002`, which reads exactly like a half-typed
command and is not -- typing replaces it, backspace restores it, Enter submits
nothing. Keyed on that string, `wait_ready` told the lead its perfectly healthy
grunt "may have failed to start", and `send` refused forever.

Readiness now keys on the mode footer (`… (shift + tab to cycle)`), which does
not rotate. It tests **drawn**, not **idle**: `--supersede` exists to interrupt a
working grunt, and `send_line`'s leading Escape is what cancels the turn, so a
check that waited for the spinner to clear would wait for the very turn it means
to kill. `BUSY` (`esc to cancel`) is exported for a caller that genuinely wants
idleness; nothing needs one yet.

L2 is worth generalising: **the pane is a rendering, not an API.** Every string
this project matches against it -- `PALETTE`, `READY`, `BUSY` -- is a guess about
someone else's chrome. Each one belongs in `panes.py`, each fails loudly rather
than silently, and each is worth re-measuring when qwen updates.
