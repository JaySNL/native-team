"""Filesystem bus primitives. Knows nothing about tmux or schemas."""
import json
import os
import tempfile
from pathlib import Path

TEAM = ".team"


class BusError(Exception):
    pass


def repo_root(start: Path | None = None) -> Path:
    cur = (start or Path.cwd()).resolve()
    for cand in [cur, *cur.parents]:
        if (cand / ".git").exists():
            return cand
    raise BusError(f"not inside a git repository: {cur}")


def team_dir(root: Path) -> Path:
    return root / TEAM


def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_json(path: Path, obj: dict) -> None:
    atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def alloc_id(root: Path) -> str:
    ids = team_dir(root) / "ids"
    ids.mkdir(parents=True, exist_ok=True)
    taken = [int(p.name) for p in ids.iterdir() if p.name.isdigit()]
    n = max(taken, default=0) + 1
    while True:
        try:
            fd = os.open(ids / f"{n:03d}", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return f"{n:03d}"
        except FileExistsError:
            n += 1


def task_path(root: Path, agent: str, tid: str) -> Path:
    return team_dir(root) / "inbox" / agent / f"{tid}.json"


def lead_inbox(root: Path) -> Path:
    return team_dir(root) / "inbox" / "lead"


def result_path(root: Path, tid: str) -> Path:
    return team_dir(root) / "results" / f"{tid}.json"


def staging_path(root: Path, tid: str) -> Path:
    return team_dir(root) / "staging" / f"{tid}.json"


def dead_path(root: Path, tid: str) -> Path:
    return team_dir(root) / "dead" / tid


def mark_dead(root: Path, tid: str) -> None:
    p = dead_path(root, tid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def is_dead(root: Path, tid: str) -> bool:
    return dead_path(root, tid).exists()


def open_task(root: Path, agent: str) -> str | None:
    box = team_dir(root) / "inbox" / agent
    if not box.is_dir():
        return None
    for p in sorted(box.glob("*.json")):
        obj = read_json(p)
        if obj.get("kind") != "task":
            continue
        tid = obj["id"]
        if result_path(root, tid).exists() or is_dead(root, tid):
            continue
        return tid
    return None
