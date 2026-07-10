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


def _resolved(root: Path, tid: str) -> bool:
    return (bus.result_path(root, tid).exists() or bus.is_dead(root, tid)
            or blocker(root, tid) is not None)


def for_tasks(root: Path, tids: list[str], timeout: float, poll: float = POLL,
              now=time.monotonic, sleep=time.sleep
              ) -> tuple[list[str], list[str], list[dict]]:
    """(sealed, missing, blocked). `blocked` holds the messages themselves,
    because the lead needs the message id to reply to it."""
    deadline = now() + timeout
    while True:
        pending = [t for t in tids if not _resolved(root, t)]
        if not pending or now() >= deadline:
            break
        sleep(poll)
    sealed = [t for t in tids if bus.result_path(root, t).exists()]
    blocked = [m for m in (blocker(root, t) for t in tids
                           if not bus.result_path(root, t).exists()) if m]
    missing = [t for t in tids if not _resolved(root, t)]
    return sealed, missing, blocked
