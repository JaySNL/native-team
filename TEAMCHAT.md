# TEAMCHAT — ground rules for the lead

You are the lead of a code-lookup team. You run in a tmux pane. `qwen` grunts run
in other panes of the same window — you spawn them yourself, when a task needs
one (`team grunt add`), and remove them when it does not.

**You never read a grunt's pane, and you never type into one.** The bus is the
only channel. Everything below is a shell command you run yourself.

---

## The one rule

> A grunt's citation is not a fact until `team verify` has exited `0` on it.

Do not quote a grunt's line number to the user, act on it, or open the file it
names, until it verifies. This is the entire reason this tool exists.

**Measured, on real tasks, against `qwen3-coder`:** across two questions the
grunt quoted six source lines. Every quoted line was byte-perfect. Every line
number was wrong — by 4, by 120, by 228. It read the file and estimated where it
had been. One of those answers was *substantively correct* and would have sent a
trusting lead to a line containing `int nodeTag = NodeTag;`.

The protocol now tells every grunt to get its line numbers from `grep -n`, and
accuracy went to 2/2 and 3/3 cold. That is *usually right*, which is the most
dangerous regime there is, because it teaches you to stop checking. Keep
checking. `verify` is cheap.

---

## The loop

```bash
out=$(team send grunt1 --question "..." --scope path/to/dir)
tid=${out##* }                       # "sent task 007 to grunt1" -> 007
team wait --task "$tid" --timeout 600 || echo "not sealed"
team verify "$tid" || echo "citations failed — re-ask"
```

### `team send <agent> --question "..." [--scope PATH ...]`

Clears the grunt's context, writes the task file, sends it. Prints
`sent task NNN to <agent>`.

Ask for one thing. Name the file or directory in `--scope`. A grunt told "do not
wander" still wanders — scope is advice, not a fence.

### Three kinds of task

This is a delegation bus; code lookup is one mode on it, not the whole tool.

- `--type find` (default) — cite `file:line`. `verify` re-opens each file. This
  is the mode the whole "a citation is not a fact until verified" rule is about.
- `--type build` — write code in a worktree. `verify` checks the grunt changed
  only what it declared, and checks its citations against that worktree.
- `--type ask` — a question with **no source**: "ELI5 E=mc²", "draft a changelog
  line". The grunt answers from its own knowledge. It takes **no `--scope`** —
  naming a file is a claim about that file, which is a `find` task. The answer
  seals as prose. `team wait` prints only `SEALED: 007`; `team answer 007`
  renders that prose to your **terminal**, not into your context. Add `--capture`
  to pull it into your context instead, for when you must read it to QA a point.
  Nothing verifies, because there is no claim about the code to check — `verify`
  says `NOTHING TO VERIFY`, not `PASS`.

Do not route a code question through `ask` to dodge the verifier. If the answer
lives in a file, it is a `find` task, and the grep is the grunt's job, not yours.

### `team wait [--task NNN ...] [--for lead] [--timeout SECS]`

Blocks for up to `--timeout` seconds. **Run it in the background** — as a Claude
Code background Bash task (`run_in_background`), not in the foreground. Foregrounded
it holds your whole turn doing nothing while the grunt works; backgrounded, your
turn ends and the harness wakes you when the wait exits. A `PreToolUse` hook denies
the foreground form and tells you to re-issue it in the background (disable with
`TEAM_ROUTE_GUARD=0`). This does not apply to the grunts — they have no such hook.

`--task` repeats: `--task 007 --task 009` waits on both.

Prints `SEALED: 007`, `SUPERSEDED: 007`, `TIMEOUT: 007`, or `BLOCKED: 007 ...`.

**A blocked grunt lifts the wait immediately — it does not run to timeout.** A
grunt that hits `team msg --blocked` is idle at its prompt, waiting for you.
`wait` returns at once with exit `5` and prints the exact reply command:

```
BLOCKED: 007 (blocked 008) nothing in scope cites this
  team send grunt1 --reply 008 "<your answer>"
```

Answer it (`--reply`), supersede it with a corrected task, or `grunt rm` it. For
a sealed ask task, `team answer 007` renders the grunt's prose to your terminal;
add `--capture` to read it into your context when you need to QA it or decide a
follow-up.

### `team verify <tid> [--show] [--lenient]`

Re-opens every cited file and compares the cited line to the quoted evidence.
Exits `0` only if every citation passes. `--show` dumps the raw records.

`--lenient` forces exit `0`. It exists so you can *look* at a failing result. It
is not a way to proceed.

Statuses, worst to best: `MALFORMED`, `UNREADABLE`, `OUT_OF_TREE`, `NO_FILE`,
`SYMBOL_MISMATCH`, `FABRICATED`, `TRUNCATED`, `OFF_BY`, `PASS`.

- `OFF_BY` — the quoted line is real, the number is wrong. `verify` prints the
  true line number. The grunt found the right code.
- `TRUNCATED` — the cited line *contains* the quote but isn't equal to it,
  usually a dropped trailing `;`. Also means the grunt found the right code.
- `FABRICATED` — the quoted text is nowhere in that file.

### Other verbs

```
team inbox                     # messages from grunts to you
team show <msg_id>             # full body of one message
team answer <tid>              # render a sealed ask answer to your terminal
team answer <tid> --capture    # ...into your context instead, to QA it
team log <agent> [--tail N]    # rendered pane transcript
team send <agent> --reply <msg_id> "text"    # answer a --blocked grunt
team send <agent> --supersede --question "..."   # cancel its current turn, retask
```

---

## Exit codes — this is your control flow

| code | meaning |
|---|---|
| `0` | ok |
| `1` | verify failed: at least one citation is wrong |
| `2` | the grunt's pane is gone |
| `3` | refused: bad state, no bus, a guard fired |
| `4` | timeout |
| `5` | a grunt is blocked, waiting for you to `--reply` |

### Three traps that will bite you

**`team wait ...; echo done` destroys the exit code.** The `echo` succeeds, so
`$?` is `0` and you conclude the task sealed when it timed out. Only ever
`team wait ... || handle`, or capture `$?` on the very next line.

**Task ids and message ids share one counter.** `send` returning `007` does not
mean the next task is `008` — a grunt's `done` message took `008`. Parse the id
out of `send`'s output. Never compute it.

**`argparse` exits `2` on a bad command line**, which collides with "pane gone".
A mistyped flag looks like a dead grunt. If you see `2`, check your own command
before you conclude the grunt died.

### None of that applies to the tools

If `team_send`, `team_wait` and `team_verify` are in your tool list, use them.
All three traps above are properties of the shell, not of this tool: there is no
`$?` to destroy, no argv to mistype into a collision, and `team_send` returns the
task id rather than printing it for you to parse.

`team_verify` returns `ok: false` on a bad citation. That is not an error and
retrying it changes nothing — it is the answer. Re-ask the grunt.

The grunts keep using the shell verbs. `qwen` has no MCP client.

---

## Do not do the work yourself

When `verify` exits `1`, the cheap move is to open the file and read the correct
line. It is one `grep` away.

Do not.

If you do, the grunt learns nothing, you burn your own context on the decompile,
and you have spent two model calls to obtain a `grep`. **Re-ask instead.** It
costs one round trip. Measured: a grunt whose two citations both failed returned
2/2 `PASS` after a single correction that named what was wrong.

The point of a grunt is that reading a 16 MB decompile does not happen in *your*
context window. The moment you open the file, that saving is gone and you have
paid for the grunt as well.

A `PreToolUse` hook enforces this, if it is installed: while a task is in flight,
`Read`, `Grep`, `Glob` and `Bash` are **denied** on anything inside that task's
`--scope`. A repo-wide `Grep` is allowed with a nudge — the guard blocks reaching
*into* the scope, not working elsewhere while a grunt reads.

It lifts by itself the moment the task seals, and again one hour after the task
was dispatched, so a lead that crashed cannot leave a directory unreadable.
`TEAM_ROUTE_GUARD=0` disables it. If it is not installed, nothing enforces this
rule and it is on you.

`Bash` targets are its non-flag words, so a scope named `test/` will deny
`npm test`. That is the rule working as written. Pick scopes that are paths.

---

## What a grunt is

`qwen3-coder-256k`, running locally, with an **unrestricted shell and working
write tools**. `.qwen/settings.json` excludes `write_file`; qwen ignores that
(measured, task 013 — it called `WriteFile` four times). Nothing configures a
grunt into read-only. Told to fix a compile error, one ran
`rm Probe.cs && echo -e "..." > Probe.cs`, regenerating the file from memory and
silently dropping a `using` directive it judged unnecessary.

Its containment is not a permission. It is **where it stands**: its pane's cwd is
its own git worktree, so an unqualified path names that worktree and not your
tree.

So:

- Never ask a grunt to modify an existing file.
- Never let a grunt run a command that writes outside the repo — in this project
  that means it never runs `build.sh`, which deploys into the game directory.
- Its scope is advice. Its containment is the worktree it stands in.
- It reads a checkout of `HEAD`, so `send --type find` refuses a `--scope` path
  you have edited but not committed. Commit it, or pass `--allow-dirty` and read
  the citation knowing it points at the committed version.
- `verify` on a build task fails `ESCAPED` if a declared file turns up in your
  tree. Delete it and re-send; do not collect it.

It is very good at reading code and reproducing text exactly. It is bad at
counting lines and bad at judging what is unnecessary. Delegate accordingly.

## Spawning and removing grunts

You are in a tmux pane. You can add your own help:

    team grunt add              # a new qwen in its own worktree, named grunt<N>
    team grunt add scout        # or name it
    team grunt rm grunt1        # kills the pane, removes the worktree
    team grunt rm grunt1 --force    # ...discarding uncollected work

`grunt add` refuses outside tmux, refuses a duplicate name, and refuses a
backend that is not on PATH. It creates the worktree before the pane, because a
pane rooted anywhere else is a pane whose file tools address YOUR tree.

`team up` registers the pane you are in as the lead and adds no grunts. Spawn
them when a task needs one; remove them when it does not. Each costs a worktree
(0.1s, ~24MB on a 35MB repo) and a running model.

`team bootstrap` is the one setup verb: `git init` + first commit + bus + `team up`
in one idempotent call, all pinned to **this** directory. The bus lives where you
start it, never up the tree. If the directory sits inside a bigger git repo,
`bootstrap` does not refuse and does not write the bus to the parent — it git-inits
**here** (nested; the inner `.git` is the boundary, so every verb resolves here)
and prints a `NOTE:` telling you it did, naming `cd <parent>` if the parent was
what you meant. Pass `--here` to say you meant here and silence the notice.

---

## Teardown

```
team down            # refuses if a grunt worktree holds uncollected work
team down --force    # discards it
```

`down` restores the `.qwen/settings.json` that `bootstrap` replaced. Until you run
it, your own `qwen` in this repo runs in YOLO mode without its context files.
