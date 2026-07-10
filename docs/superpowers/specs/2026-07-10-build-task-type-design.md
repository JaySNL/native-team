# The `build` task type — design

**Status:** proposed. Nothing below is implemented.

**Goal:** let a grunt produce *code* — not just citations — with the compiler as
the verifier and the tree as the containment boundary.

---

## Why this is not `--freeform`

The obvious extension is a task whose answer is prose: "explain X", "sketch an
approach". Rejected. `team`'s thesis is not "grunts return citations" — it is
**grunt output must be mechanically checkable**. Citations were the first thing
that was; they are not the only one.

Prose has no verifier. A `--freeform` task would make `team verify` sometimes
mean "I re-read the file and the line matches" and sometimes mean "I did
nothing," and the exit code would stop being a fact. For prose against two
local models, `ask` / `askc` / `llc` already exist and need no bus.

Code is checkable *better* than a citation: `verify` re-reads one line, but a
compiler reads every line.

---

## What was measured

Every claim here comes from the live dogfood session of 2026-07-10, against
`qwen3-coder-256k` in a tmux pane. Task ids are real.

| Obs | Task | Finding |
|-----|------|---------|
| 1 | 001, 005 | Six cold citations. **Every quoted source line byte-perfect. Every line number wrong** (−4, −120, +228). The grunt called its `Read` tool and estimated the position. |
| 2 | 007, 009 | Told to use `grep -n`, the same grunt on the same question cites exactly. 2/2 and 3/3 PASS, cold, no answers given. |
| 3 | 011 | Given a project with a deliberate `CS0246`, the grunt ran `dotnet build`, read its own compiler error, fixed the source, rebuilt until green, and cited the line it added. **No lead intervention.** |
| 4 | 011 | It has no write tools (`excludeTools`), so it wrote through the shell: first `sed -i`, then `rm Probe.cs && echo -e "..." > Probe.cs`. |
| 5 | 011 | The re-emitted file was byte-identical **except that `using System;` was silently deleted** — the model judged it unnecessary. |
| 6 | 011 | It stayed inside the declared directory. |

Observation 1 and 5 are the same fact seen twice: **these models reproduce text
faithfully and reason about position and necessity badly.** That is exactly the
profile of a good scaffolder and a catastrophic editor.

Observation 3 kills a design assumption I held before running it. I predicted
the lead would have to feed compiler errors back via `--reply`, making `--reply`
load-bearing. It doesn't. The grunt self-drives the build loop. `--reply` stays
out of this spec.

---

## The hard rule

> **A grunt never modifies an existing file.** It creates new ones.

Not a style preference. Observation 4 + 5: handed an existing file, the grunt's
repair strategy is `rm` followed by regeneration from memory, and regeneration
drops whatever it deems unnecessary. At 14 lines that is lossless. At 300 lines
it is a lossy re-emit whose deletions nobody reviews. It is also, precisely, the
project rule *"never remove a feature unprompted"* — broken by a model that has
never read that rule and never will.

So `send --type build` takes `--create` and has **no `--modify` flag**. If an
existing file must change, the grunt emits a new file and the lead applies the
change. Generation is delegated. Mutation is not.

---

## CLI surface

```
team send <agent> --type build \
    --question "<what to build>" \
    --create <path> [--create <path> ...] \
    [--build-dir <dir>] [--build-cmd <cmd>]
```

- `--type find` is the default and is today's behaviour, unchanged.
- `--create` declares every path the grunt may bring into existence. **Each must
  not exist at send time**; `send` refuses otherwise (exit `3`). This is what
  makes "never modify" enforceable rather than advisory.
- `--build-dir` defaults to the repo root. `--build-cmd` defaults to
  `dotnet build -v q --nologo`.
- `--scope` keeps its meaning: files the grunt should read.

`send` writes `.team/snapshots/<tid>.json` before announcing the task:

```json
{
  "task": "011",
  "create": ["probe/Probe.cs"],
  "build_dir": "probe",
  "build_cmd": "dotnet build -v q --nologo",
  "tree": ["?? probe/Probe.csproj", " M .gitignore"]
}
```

`tree` is the output of `git status --porcelain -uall`, sorted.

### `-uall` is not optional

While testing observation 6 I checked containment with `git status --porcelain`
and reported the grunt had stayed in bounds. That check could not have detected
a violation: plain `--porcelain` collapses an untracked directory to a single
`?? probe/` entry, so a grunt writing `probe/evil.sh` is invisible. Only
`-uall` enumerates the files. The claim was right; the evidence for it was not.

**Known hole, recorded not fixed:** `-uall` still omits gitignored paths, so a
write into `bin/`, `obj/`, or anything in `.gitignore` is not seen. Adding
`--ignored` would drown the manifest in build output. A grunt that wants to hide
a write can. This is a containment check against accident, not against malice —
same standing as the unrestricted `run_shell_command` risk already in the spec.

---

## `team verify <tid>` on a build task

Four checks, in order, first failure wins:

| Status | Check |
|---|---|
| `CONTAINMENT` | `git status --porcelain -uall` now, minus the snapshot's `tree`, must be a subset of `--create`. No snapshot entry may have changed state. |
| `NOT_CREATED` | every `--create` path exists and is a file |
| `BUILD_FAIL` | `--build-cmd` in `--build-dir` exits non-zero. First 20 lines of stderr are the detail. |
| `PASS` | all of the above |

If the grunt also ran `team result add`, those citations verify exactly as they
do today and are reported in the same table. A build task with zero citations is
legal; the build *is* the evidence.

Exit codes are unchanged — `0` ok, `1` verify failed, `2` pane gone, `3` refused,
`4` timeout. `CONTAINMENT` and `BUILD_FAIL` are both exit `1`. Resist the urge to
give containment its own code: the contract's value is that a lead can branch on
`1` meaning "the grunt's output is not trustworthy," without enumerating why.

### `--build-cmd` must not be `build.sh`

The project's `build.sh` deploys shared libraries into the game directory before
compiling. A grunt is a process with an unrestricted shell running unattended;
it does not get a command that writes outside the repo. The lead runs `build.sh`
after `verify` returns `0`.

---

## Isolation

Build tasks mutate the working tree. They must run against a **disposable git
worktree**, never a live checkout. The entire dogfood session ran in a detached
worktree for exactly this reason, and observation 4 retroactively justifies it.

This spec does **not** add worktree creation to `team up`. Creating and
destroying worktrees on the user's behalf is a bigger and more dangerous
decision than anything else here, and it deserves its own design. Until then,
`team init` prints a warning when it detects a non-detached HEAD, and the
operator sets up the worktree.

---

## Where this leaves the pipeline

`team` owns exactly one phase of building a mod, and it is the expensive one:

- **A — Lookup.** `--type find`, N grunts in parallel over `Decompiled/`. The
  lead gets verified `file:line` pointers and never opens the decompile. This is
  what the route-guard hook exists to enforce; here it holds by construction.
- **B — Design.** Opus, no grunts. Ranged-read the ~10 verified lines, choose the
  hook, grep every mod for an existing patcher. Not delegable.
- **C — Scaffold.** `--type build`. New files only. Compiler verifies.
- **D — Gate.** The lead runs `build.sh`, reviews the diff, ships.

The token saving is not that Opus stops reading the code — it still reviews
everything that ships. The saving is that Opus stops *doing the process*: the
forty tool calls, the whole-file reads, the compile-fix-recompile churn.

---

## Testing

`verify`'s build step takes an injected runner, exactly as `Panes` takes one, so
the unit tests never invoke `dotnet`. One end-to-end test compiles a real
14-line `netstandard2.0` project — the `probe/` fixture from task 011 — and is
skipped when `dotnet` is absent.

Every new branch gets a mutation check before it is called tested.

---

## Explicitly not in this spec

- `--freeform` prose tasks. See the top.
- `--modify`. See "The hard rule".
- `--reply`-driven compiler-error loops. Observation 3 says they are unnecessary;
  build the thing that is needed when it is needed.
- Worktree lifecycle management.
- Containment against a hostile grunt.
