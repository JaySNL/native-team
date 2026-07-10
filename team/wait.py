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


def _resolved(root: Path, tid: str) -> bool:
    return bus.result_path(root, tid).exists() or bus.is_dead(root, tid)


def for_tasks(root: Path, tids: list[str], timeout: float, poll: float = POLL,
              now=time.monotonic, sleep=time.sleep) -> tuple[list[str], list[str]]:
    deadline = now() + timeout
    while True:
        pending = [t for t in tids if not _resolved(root, t)]
        if not pending or now() >= deadline:
            break
        sleep(poll)
    sealed = [t for t in tids if bus.result_path(root, t).exists()]
    missing = [t for t in tids if not _resolved(root, t)]
    return sealed, missing
