"""Bus lifecycle, and the target repo's qwen configuration.

`team init` mutates the target repo: it writes `.qwen/settings.json` so that a
grunt qwen pane does not autoload `AGENTS.md`/`CLAUDE.md`/`QWEN.md` (measured:
qwen loads them from the git root and `/clear` does not drop them) and does not
wedge on an approval prompt (`approvalMode: yolo`). Everything `init` touches is
recorded in `.team/init.json` so `team down` can put it back.

The same settings are provisioned into every grunt worktree, because that is
where the grunt pane's cwd is and qwen reads its config from the git root it
finds there.

Both `init(force=True)` and `down` delete `.team`. Deleting the wrong
directory here means deleting a user's working tree, so every deletion goes
through `_assert_safe_to_delete`, which resolves symlinks and independently
re-derives the repo boundary via `bus.repo_root` before allowing anything to
be removed. Do not call `shutil.rmtree` on a bus path anywhere else in this
module without going through that guard first.
"""
import shutil
from pathlib import Path

from team import bus, worktrees

BUS_SUBDIRS = ("inbox/lead", "results", "staging", "logs", "ids", "dead",
               "snapshots")
GITIGNORE_ENTRIES = (".team/", ".qwen/")
TEAM_DIRNAME = ".team"

# `excludeTools` is defence in depth and NOTHING MORE. Measured on task 013: a
# pane running with exactly these settings called `WriteFile` four times. qwen
# ignores `coreTools` allowlists, and ignores `excludeTools` for at least
# `write_file`. No configuration in this file prevents a grunt from writing any
# file its shell can reach. The pane's cwd (its worktree) and the containment
# check are the enforcement; this dict is a hint. Never write a comment, a
# docstring, or a protocol line that claims a grunt "has no write tools".
GRUNT_SETTINGS = {
    "context": {"fileName": ["TEAM_GRUNT_CONTEXT.md"]},
    "tools": {
        "approvalMode": "yolo",
        "computerUse": {"enabled": False},
        "excludeTools": ["write_file", "replace", "edit", "save_memory", "web_fetch"],
    },
}


class StateError(Exception):
    """A bus/config precondition was violated: a stale bus without --force,
    a stale settings backup without --force, or a delete target that could
    not be proven safe. Callers should surface this and stop."""


def _qwen_settings(root: Path) -> Path:
    return root / ".qwen" / "settings.json"


def _backup(root: Path) -> Path:
    return root / ".qwen" / "settings.json.team-backup"


def _assert_safe_to_delete(target: Path, root: Path) -> None:
    """Refuse to treat `target` as a deletable bus dir unless it is
    unambiguously `<repo_root>/.team` once symlinks are resolved.

    A symlinked `.team` is refused outright, even if it happens to point back
    inside the repo -- `.team` must always be a real directory this module
    created, never a link. For a non-symlink target, the resolved path's
    final component must be exactly ".team" (catching a `bus.team_dir` that
    hands back some other directory under a misleading name) *and* the
    independently-recomputed `bus.repo_root(root)` must be one of its
    resolved parents (catching one that resolves outside the repo entirely).
    Any failure raises `StateError` instead of touching the filesystem.
    """
    if target.is_symlink():
        raise StateError(f"refusing to delete {target}: it is a symlink, not a real directory")
    resolved = target.resolve()
    root_resolved = bus.repo_root(root).resolve()
    if resolved.name != TEAM_DIRNAME:
        raise StateError(
            f"refusing to delete {target}: resolves to {resolved}, "
            f"whose name is {resolved.name!r}, not {TEAM_DIRNAME!r}"
        )
    if root_resolved not in resolved.parents:
        raise StateError(
            f"refusing to delete {target}: resolves to {resolved}, "
            f"which is not inside {root_resolved}"
        )


def _update_gitignore(root: Path) -> None:
    path = root / ".gitignore"
    existing = path.read_text().splitlines() if path.exists() else []
    missing = [e for e in GITIGNORE_ENTRIES if e not in existing]
    if not missing:
        return
    lines = existing + missing
    bus.atomic_write(path, "\n".join(lines) + "\n")


def init(root: Path, force: bool = False, wt=None) -> list[str]:
    wt = wt if wt is not None else worktrees.Worktrees()
    team = bus.team_dir(root)
    stale = team.exists() or team.is_symlink()
    if stale and not force:
        raise StateError(
            f"{team} already exists. A stale bus makes `team wait` return "
            f"instantly on yesterday's results. Run `team down`, or pass --force."
        )

    prior_meta = {}
    if stale:
        _assert_safe_to_delete(team, root)
        init_json = team / "init.json"
        if init_json.exists():
            prior_meta = bus.read_json(init_json)
        shutil.rmtree(team)

    # A bus removed by hand -- `rm -rf .team` -- takes its worktrees' directories
    # with it and leaves git's admin entries behind, marked prunable. Left there,
    # the next `worktree add` for the same agent is refused as already
    # registered. `prune` removes only entries whose directory is gone, so it is
    # a no-op on a healthy repo and on the user's own unrelated worktrees.
    #
    # It is a repair, not a precondition: a bus is perfectly usable for `find`
    # tasks in a tree where git cannot run at all. Report the failure, never
    # raise on it.
    notes: list[str] = []
    try:
        wt.prune(root)
    except worktrees.WorktreeError as exc:
        notes.append(f"note: could not prune stale worktrees: {exc}")

    for sub in BUS_SUBDIRS:
        (team / sub).mkdir(parents=True, exist_ok=True)
    bus.write_json(team / "roster.json", {})

    settings, backup = _qwen_settings(root), _backup(root)
    settings.parent.mkdir(parents=True, exist_ok=True)

    if "created_qwen_settings" in prior_meta:
        # Re-initializing over a bus we created before (a --force re-init with
        # no `down` in between): settings.json, if present, already holds our
        # own GRUNT_SETTINGS, not fresh user content. Trust the provenance
        # recorded last time instead of re-deriving it from a file we already
        # overwrote -- that is what makes repeated --force idempotent and
        # keeps us from re-copying our own output over the *real* backup.
        created = prior_meta["created_qwen_settings"]
    elif settings.exists() and bus._try_read_obj(settings) == GRUNT_SETTINGS:
        # Provenance was lost -- someone removed .team by hand instead of
        # running `team down`. But the file on disk is byte-for-byte our own
        # GRUNT_SETTINGS, so it cannot be user content. Treat it as ours, or
        # `down` will "restore" our YOLO config as though the user wrote it.
        created = True
    else:
        created = not settings.exists()
        if not created:
            if backup.exists() and not force:
                raise StateError(
                    f"{backup} already exists from a previous `team init` that was "
                    f"never cleanly `team down`'d. Refusing to overwrite it again -- "
                    f"that would discard the original {settings} it is holding. "
                    f"Restore it by hand, or pass --force."
                )
            shutil.copy2(settings, backup)

    bus.write_json(settings, GRUNT_SETTINGS)
    bus.write_json(team / "init.json", {"created_qwen_settings": created})
    _update_gitignore(root)

    return [
        f"bus ready at {team}",
        f"wrote {settings} (grunt: no context files, approvalMode=YOLO). "
        f"A grunt's write tools and shell stay unrestricted; its worktree, not "
        f"this file, is what contains it.",
        "WARNING: while this session is live, your own `qwen` in this repo loses "
        "CLAUDE.md context and runs in YOLO mode. `team down` restores it.",
        *notes,
    ]


def provision(work: Path) -> Path:
    """Write the grunt settings into a worktree, whose git root -- and so whose
    qwen project root -- is the worktree itself.

    Called by `worktree up`, before any `send` snapshots the tree, so the file
    is already there when containment takes its baseline. `worktrees.dirty`
    filters `PROVISIONED` regardless, so ordering is belt and braces.
    """
    settings = work / ".qwen" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    bus.write_json(settings, GRUNT_SETTINGS)
    return settings


def _refuse_if_uncollected(root: Path, wt, force: bool) -> None:
    """`git worktree remove --force` discards untracked files without a word,
    and a grunt's entire output is untracked files. Teardown is the one moment a
    user cannot undo, so it asks before it destroys.

    Separated from the removal so that `down` can run it *before* killing any
    pane. A refused teardown must not have already destroyed the grunts.
    """
    if force:
        return
    uncollected = {a: lines for a in wt.agents(root) if (lines := wt.dirty(root, a))}
    if uncollected:
        detail = "; ".join(
            f"{a}: {len(lines)} file(s), e.g. {lines[0].split(maxsplit=1)[-1]}"
            for a, lines in sorted(uncollected.items()))
        raise StateError(
            f"refusing to remove worktrees holding uncollected work -- "
            f"{detail}. Run `team collect <tid>` for the tasks you want, "
            f"or `team down --force` to discard them."
        )


def _kill_grunt_panes(root: Path, killer) -> list[str]:
    """Kill every grunt pane, never the lead's -- that is where the person who
    typed `team down` is sitting.

    `killer` is a callable taking a pane target. It is injected rather than
    imported: this module must keep working if tmux were swapped out, and
    `panes.py` is the only module allowed to know tmux exists.

    Called after the uncollected check and before the worktrees are removed. A
    grunt whose worktree is deleted out from under it keeps running in a
    directory that no longer exists.
    """
    if killer is None:
        return []
    roster = bus._try_read_obj(bus.roster_path(root)) or {}
    actions = []
    for agent, entry in sorted(roster.items()):
        if agent == "lead" or not isinstance(entry, dict) or not entry.get("pane"):
            continue
        killer(entry["pane"])
        actions.append(f"killed pane for {agent}")
    return actions


def _drop_worktrees(root: Path, wt, force: bool) -> list[str]:
    """Remove every grunt worktree. Assumes `_refuse_if_uncollected` already
    ran; it re-checks, because `down` is not the only caller of `config.down`."""
    agents = wt.agents(root)
    if not agents:
        return []
    _refuse_if_uncollected(root, wt, force)

    actions = []
    for agent in agents:
        wt.remove(root, agent)
        actions.append(f"removed worktree for {agent}")
    wt.prune(root)
    return actions


def down(root: Path, force: bool = False, wt=None, killer=None) -> list[str]:
    wt = wt if wt is not None else worktrees.Worktrees()
    team = bus.team_dir(root)
    actions: list[str] = []

    exists = team.exists() or team.is_symlink()
    meta = {}
    if exists:
        _assert_safe_to_delete(team, root)
        init_json = team / "init.json"
        if init_json.exists():
            meta = bus.read_json(init_json)
        # Ordering is the whole safety property. Refuse first, while nothing has
        # been touched; then kill the panes, so no agent is left running in a
        # directory that is about to vanish; then remove the worktrees, which
        # live *inside* .team and would otherwise leave prunable admin entries
        # behind; then rmtree.
        _refuse_if_uncollected(root, wt, force)
        actions += _kill_grunt_panes(root, killer)
        actions += _drop_worktrees(root, wt, force)

    settings, backup = _qwen_settings(root), _backup(root)
    if backup.exists():
        shutil.move(str(backup), str(settings))
        actions.append(f"restored {settings} from backup")
    elif meta.get("created_qwen_settings") and settings.exists():
        settings.unlink()
        actions.append(f"removed {settings}")

    if exists:
        shutil.rmtree(team)
        actions.append(f"removed {team}")

    return actions
