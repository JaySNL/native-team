# Worktree lifecycle — design

**Status:** implemented, then amended. Read **Amendment 1** at the bottom before
the two sections it supersedes.

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

> **Superseded by Amendment 1 (below).** This section is wrong, and live task 013
> proved it wrong. Kept for the record; read the amendment for what is built.

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

> **Superseded by Amendment 1 (below).** `find` tasks now also run in the
> worktree, and `send` refuses a scope path that is dirty in the main tree.

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

---

## Amendment 1 — the fence had a gate (2026-07-10)

Written after the first real build task, **013**, run against the live tmux
session. Everything in this section is measured, not predicted.

### What happened

`team send grunt1 --type build --create probe/WaveTally.cs --build-dir probe`

The grunt's pane log, line 30:

```
✓ WriteFile Writing to probe/WaveTally.cs
```

That path is relative to **qwen's project root, which was the main tree**. The
file landed in the main tree, and only afterwards did the grunt `mkdir` the same
path inside its worktree and write it again. Both copies are byte-identical
(`md5 361f8be49b2457426a3baece21f88269`). `git status --porcelain -uall` in the
main tree shows `?? probe/WaveTally.cs`.

`team verify 013` reported:

```
build 013: CONTAINMENT — grunt1 changed files it did not declare: ?? Probe.csproj
EXIT=1
```

It caught a stray `Probe.csproj` **in the worktree**. It never saw the file in
the main tree, because `verify_build` inspects only the worktree.

The grunt also ran `dotnet build` in the main tree, and invented the `.csproj`
when that build failed.

### Three findings

**F1. `excludeTools` does not block `write_file`.** `.qwen/settings.json` on
disk excluded `write_file`, `replace`, `edit`, `save_memory`, `web_fetch`. The
pane used `WriteFile` four times. The original spec's claim — "`excludeTools` is
the only lock that actually holds" — is false. **No qwen configuration prevents a
grunt from writing.** `coreTools` is ignored; `excludeTools` is ignored for at
least `write_file`. Containment is the enforcement. There is no other.

**F2. The pane's cwd is qwen's tool sandbox root.** The W1 reasoning was correct
about the mechanism (qwen resolves its project root by git root, so a pane
launched in a worktree finds no `.qwen/settings.json` at the main root) and
picked the wrong horn. Leaving the pane in the main root makes every unqualified
tool path — `WriteFile`, `Shell` without a `cd` — address the main tree, which is
precisely the tree the containment check cannot see. The task file said `cd
<worktree>` and the grunt did prefix its *shell* calls with it; that discipline
does not extend to the file tools, which take no cwd.

**F3. `result_done` ran twice.** Observed order: `done` → `add` → `done`. The
first sealed and announced (message 014); `result_add` then re-created a staging
file for a sealed task; the second `done` was correctly refused as
`already sealed`. No corruption, but a sealed task accepted a new record — a lead
that has already run `verify` can have its evidence change underneath it.

### The fix

**A1 — the grunt pane's cwd is its worktree.** `git worktree add` runs before
`split-window`, and the pane is created with `-c <worktree>`. qwen's project root
is then the worktree, and there is no unqualified path that names the main tree.

**A2 — `.qwen/settings.json` is provisioned into each worktree.** This is what
made A1 look impossible. It is not: the settings file is written by
`worktree up`, *before* `send` snapshots the tree, so it lands in the
containment baseline and `verify` never blames the grunt for it. `down`'s
dirty-tree guard skips the same prefix. A grunt rewriting its own
`.team/work/<agent>/.qwen/` is inside its own fence and changes nothing outside
it; that hole is accepted and named here rather than papered over.

**A3 — `find` tasks move into the worktree too**, because the pane has one cwd.
A worktree is a detached checkout of `HEAD`, so a grunt now reads **committed**
code, while `verify` resolves citations against the main tree. Divergence would
produce a false `FABRICATED` — a loud, closed failure, not a silent wrong answer,
but still noise. So `send` refuses up front:

> `send --type find` runs `git status --porcelain -uall -- <scope paths>` in the
> main tree. Any output, and the task is refused (exit `3`) naming the dirty
> file. `--allow-dirty` opts out. With no `--scope`, there is nothing to check
> and nothing is refused.

This converts a stale read into a precondition the lead must clear, and it costs
one `git status` per dispatch.

**A4 — `verify_build` also checks the main tree, narrowly.** A new status
`ESCAPED`, checked *before* `CONTAINMENT`: none of the declared `--create` paths
may exist in the main tree. This is exactly the 013 failure and it has no false
positives, because `compose_build_task` now refuses at dispatch if a `--create`
path already exists in the main tree. `--replace` still deletes only the
worktree copy — a file in the lead's own tree is never deleted by this tool, and
`collect` would refuse to overwrite it anyway. Move it aside by hand.

It deliberately does **not** diff the whole main tree. The lead edits the main
tree while a grunt works; a general diff would fire on the lead's own work, and a
check that cries wolf is a check people pass `--lenient` to. A1 is the fence.
A4 is the tripwire on the one gate we watched a grunt walk through.

**A5 — the protocol stops lying.** `TEMPLATE` said "You have no write tools."
It is replaced by an instruction, not a claim of impossibility. `BUILD_TEMPLATE`
no longer tells the grunt to `cd` into its worktree — it is already there.

**A6 — `result_add` refuses on a sealed task**, so a task's evidence is
write-once in both directions.

### Two more defects, found by re-running the same task

The fix was verified by tearing the session down, rebuilding it under A1, and
re-sending task 013's exact question. The grunt used `WriteFile` again — and it
landed in the worktree. Main tree clean. `verify` said `PASS`.

**A7 — a build task's citations were never verified.** The grunt sealed
`probe/WaveTally.cs:7 WaveTally`. The symbol is on line **6**. `verify` printed
`PASS` because `cmd_verify`, on seeing a build task, checked containment and the
compiler and then returned — it never looked at the records. The spec claimed
those citations "verify as they do today"; nothing implemented it.

Now: on a build task that passes its task-level checks, sealed records are
verified against **the grunt's worktree**, and the exit code is the worse of the
two. Skipped when the task-level check already failed — `ESCAPED` beside a
green citation table invites the lead to read the pointer and miss the breach.

The compiler proves the code. Only `verify` proves the pointer. The same grunt,
in the same run, got the code exactly right and the line number wrong by one.

**A8 — `collect` framed the grunt.** `ESCAPED` reads "a declared file exists in
the main tree", and `collect`'s whole job is to put one there. Running
`team verify` after `team collect` accused the grunt of the lead's own copy.
`collect` now records what it moved into the snapshot, and `ESCAPED` skips those
paths — an uncollected declared file appearing in the main tree still fires.

### What this costs

- A `find` task on a file you have edited but not committed is refused. Commit,
  or pass `--allow-dirty` and read the citation with that in mind.
- Each worktree carries an untracked `.qwen/`. It is baselined, and `collect`
  never looks at it.
- `team up` must create worktrees before it creates panes. If `worktree up`
  fails, the panes fall back to the main root with a warning, and build tasks
  refuse (`no worktree for <agent>`) exactly as before.
