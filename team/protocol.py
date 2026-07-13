"""The contract that ships inside every task file.

`/clear` fires before each new task, so the grunt has no memory of the
protocol. Therefore the protocol travels with the task. The lead never
authors this text — `team send` embeds it — so the lead cannot forget it.
"""

TEMPLATE = """\
You are a grunt on a code-lookup team. Answer ONLY from files you actually open.

TASK {tid}
QUESTION:
{question}

SCOPE (open these; do not wander):
{scope}

HOW TO REPORT — you have no other channel. Prose is not a report.

For every finding, run exactly one command per citation:

    team result add --task {tid} --file <path> --line <n> \\
        --symbol <name> --evidence '<the exact source line, copied verbatim>'

GET EVERY LINE NUMBER FROM `grep -n`:

    grep -n '<pattern>' <file>

The number grep prints before the colon IS the line number. Never count lines
by eye in a file you have read -- measured: reading the file and estimating
gets the line wrong nearly every time, even when you quote the line correctly.

`--evidence` must be the FULL source line as it appears in the file, including
any trailing `;` or `{{`. Leading indentation is ignored; nothing else is. It is
re-read and compared. A guessed line number will be detected and rejected.
`--symbol` must appear inside `--evidence`.
`--file` should be relative to the repo root, e.g. `src/A.cs`.

When every citation is added:

    team result done --task {tid}

If you cannot proceed, do not guess and do not write a scratch file:

    team msg --blocked --task {tid} "your question here"

Do not create, edit, or delete any file. Your citations are the only output.
You are reading a checkout of the last commit, not anyone's edits since.
"""


ASK_TEMPLATE = """\
You are a grunt on a team. This task is a QUESTION, not a code lookup.

TASK {tid}
QUESTION:
{question}

Answer it from what you know. There is no scope, and nothing here is a claim
about any codebase: do NOT search the repo, do NOT open files, do NOT cite.
If the question cannot be answered without reading code, it was sent to you as
the wrong kind of task -- say so and stop:

    team msg --blocked --task {tid} "this needs code; re-send as --type find"

HOW TO REPORT -- your whole deliverable is ONE file. Write it, then STOP.

    Write your full answer to this exact path:
        {answer_path}

Then you are done. Do NOT type the answer into a shell (a quote or a newline
would silently truncate it) and do NOT run any seal command -- there is none to
run. Your lead reads that file and seals it for you. The moment the file exists
and is complete, the task is finished: stop and wait for the next one.

That path is a new file in your own worktree. Creating it is allowed; that is
what it is for. Do not modify any file that was already there.
"""


FREE_TEMPLATE = """\
You are a grunt on a team. This task is open-ended work, described below. Unlike a
find/build/ask task it has no fixed shape and no fence: do whatever the task says --
read, edit, create, run commands, git, publish, whatever it takes. There is no --scope
and no --create list. The task text is your whole brief and your authority.

TASK {tid}
WHAT TO DO:
{question}

HOW TO REPORT -- the bus is your only channel back to the lead.

When the work is genuinely done, seal the task exactly once:

    team result done --task {tid}

If you cannot proceed and need the lead, do NOT guess and do NOT go silent:

    team msg --blocked --task {tid} "exactly what you need"

If the work failed outright:

    team msg --failed --task {tid} "what failed"

You may post a one-line status at any point -- this does NOT seal the task:

    team msg --note --task {tid} "..."

The lead is blocked on `team wait --task {tid}` until you seal or signal. This task
type is transport, not a contract: how the work is shaped was the lead's call, not
this protocol's. Do what the brief says.
"""


BUILD_TEMPLATE = """\
You are a grunt on a code-writing team. Work ONLY inside your worktree.

TASK {tid}
WHAT TO BUILD:
{question}

YOUR WORKTREE -- you are already in it. Do not cd out of it:

    {workdir}

CREATE EXACTLY THESE FILES, and no others:
{create}

You may READ anything. You may create only the files listed above. Do NOT
modify or delete any file that already exists -- not one line. If a file you
need to change already exists, stop and say so with `team msg --blocked`.

BUILD IT until it compiles:

    cd {build_dir} && {build_cmd}

Read the compiler errors and fix your own files until the build succeeds. Do
not edit the project file. Do not run any other build script.

WHEN THE BUILD SUCCEEDS:

    team result done --task {tid}

If you want to point at a specific line you wrote, add citations first -- the
line number must come from `grep -n`, and the evidence must be the full line:

    team result add --task {tid} --file <path> --line <n> \\
        --symbol <name> --evidence '<the exact source line>'

If you cannot proceed, do not guess and do not touch anything outside your
files:

    team msg --blocked --task {tid} "your question here"

Everything you changed is checked against the list above. Touching anything
else fails the task.
"""


BUILD_ATTACH_TEMPLATE = """\
You are a grunt on a code-writing team. Work ONLY inside your worktree.

TASK {tid}
WHAT THIS IS: {question}

YOUR WORKTREE -- you are already in it. Do not cd out of it:

    {workdir}

The exact, authoritative bytes for every file below are ALREADY staged in your
worktree under `.attach/`, mirroring these paths:
{create}

DO NOT type these files yourself, do NOT open them in an editor, do NOT
"improve" or reformat them, do NOT regenerate them from what you think a file
like this should contain. Your only job is to copy the staged bytes into place,
byte for byte. For each path P listed above, run:

    mkdir -p "$(dirname "P")" && cp ".attach/P" "P"

Then CHECK it compiles -- this is a check, not a licence to edit:

    cd {build_dir} && {build_cmd}

The staged bytes are authoritative. If the build fails, do NOT change the files
to make it pass -- that would corrupt the exact content you were given. Stop and
report the error instead:

    team msg --blocked --task {tid} "build failed after verbatim copy: <error>"

WHEN THE BUILD SUCCEEDS:

    team result done --task {tid}

Everything you changed is checked against the path list above. The files must
match the staged bytes exactly; copying anything else, or editing after the
copy, fails the task.
"""


def task_body(tid: str, question: str, scope: list[str]) -> str:
    scope_text = "\n".join(f"  - {s}" for s in scope) or "  (none given)"
    return TEMPLATE.format(tid=tid, question=question.strip(), scope=scope_text)


def ask_body(tid: str, question: str, answer_path: str) -> str:
    return ASK_TEMPLATE.format(tid=tid, question=question.strip(),
                               answer_path=answer_path)


def free_body(tid: str, question: str) -> str:
    return FREE_TEMPLATE.format(tid=tid, question=question.strip())


def build_body(tid: str, question: str, workdir: str, create: list[str],
               build_dir: str, build_cmd: list[str], attach: bool = False) -> str:
    import shlex
    create_text = "\n".join(f"  - {c}" for c in create)
    template = BUILD_ATTACH_TEMPLATE if attach else BUILD_TEMPLATE
    return template.format(
        tid=tid, question=question.strip(), workdir=workdir,
        create=create_text, build_dir=build_dir,
        build_cmd=" ".join(shlex.quote(a) for a in build_cmd),
    )
