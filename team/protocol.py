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

HOW TO REPORT -- write the answer to a file, then hand over the file.
Your answer is prose. Prose does not survive being typed into a shell as an
argument: a quote or a newline will silently truncate it.

    1. Write your full answer to ANSWER.md in the current directory.
    2. team result answer --task {tid} --from ANSWER.md
    3. team result done --task {tid}

ANSWER.md is a new file in your own worktree. Creating it is allowed; that is
what it is for. Do not modify any file that was already there.
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


def task_body(tid: str, question: str, scope: list[str]) -> str:
    scope_text = "\n".join(f"  - {s}" for s in scope) or "  (none given)"
    return TEMPLATE.format(tid=tid, question=question.strip(), scope=scope_text)


def ask_body(tid: str, question: str) -> str:
    return ASK_TEMPLATE.format(tid=tid, question=question.strip())


def build_body(tid: str, question: str, workdir: str, create: list[str],
               build_dir: str, build_cmd: list[str]) -> str:
    import shlex
    create_text = "\n".join(f"  - {c}" for c in create)
    return BUILD_TEMPLATE.format(
        tid=tid, question=question.strip(), workdir=workdir,
        create=create_text, build_dir=build_dir,
        build_cmd=" ".join(shlex.quote(a) for a in build_cmd),
    )
