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

`--evidence` must be the FULL source line as it appears in the file. It is
re-read and compared. A guessed line number will be detected and rejected.
`--symbol` must appear inside `--evidence`.

When every citation is added:

    team result done --task {tid}

If you cannot proceed, do not guess and do not write a scratch file:

    team msg --blocked --task {tid} "your question here"

Do not edit any file. You have no write tools.
"""


def task_body(tid: str, question: str, scope: list[str]) -> str:
    scope_text = "\n".join(f"  - {s}" for s in scope) or "  (none given)"
    return TEMPLATE.format(tid=tid, question=question.strip(), scope=scope_text)
