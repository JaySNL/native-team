"""Structured answers for the three verbs the lead's control flow depends on.

`cli` renders these as text and exit codes; `mcp_server` serialises them as
JSON. Both sit on this. The alternative -- an MCP server that re-implements
`cmd_verify`'s build/find branch -- would drift, and the drift would be silent:
two answers to *is this citation real?*, which is the one question this whole
tool exists to answer once.

Nothing here prints, and nothing here exits. Errors are raised as the exceptions
`cli.main` already maps to exit codes, so the CLI's behaviour is unchanged by
construction.
"""
from dataclasses import dataclass, field
from pathlib import Path

from team import bus, buildverify, ops, panes, verify, wait, worktrees
from team.config import StateError

DEFAULT_BUILD_CMD = ("dotnet", "build", "-v", "q", "--nologo")
DEFAULT_WAIT_TIMEOUT = 600.0


def roster(root: Path) -> dict:
    return bus.read_json(bus.roster_path(root))


def pane_for(root: Path, agent: str) -> str:
    entry = roster(root).get(agent)
    if not entry:
        raise StateError(f"no agent {agent!r} in roster.json")
    return entry["pane"]


@dataclass
class SendResult:
    kind: str          # "task" | "reply"
    id: str
    agent: str


@dataclass
class WaitResult:
    sealed: list[str] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)
    timed_out: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """A superseded task is resolved, not lost. Only a timeout is a miss."""
        return not self.timed_out


@dataclass
class VerifyResult:
    task: str
    kind: str                                  # "find" | "build"
    verdicts: list[verify.Verdict]
    build: buildverify.TaskVerdict | None = None

    @property
    def ok(self) -> bool:
        if self.build is not None and self.build.failed:
            return False
        return not verify.any_failed(self.verdicts)


def send(root: Path, agent: str, *, question: str = "", scope=(),
         supersede: bool = False, allow_dirty: bool = False,
         reply: str | None = None, text: str = "", kind: str = "find",
         create=(), replace: bool = False, build_dir: str = ".",
         build_cmd=None, p=None) -> SendResult:
    """Compose a task, clear the grunt's context, hand it the task path.

    Raises `panes.PaneError` if the grunt's pane is gone. The caller decides
    what that means: the CLI exits `PANE_GONE`, the MCP server returns isError.
    """
    p = p if p is not None else panes.Panes()
    pane = pane_for(root, agent)
    if not p.exists(pane):
        raise panes.PaneError(f"pane {pane} for {agent} is gone")
    # The pane exists, but a grunt spawned a moment ago may not be listening
    # yet, and keys typed before its TUI draws are dropped silently.
    p.wait_ready(pane)

    if reply:
        rid = ops.reply(root, agent, reply, text)
        p.send_line(pane, f"do task {bus.task_path(root, agent, rid)}")
        return SendResult("reply", rid, agent)

    if kind == "build":
        tid = ops.compose_build_task(
            root, agent, question, list(create), build_dir,
            list(build_cmd) if build_cmd else list(DEFAULT_BUILD_CMD),
            replace=replace)
    else:
        tid = ops.compose_task(root, agent, question, list(scope),
                               supersede=supersede, allow_dirty=allow_dirty)
    p.clear_context(pane)
    # ABSOLUTE. The grunt's cwd is its worktree, which has no `.team/` in it --
    # the bus lives once, in the main tree. A path relative to the main root
    # names nothing from where the grunt is standing. Measured live: the grunt
    # was handed `.team/inbox/grunt1/001.json`, could not open it, guessed the
    # absolute path, and the task died there.
    p.send_line(pane, f"do task {bus.task_path(root, agent, tid)}")
    return SendResult("task", tid, agent)


def wait_tasks(root: Path, tasks: list[str],
               timeout: float = DEFAULT_WAIT_TIMEOUT) -> WaitResult:
    sealed, missing = wait.for_tasks(root, tasks, timeout=timeout)
    # A superseded task is resolved but never seals. Reporting it as neither
    # sealed nor timed out left `team wait` printing nothing and exiting 0.
    superseded = [t for t in tasks if t not in sealed and t not in missing]
    return WaitResult(sealed=list(sealed), superseded=superseded,
                      timed_out=list(missing))


def verify_task(root: Path, task: str) -> VerifyResult:
    """Re-open every cited file and compare the cited line to the evidence.

    A build task's citations are checked against the worktree the grunt wrote
    them in, not the main tree. Measured on the first clean build run: the file
    was correct, it compiled, and the grunt cited line 7 of a symbol on line 6.
    The compiler proves the code. Only this proves the pointer.

    The citation pass is skipped when the task-level check already failed:
    there is no sound tree to resolve a path against.
    """
    if not buildverify.is_build_task(root, task):
        payload = bus.read_json(bus.result_path(root, task))
        return VerifyResult(task, "find",
                            verify.verify_records(root, payload["records"]))

    tv = buildverify.verify_build(root, task)
    verdicts: list[verify.Verdict] = []
    if not tv.failed:
        records = task_records(root, task)
        if records:
            agent = bus.read_json(bus.snapshot_path(root, task))["agent"]
            verdicts = verify.verify_records(worktrees.path(root, agent), records)
    return VerifyResult(task, "build", verdicts, build=tv)


def task_records(root: Path, task: str) -> list[dict]:
    """The citations a grunt sealed, or [] if it sealed none."""
    path = bus.result_path(root, task)
    if not path.exists():
        return []
    return bus.read_json(path).get("records") or []
