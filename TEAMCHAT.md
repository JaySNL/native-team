# TEAMCHAT — ground rules for the lead

You are the lead of a code-lookup team. You run in tmux pane 0. Two or three
`qwen` grunts run in the other panes.

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

### `team wait [--task NNN ...] [--for lead] [--timeout SECS]`

Blocks. `--task` repeats: `--task 007 --task 009` waits on both.

Prints `SEALED: 007`, `SUPERSEDED: 007`, or `TIMEOUT: 007`.

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

Nothing enforces this rule. It is on you.

---

## What a grunt is

`qwen3-coder-256k`, running locally, with no write tools and an **unrestricted
shell**. It can therefore write files, and has: told to fix a compile error, one
ran `rm Probe.cs && echo -e "..." > Probe.cs`, regenerating the file from memory
and silently dropping a `using` directive it judged unnecessary.

So:

- Never ask a grunt to modify an existing file.
- Never let a grunt run a command that writes outside the repo — in this project
  that means it never runs `build.sh`, which deploys into the game directory.
- Its scope is advice. Its containment is a git worktree at `.team/work/<agent>`,
  and only for build tasks.

It is very good at reading code and reproducing text exactly. It is bad at
counting lines and bad at judging what is unnecessary. Delegate accordingly.

---

## Teardown

```
team down            # refuses if a grunt worktree holds uncollected work
team down --force    # discards it
```

`down` restores the `.qwen/settings.json` that `init` replaced. Until you run
it, your own `qwen` in this repo runs in YOLO mode without its context files.
