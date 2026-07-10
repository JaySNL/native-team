"""Verify a build task. The compiler is the verifier; the worktree is the fence.

A citation is checked by re-reading one line. Code is checked by compiling every
line of it -- so a build task's evidence *is* the build. What the compiler cannot
tell you is whether the grunt stayed where it was told, and that is the other
half of this module.

Task-level, not record-level. `verify.Verdict` describes one citation; nothing
here describes a citation, and hanging these statuses on a fabricated record
would be a lie in the data model.
"""
from dataclasses import dataclass
from pathlib import Path

from team import bus, worktrees

STATUSES = ("PASS", "ESCAPED", "CONTAINMENT", "NOT_CREATED", "BUILD_FAIL",
            "NO_WORKTREE")

MAX_DETAIL_LINES = 20


@dataclass
class TaskVerdict:
    task: str
    status: str
    detail: str

    @property
    def failed(self) -> bool:
        return self.status != "PASS"


def is_build_task(root: Path, tid: str) -> bool:
    return bus.snapshot_path(root, tid).is_file()


def _unexpected(before: list[str], now: list[str], allowed: set[str]) -> list[str]:
    """Porcelain lines present now, absent before, and not the files the grunt
    was told to create.

    Compares whole porcelain lines, so a file that was untracked before and is
    modified now reads as a change, not as the same entry.
    """
    baseline = set(before)
    out = []
    for line in now:
        if line in baseline:
            continue
        rel = worktrees.porcelain_rel(line)
        if rel not in allowed:
            out.append(line)
    return out


def _escaped(root: Path, created: list[str]) -> list[str]:
    """Declared files that turned up in the MAIN tree.

    Measured, task 013: qwen's `WriteFile` resolves against its project root and
    takes no cwd, so a pane rooted in the main tree wrote the declared file
    there. `compose_build_task` refuses to dispatch if the path already exists
    in the main tree, so anything found here appeared during the task.

    Deliberately not a full main-tree diff. The lead edits the main tree while
    the grunt works; a general diff would fire on the lead's own work every run,
    and a check that cries wolf gets `--lenient`'d. The pane's cwd is the fence.
    This is the tripwire on the one gate a grunt was measured walking through.
    """
    return [rel for rel in created if (root / rel).exists()]


def verify_build(root: Path, tid: str, wt=None) -> TaskVerdict:
    wt = wt if wt is not None else worktrees.Worktrees()
    snap = bus.read_json(bus.snapshot_path(root, tid))
    agent = snap["agent"]
    created = list(snap["create"])

    # Before NO_WORKTREE: a grunt that wrote into the main tree because it had
    # no worktree to write into is exactly the failure worth naming.
    escaped = _escaped(root, created)
    if escaped:
        return TaskVerdict(tid, "ESCAPED",
                           f"{agent} wrote outside its worktree, into the main "
                           f"tree: {', '.join(escaped)}. Delete those, then "
                           f"re-send.")

    work = worktrees.path(root, agent)
    if not work.is_dir():
        return TaskVerdict(tid, "NO_WORKTREE", f"no worktree for {agent!r}")

    now = sorted(wt.dirty(root, agent))
    unexpected = _unexpected(snap.get("tree", []), now, set(created))
    if unexpected:
        shown = ", ".join(unexpected[:5])
        more = f" (+{len(unexpected) - 5} more)" if len(unexpected) > 5 else ""
        return TaskVerdict(tid, "CONTAINMENT",
                           f"{agent} changed files it did not declare: {shown}{more}")

    missing = [rel for rel in created if not (work / rel).is_file()]
    if missing:
        return TaskVerdict(tid, "NOT_CREATED",
                           f"declared but never created: {', '.join(missing)}")

    rc, out = wt.build(root, agent, snap.get("build_dir", "."),
                       list(snap["build_cmd"]))
    if rc != 0:
        lines = [l for l in out.splitlines() if l.strip()][:MAX_DETAIL_LINES]
        return TaskVerdict(tid, "BUILD_FAIL",
                           f"exit {rc}\n" + "\n".join(f"    {l}" for l in lines))

    return TaskVerdict(tid, "PASS", f"{len(created)} file(s) created; build succeeded")


def render(v: TaskVerdict) -> str:
    head = f"build {v.task}: {v.status}"
    return f"{head} — {v.detail}" if v.detail else head
