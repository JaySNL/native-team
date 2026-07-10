# Worktree lifecycle — design

**Status:** proposed. Nothing below is implemented.

**Goal:** give each grunt a private working tree, so that "what did this grunt
change?" is answerable by looking at one directory.

This spec exists because the `build` task type cannot be made sound without it.
See `2026-07-10-build-task-type-design.md`.

---

## The problem

`team up` puts a lead and 2–3 grunts in **one shared working tree**. A build
task's central safety check is "the set of files this grunt changed must be a
subset of what it declared." In a shared tree that check is unsound: grunt2
creating a file, or the lead editing one — which is the lead's whole job — makes
grunt1's check fail. Grunt1 did nothing wrong.

Containment needs a tree the grunt owns.

---

## Measured

All numbers from `IFZ-Modding` (2913 tracked files, 35 MB tree, 16 MB of
decompiled C#), 2026-07-10.

| Question | Answer |
|---|---|
| Cost of `git worktree add --detach` | **0.10 s, 24 MB** |
| Does a nested worktree under a gitignored dir pollute the main tree's `git status -uall`? | **No.** Main status stays clean. |
| Is `<worktree>/.git` a file or a directory? | **A file**, containing `gitdir: …` |
| If `.team/` is `rm -rf`'d without removing the worktrees first? | `git worktree list` marks them `prunable`; `git worktree prune` fully cleans, no leftover admin dirs |

Per-grunt worktrees are cheap enough that per-*task* worktrees would also be
affordable. Per-grunt is chosen because the lifecycle is simpler and the
containment property is identical.

---

## Design

One worktree per grunt, created at `team up`, removed at `team down`.

```
<root>/.team/work/<agent>/     # git worktree add --detach <path> HEAD
```

Placed under `.team/` because `init` already adds `.team/` to `.gitignore`, so
the main tree never sees it.

The **bus stays in the main tree**. There is exactly one `.team/`.

### The pane's cwd stays the main tree

Tempting and wrong: launch each grunt pane *inside* its worktree. `init` writes
`.qwen/settings.json` at the main root — that file is what supplies
`approvalMode: yolo`, the `excludeTools` lock, and the `context.fileName`
override that stops qwen autoloading `CLAUDE.md`/`AGENTS.md`. qwen resolves its
project root by git root, and inside a worktree **the git root is the worktree**.
A grunt launched there finds no settings: it wedges on an approval prompt,
regains its write tools, and reloads the context files we suppress.

Writing a copy of `.qwen/settings.json` into each worktree would fix that and
break `down`'s dirty-tree guard, which would then fire on every teardown.

So: the pane's cwd is the main root, qwen loads its config there exactly as it
does today, and a build task's shell commands `cd` into the worktree. The
worktree sits under `.team/`, inside the main workspace, so qwen's own tool
sandbox never sees a path outside its project root either.

Task 011 measured that grunts reliably prefix shell calls with `cd <path> && …`
when the task says to.

### The bug this creates, and its fix

`bus.repo_root()` walks up from cwd looking for `.git`. A grunt sitting in
`.team/work/grunt1` finds `.team/work/grunt1/.git` — the worktree's gitdir
*file* — and resolves the repo root to **the worktree**. Its `team result add`
would then look for `<worktree>/.team/staging` and fail, or `team init` there
would create a second bus.

Fix: introduce `bus.bus_root()`, which walks up looking for a directory
containing `.team` rather than one containing `.git`.

From `<root>/.team/work/grunt1` the walk passes `work`, then `.team` (which does
not contain a `.team`), then reaches `<root>`, which does. It terminates on the
one true bus. Verified by inspection of the path structure above.

- `init` and `down` keep using `repo_root()` — the bus does not exist yet, or is
  being destroyed.
- Every other verb (`send`, `wait`, `result`, `verify`, `inbox`, `show`, `log`,
  `msg`) uses `bus_root()` and errors (exit `3`) if no `.team` is found in any
  ancestor. No `--root` flag in the protocol, no environment variable, nothing
  for a grunt to get wrong.

### `find` tasks stay in the main tree

This is an asymmetry, and it is deliberate.

A worktree is checked out from `HEAD`. Your `IFZ-Modding` master has 26
uncommitted files. A grunt in a worktree would read the **committed** version of
code you have since edited, cite a line from it, and `verify` — running against
the main tree — would report `OFF_BY` against a line that never moved. The
citation would be right about a file that isn't yours.

So:

| Task type | Grunt works in | `verify` resolves paths against |
|---|---|---|
| `find` | the main tree (its pane cwd) | the main tree |
| `build` | `.team/work/<agent>`, via `cd` | that worktree (`git -C`, `subprocess(cwd=…)`) |

`find` tasks are read-only by convention, not by enforcement — a grunt's shell
is unrestricted. Across the whole dogfood session, grunts running `find` tasks
used only `Read`, `Search`, and `team`. This is an observation, not a guarantee.

The task file carries the directory the grunt should work in.

---

## Lifecycle

**`team up`** — after the bus exists, for each grunt:

```
git worktree add --detach .team/work/<agent> HEAD
```

Refuses (exit `3`) if `HEAD` is unborn (a repo with no commits) — there is
nothing to check out.

**`team down`** — ordering matters:

1. Refuse (exit `3`) if any grunt worktree has modified or untracked files,
   unless `--force`. Those files are grunt output that was never collected;
   `git worktree remove --force` would silently discard them.
2. `git worktree remove --force` each worktree.
3. `_assert_safe_to_delete` then `shutil.rmtree(.team)` — unchanged.
4. `git worktree prune`, which is a no-op after step 2 and the repair path
   after a crash.

**Crash recovery.** If `.team` is removed by hand, the worktrees become
`prunable` and `git worktree prune` cleans them completely. `team init --force`
runs `prune` before recreating.

---

## Getting work out of a worktree

A grunt's created files live in its worktree, untracked. They must reach the
main tree.

```
team collect <tid>
```

Copies the paths declared in that task's `--create` from the grunt's worktree
into the main tree. **Refuses (exit `3`) if a target already exists.** No
`--replace`, no `--force`: overwriting a file in your working tree because a 30B
model produced one with the same name is not a capability this tool should
offer. The lead resolves the collision by hand.

Copies only declared paths — never the whole tree, never a directory walk.

Deliberately not `git merge`, not `cherry-pick`, not a patch apply. The grunt's
worktree is a scratchpad on a detached HEAD; nothing in it is a commit, and
nothing about it should reach the main tree except the files the lead asked for
by name.

---

## Explicitly not in this spec

- Per-task worktrees. Affordable, but the isolation is no better.
- Sparse checkout. `Decompiled/` is 16 MB; a full checkout costs 0.1 s.
- Letting a grunt commit. Its output is files, and `collect` moves files.
- Protecting the main tree from a grunt that ignores its cwd. Its shell is
  unrestricted; this is containment against accident.
