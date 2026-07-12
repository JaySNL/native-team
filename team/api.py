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
import os
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


def _current_pane() -> str | None:
    """The tmux pane the caller runs in, or None outside tmux. Split out so a
    test can assert a pane identity without a real tmux."""
    return os.environ.get("TMUX_PANE")


def assert_own_bus(root: Path, pane: str | None = None) -> None:
    """Refuse to operate a bus that a DIFFERENT pane bootstrapped.

    Every project's bus is `.team`, so `bus_root()` picks by cwd/$TEAM_ROOT --
    position, not identity. A lead whose cwd or env drifted to another project
    would otherwise dispatch straight into it. Measured: a task meant for
    ~/teamTest was written to ~/Projects/IFZ-Modding/.team and run by its grunt.

    `team up` records the pane that ran it; the lead runs every later verb from
    that same pane. If the caller's pane differs, this bus is not theirs -- refuse
    rather than cross-contaminate. Dormant when there is nothing to compare:
    outside tmux (no pane), or a bus with no lead in its roster (hand-built, or
    torn down mid-flight).
    """
    pane = pane if pane is not None else _current_pane()
    if not pane:
        return
    try:
        lead = roster(root).get("lead") or {}
    except (OSError, ValueError):
        return
    lead_pane = lead.get("pane")
    if lead_pane and lead_pane != pane:
        raise StateError(
            f"refusing to cross-contaminate: the bus at {bus.team_dir(root)} was "
            f"started by lead pane {lead_pane} (at {lead.get('cwd')}), but you are "
            f"pane {pane}. This is not your bus. Run `team` from the project where "
            f"you did `team up`, or `team up` here first."
        )


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
    blocked: list[dict] = field(default_factory=list)
    # Prose from sealed ask tasks, keyed by task id. The lead renders this; it
    # does not go and read anything. A find task's records are NOT carried
    # here -- keeping the decompile out of the lead's context is the point.
    answers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """A superseded task is resolved, not lost. A blocked one is not: it is
        idle, waiting for the lead. Only a timeout and a block are misses."""
        return not self.timed_out and not self.blocked


@dataclass
class VerifyResult:
    task: str
    kind: str                                  # "find" | "build" | "ask"
    verdicts: list[verify.Verdict]
    build: buildverify.TaskVerdict | None = None
    answer: str | None = None

    @property
    def verifiable(self) -> bool:
        """An ask task carries no claim about any file. Nothing to re-open."""
        return self.kind != "ask"

    @property
    def ok(self) -> bool:
        if self.kind == "ask":
            return True                        # nothing failed; nothing passed
        if self.build is not None:
            return not self.build.failed and not verify.any_failed(self.verdicts)
        # Fail closed on an empty find. `any_failed([])` is False, so without
        # this a zero-citation seal would report a vacuous PASS -- and once ask
        # tasks can seal with no records, `result_done` is no longer the only
        # thing standing between a lead and a green light on nothing.
        if not self.verdicts:
            return False
        return not verify.any_failed(self.verdicts)


def answer(root: Path, task: str) -> str | None:
    """The prose a sealed ask task carries, or None."""
    path = bus.result_path(root, task)
    if not path.exists():
        return None
    return bus.read_json(path).get("answer")


def send(root: Path, agent: str, *, question: str = "", scope=(),
         supersede: bool = False, allow_dirty: bool = False,
         reply: str | None = None, text: str = "", kind: str = "find",
         create=(), replace: bool = False, build_dir: str = ".",
         build_cmd=None, attach_dir: str | None = None, p=None) -> SendResult:
    """Compose a task, clear the grunt's context, hand it the task path.

    Raises `panes.PaneError` if the grunt's pane is gone. The caller decides
    what that means: the CLI exits `PANE_GONE`, the MCP server returns isError.
    """
    # Before anything: this must be OUR bus. A lead whose cwd/env drifted to
    # another project resolves that project's `.team` and would dispatch into it.
    assert_own_bus(root)
    # Reject a malformed request before any pane side effect. An ask task with a
    # scope is a category error (a claim about a file is a find task), not a
    # thing to compose and dispatch.
    if kind == "ask" and scope:
        raise StateError(
            "an ask task takes no --scope. Naming a file is a claim about "
            "that file, and a claim is checkable: send it as --type find, "
            "where `verify` re-opens the file."
        )

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

    if kind == "ask":
        tid = ops.compose_ask_task(root, agent, question, supersede=supersede)
    elif kind == "build":
        tid = ops.compose_build_task(
            root, agent, question, list(create), build_dir,
            list(build_cmd) if build_cmd else list(DEFAULT_BUILD_CMD),
            replace=replace, attach_dir=attach_dir)
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
    sealed, missing, blocked = wait.for_tasks(root, tasks, timeout=timeout)
    stuck = {m["task"] for m in blocked}
    # A superseded task is resolved but never seals. Reporting it as neither
    # sealed nor timed out left `team wait` printing nothing and exiting 0.
    superseded = [t for t in tasks
                  if t not in sealed and t not in missing and t not in stuck]
    answers = {t: a for t in sealed if (a := answer(root, t))}
    return WaitResult(sealed=list(sealed), superseded=superseded,
                      timed_out=list(missing), blocked=blocked, answers=answers)


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
        if payload.get("kind") == "ask" or payload.get("answer") is not None:
            return VerifyResult(task, "ask", [], answer=payload.get("answer"))
        return VerifyResult(task, "find",
                            verify.verify_records(root, payload.get("records") or []))

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
