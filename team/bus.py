"""Filesystem bus primitives. Knows nothing about tmux or schemas."""
import json
import os
import re
import tempfile
from pathlib import Path

TEAM = ".team"
ID_RE = re.compile(r"[0-9]{3}")

# A named bus is `.team-<slug>`; the plain `.team` is the default, unnamed one.
# `BUS_SLUG_RE` validates the `<slug>` a `--bus` flag carries; `BUS_DIR_RE`
# validates a whole directory name (`.team` or `.team-<slug>`), which is what
# `$TEAM_BUS` holds and what a bus-named ancestor is recognised by.
BUS_SLUG_RE = re.compile(r"[A-Za-z0-9._-]+")
BUS_DIR_RE = re.compile(r"\.team(-[A-Za-z0-9._-]+)?")


class BusError(Exception):
    pass


def resolve_bus_name(cli_flag: str | None = None, start: Path | None = None) -> str:
    """The bus directory name to address, by first match:

      a. an explicit `--bus` slug: '' or 'default' -> '.team', else '.team-<slug>'
      b. `$TEAM_BUS`, which holds a full dir name (e.g. '.team-auth'), verbatim
      c. the nearest ancestor of `start`/cwd whose own basename is a bus name --
         this is what lets a grunt pane, whose cwd is `.team-auth/work/<agent>`,
         find its own bus without being told which one it is
      d. '.team'

    With neither a flag nor `$TEAM_BUS` and no bus-named ancestor, the answer is
    exactly '.team' -- the single-team default, byte-for-byte as it always was.

    The flag wins over the env var so a lead can override an exported `$TEAM_BUS`
    for one command; the env var wins over the walk-up so an explicit choice is
    never second-guessed by where the shell happens to sit.
    """
    if cli_flag is not None:
        slug = cli_flag.strip()
        if slug in ("", "default"):
            return TEAM
        if not BUS_SLUG_RE.fullmatch(slug):
            raise BusError(
                f"invalid --bus slug {slug!r}: use letters, digits, '.', '_', '-'"
            )
        return f"{TEAM}-{slug}"

    env = os.environ.get("TEAM_BUS")
    if env:
        env = env.strip()
        if not BUS_DIR_RE.fullmatch(env):
            raise BusError(
                f"invalid $TEAM_BUS {env!r}: expected '.team' or '.team-<slug>'"
            )
        return env

    cur = (start or Path.cwd()).resolve()
    for cand in [cur, *cur.parents]:
        if BUS_DIR_RE.fullmatch(cand.name):
            return cand.name

    return TEAM


def repo_root(start: Path | None = None) -> Path:
    """The enclosing git repository. Used only by `init` and `down`, which run
    before the bus exists or while destroying it."""
    cur = (start or Path.cwd()).resolve()
    for cand in [cur, *cur.parents]:
        if (cand / ".git").exists():
            return cand
    raise BusError(f"not inside a git repository: {cur}")


def bus_root(start: Path | None = None) -> Path:
    """The directory holding the bus. Every verb but `init`/`down` wants this.

    Not `repo_root`. A grunt working inside a git worktree -- `<busdir>/work/<agent>`,
    where a build task runs -- would have `repo_root` stop at the worktree's own
    `.git` *file* and report the worktree as the repo. Its `team result add`
    would then address a bus that does not exist. Walking up for the resolved bus
    dir instead finds the one real bus, and terminates correctly even from inside
    `<busdir>/work/<agent>`, since none of `<agent>`, `work`, or the bus dir
    contains a bus dir of its own.

    Which bus is picked comes from `resolve_bus_name` (a `--bus` flag lives in
    `$TEAM_BUS` by the time we get here). With no flag and no env the resolved
    name is '.team', so this is byte-for-byte the old single-team walk-up.
    """
    cur = (start or Path.cwd()).resolve()
    name = resolve_bus_name(start=cur)
    for cand in [cur, *cur.parents]:
        if (cand / name).is_dir():
            return cand
    raise BusError(
        f"no {name}/ bus found in {cur} or any parent. Run `team init` first."
    )


def team_dir(root: Path, name: str | None = None) -> Path:
    return root / (name or resolve_bus_name())


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


def _try_read_obj(path: Path) -> dict | None:
    """Try to read a JSON object from a file, returning None if read or parse fails.

    Returns None if:
    - File cannot be read (OSError, UnicodeDecodeError)
    - JSON parsing fails (JSONDecodeError)
    - JSON parses but is not a dict (e.g., array, scalar)
    """
    try:
        obj = read_json(path)
        # Only return if it's actually a dict (object), not a list or other JSON type
        if isinstance(obj, dict):
            return obj
        return None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def alloc_id(root: Path) -> str:
    ids = team_dir(root) / "ids"
    ids.mkdir(parents=True, exist_ok=True)
    taken = [int(p.name) for p in ids.iterdir() if ID_RE.fullmatch(p.name)]
    n = max(taken, default=0) + 1
    if n > 999:
        raise BusError("task id space exhausted (max 999 per bus); run `team down` and `team init`")
    while True:
        try:
            fd = os.open(ids / f"{n:03d}", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return f"{n:03d}"
        except FileExistsError:
            n += 1
            if n > 999:
                raise BusError("task id space exhausted (max 999 per bus); run `team down` and `team init`")


def task_path(root: Path, agent: str, tid: str) -> Path:
    return team_dir(root) / "inbox" / agent / f"{tid}.json"


def lead_inbox(root: Path) -> Path:
    return team_dir(root) / "inbox" / "lead"


def result_path(root: Path, tid: str) -> Path:
    return team_dir(root) / "results" / f"{tid}.json"


def staging_path(root: Path, tid: str) -> Path:
    return team_dir(root) / "staging" / f"{tid}.json"


def roster_path(root: Path) -> Path:
    return team_dir(root) / "roster.json"


def snapshot_path(root: Path, tid: str) -> Path:
    """Where `send --type build` records what a task was allowed to create.

    Written before the task is announced, so it is the lead's own statement of
    intent, not the grunt's report of what it did. `verify` and `collect` both
    read it; neither trusts the worktree to describe itself.
    """
    return team_dir(root) / "snapshots" / f"{tid}.json"


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
        obj = _try_read_obj(p)
        if obj is None:
            continue
        if obj.get("kind") != "task":
            continue
        # Derive task id from filename (authoritative), not from embedded obj["id"]
        tid = p.stem
        # Validate that the id matches the required format (zero-padded 3-digit)
        if not ID_RE.fullmatch(tid):
            continue
        if result_path(root, tid).exists() or is_dead(root, tid):
            continue
        return tid
    return None
