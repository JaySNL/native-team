"""Move a build task's output out of the grunt's worktree into the main tree.

A grunt's created files live in `.team/work/<agent>`, untracked, on a detached
HEAD. Nothing in there is a commit and nothing about it should reach the main
tree except the files the lead named in `--create`.

So this is a copy of an explicit list, not a merge, not a patch apply, and not
a directory walk. It refuses on any surprise rather than resolving it.
"""
import shutil
from pathlib import Path

from team import bus, worktrees
from team.config import StateError


def _snapshot(root: Path, tid: str) -> dict:
    path = bus.snapshot_path(root, tid)
    if not path.is_file():
        raise StateError(
            f"task {tid} has no snapshot: it is not a build task, so there is "
            f"nothing to collect. `team verify {tid}` reads its citations."
        )
    snap = bus._try_read_obj(path)
    if not isinstance(snap, dict) or not isinstance(snap.get("create"), list):
        raise StateError(f"snapshot for task {tid} is unreadable or malformed")
    return snap


def _contained(base: Path, rel: str, what: str) -> Path:
    """Resolve `rel` under `base`, refusing anything that escapes it.

    Same rule as verify's OUT_OF_TREE: containment is checked after resolving
    symlinks, so spelling a path differently cannot smuggle one out.
    """
    base_resolved = base.resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base_resolved):
        raise StateError(f"{what} path escapes {base_resolved}: {rel}")
    return target


def collect(root: Path, tid: str, wt=None) -> list[str]:
    wt = wt if wt is not None else worktrees.Worktrees()
    snap = _snapshot(root, tid)
    agent = snap.get("agent", "")

    # A task that never sealed may have a half-written file on disk. The grunt
    # announces completion by sealing; until then its output is not output.
    if not bus.result_path(root, tid).is_file():
        raise StateError(
            f"task {tid} has not sealed. Wait for it (`team wait --task {tid}`) "
            f"before collecting a file the grunt may still be writing."
        )

    src_root = worktrees.path(root, agent)
    if not src_root.is_dir():
        raise StateError(f"no worktree for {agent!r} at {src_root}")

    # Resolve and check *everything* before copying *anything*. A collision on
    # the second of three files must not leave the first one already copied.
    plan: list[tuple[Path, Path, str]] = []
    for rel in snap["create"]:
        src = _contained(src_root, rel, "created")
        dst = _contained(root, rel, "destination")
        if not src.is_file():
            raise StateError(
                f"task {tid} declared {rel} but {agent} never created it")
        if dst.exists():
            raise StateError(
                f"refusing to overwrite {rel}: it already exists in the main "
                f"tree. Move it aside, or delete it, and collect again."
            )
        plan.append((src, dst, rel))

    actions = []
    for src, dst, rel in plan:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        actions.append(f"collected {rel}")
    return actions
