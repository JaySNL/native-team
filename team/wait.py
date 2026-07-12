"""Blocking waits. The lead backgrounds these; their exit is the wake signal.

Polling, not inotify: turns take tens of seconds, and a stdlib-only poll
loop has no dependency and no partial-read hazard (all writes are atomic).
"""
import time
from pathlib import Path

from team import bus

POLL = 0.25


def _lead_files(root: Path) -> set[str]:
    box = bus.lead_inbox(root)
    return {p.name for p in box.glob("*.json")} if box.is_dir() else set()


def for_lead(root: Path, timeout: float, poll: float = POLL,
             now=time.monotonic, sleep=time.sleep) -> list[dict]:
    before = _lead_files(root)
    deadline = now() + timeout
    while now() < deadline:
        sleep(poll)
        fresh = sorted(_lead_files(root) - before)
        if fresh:
            return [bus.read_json(bus.lead_inbox(root) / name) for name in fresh]
    return []


STUCK = ("blocked", "failed")


def blocker(root: Path, tid: str) -> dict | None:
    """The message that says `tid` cannot proceed without the lead.

    A blocked grunt is idle at its prompt, waiting for a reply that will never
    come while the lead sleeps in `for_tasks`. Measured: a grunt with nothing to
    cite posted `--blocked` after four seconds, and its lead sat out the full
    600s timeout with the answer already sitting in its inbox.
    """
    box = bus.lead_inbox(root)
    if not box.is_dir():
        return None
    for path in sorted(box.glob("*.json")):
        msg = bus._try_read_obj(path)
        if msg and msg.get("task") == tid and msg.get("type") in STUCK:
            return msg
    return None


ANSWER_FILE = "ANSWER.md"


def _reap_answer(root: Path, tid: str, seen: dict) -> bool:
    """Seal an ask grunt's written answer when the grunt itself never did.

    An ask grunt's whole deliverable is ANSWER.md in its worktree; running a
    seal command afterward is a tail step a 30B grunt drops. Measured: it wrote
    the file, went idle, and qwen's rotating ghost placeholder showed an unrun
    `team result answer` in the input line -- which reads exactly like a command
    it ran and is not. With no lead-side reap the task then sat unsealed and the
    lead's `team wait` blocked to timeout. So the lead seals the file directly.

    `seen` carries each answer file's mtime across poll ticks: a file is reaped
    only once its mtime has held steady from the previous tick, so a half-written
    answer is never sealed mid-write. Everything here is best-effort and fails
    closed -- a reap that cannot run must never break the wait it rides inside,
    so any error just leaves the task pending for the next tick or the timeout.
    """
    if bus.result_path(root, tid).exists() or bus.is_dead(root, tid):
        return False
    try:
        from team import ops, worktrees
        tf = bus.find_task_file(root, tid)
        if tf is None:
            return False
        obj = bus._try_read_obj(tf)
        if not obj or obj.get("kind") != "ask":
            return False
        agent = obj.get("to")
        if not agent:
            return False
        # Primary: the exact path the task told the grunt to write. Fallback:
        # the agent inbox, where a grunt that misread "your worktree" has been
        # seen to drop the file instead. First one that exists wins.
        cands = [worktrees.path(root, agent) / ANSWER_FILE,
                 bus.team_dir(root) / "inbox" / agent / ANSWER_FILE]
        for ans in cands:
            try:
                st = ans.stat()
            except OSError:
                continue
            if st.st_size == 0:
                continue
            key = f"{tid}:{ans}"
            prev = seen.get(key)
            seen[key] = st.st_mtime
            if prev != st.st_mtime:
                return False              # first sighting, or still writing: wait a tick
            text = ans.read_text(encoding="utf-8", errors="replace")
            if not text.strip():
                return False
            ops.result_answer(root, tid, text, agent)
            return True
    except Exception:
        # Already sealed (a grunt that DID seal raced us), a corrupt task file,
        # a torn read -- none of it is the wait's problem. Report only whether a
        # result now exists on disk, and let the loop re-evaluate.
        return bus.result_path(root, tid).exists()
    return False


def _resolved(root: Path, tid: str) -> bool:
    return (bus.result_path(root, tid).exists() or bus.is_dead(root, tid)
            or blocker(root, tid) is not None)


def for_tasks(root: Path, tids: list[str], timeout: float, poll: float = POLL,
              now=time.monotonic, sleep=time.sleep
              ) -> tuple[list[str], list[str], list[dict]]:
    """(sealed, missing, blocked). `blocked` holds the messages themselves,
    because the lead needs the message id to reply to it."""
    deadline = now() + timeout
    seen: dict = {}
    while True:
        # The lead seals any ask answer whose grunt wrote the file but never ran
        # a seal (see `_reap_answer`). Done before the pending check so a reap
        # this tick lets the loop exit on the same tick, not one poll later.
        for t in tids:
            _reap_answer(root, t, seen)
        pending = [t for t in tids if not _resolved(root, t)]
        if not pending or now() >= deadline:
            break
        sleep(poll)
    sealed = [t for t in tids if bus.result_path(root, t).exists()]
    blocked = [m for m in (blocker(root, t) for t in tids
                           if not bus.result_path(root, t).exists()) if m]
    missing = [t for t in tids if not _resolved(root, t)]
    return sealed, missing, blocked
