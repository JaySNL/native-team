---
name: grunt
description: Use when the user types /grunt, or when a task is tagged "GRUNT TASK", or when the user asks to hand work to a grunt / local model. The tag is a ROUTING instruction, not a question to answer — dispatch it to a grunt and render the grunt's output to the terminal. Never do the work yourself on Opus, never WebSearch/produce the answer, never pull the grunt's output into your own context.
---

# /grunt

`/grunt "<task>"` hands one task to a grunt and renders the grunt's output to the
user's **terminal** — not into your context window.

A "GRUNT TASK" tag is a **routing instruction**. It is not a question for you to
answer. The moment you read the verb ("explain", "write", "find") and start
producing an answer on Opus — WebSearch, drafting, narrating — you have failed
the task. Classify by the tag *before* you touch a tool. The one correct first
question is **"who is this task FOR?"** — the grunt. Dispatch it.

## Why this exists

The whole point of a grunt is that expensive work does not happen in *your*
context window. If you answer it yourself you have spent Opus tokens to do a
grunt's job, and burned context you cannot get back. The grunt runs free; its
output goes to the human's terminal, and you never ingest it.

## Do this, in order

**1. Pick the backend by what's available.**

```bash
# native-team bus up here (you are the lead)?
test -d "$(team brief >/dev/null 2>&1 && dirname "$(team brief 2>/dev/null)")/.team" 2>/dev/null \
  || ls -d ./.team >/dev/null 2>&1 && echo bus || echo nobus
```

- **Bus up** (you ran `/teamup`, a `.team/` exists) → dispatch through the bus
  (step 2a). This reuses `verify`, so a code citation is checked before you
  trust it.
- **No bus** (plain chat) and `claude-local` is on PATH → dispatch to the local
  model (step 2b). Zero Anthropic tokens.
- **Neither** → tell the user: no grunt backend. Offer `/teamup` to start one.

**2a. Bus path — send, wait, render.**

```bash
# spawn a grunt if the roster has none
python -c "import json,sys; r=json.load(open('.team/roster.json')); sys.exit(0 if any(k!='lead' for k in r) else 1)" \
  || team grunt add

# type by task shape: build = write code, find = cite file:line, ask = prose
out=$(team send grunt1 --type <build|find|ask> --question "<task>")
tid=${out##* }                                    # "sent task 007 ..." -> 007
team wait --task "$tid" --timeout 600 || echo "not sealed"

# render to the user's terminal — NOT into your context:
#   find/build -> verify prints PASS/FAIL + true line numbers (small, safe to show)
#   ask        -> answer renders the prose to /dev/tty
case "<kind>" in
  ask) team answer "$tid" ;;
  *)   team verify "$tid" || echo "citations failed — re-ask" ;;
esac
```

A grunt's citation is **not a fact until `team verify` exits `0`**. Do not quote
its line number, act on it, or open the file it names, until it verifies. If
verify fails, re-ask the grunt — do not open the file and fix it yourself.

**2b. Local path — run, render, report.**

Run the grunt with output to a file, then render that file to the terminal. The
redirect is the whole trick: the run's stdout goes to a file, the file goes to
`/dev/tty`, so **your** Bash tool result stays empty and you never ingest it.

```bash
OUT="<scratchpad>/grunt-out.md"
claude-local -p "<task>" > "$OUT" 2>&1        # grunt does the whole task, local, free
cat "$OUT" > /dev/tty                          # human sees it; you do NOT
```

If `/dev/tty` is not available (some harnesses capture it), the render lands in
your context instead — that is the failure mode this avoids, so say so and stop
rather than silently ingesting a huge blob.

**3. Report one line.** State what ran, where the output went, and (bus path)
whether it verified. Do **not** re-type the grunt's output into your reply.

## Capture (QA only)

Default is no-ingest. Only when the user must have you *reason about* the output
— QA a specific point, decide a follow-up — read the file (`2b`) or run
`team answer <tid> --capture` (bus). That is the exception, not the loop.
