"""Bus lifecycle, and the target repo's qwen configuration.

`team init` mutates the target repo: it writes `.qwen/settings.json` so that a
grunt qwen pane does not autoload `AGENTS.md`/`CLAUDE.md`/`QWEN.md` (measured:
qwen loads them from the git root and `/clear` does not drop them), does not
wedge on an approval prompt (`approvalMode: yolo`), and cannot use its write
tools (`excludeTools` -- `coreTools` allowlists are measured to be ignored by
qwen, so `excludeTools` is the only lock that actually holds; see the brief).
Everything `init` touches is recorded in `.team/init.json` so `team down` can
put it back.

Both `init(force=True)` and `down` delete `.team`. Deleting the wrong
directory here means deleting a user's working tree, so every deletion goes
through `_assert_safe_to_delete`, which resolves symlinks and independently
re-derives the repo boundary via `bus.repo_root` before allowing anything to
be removed. Do not call `shutil.rmtree` on a bus path anywhere else in this
module without going through that guard first.
"""
import shutil
from pathlib import Path

from team import bus

BUS_SUBDIRS = ("inbox/lead", "results", "staging", "logs", "ids", "dead")
GITIGNORE_ENTRIES = (".team/", ".qwen/")
TEAM_DIRNAME = ".team"

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


def init(root: Path, force: bool = False) -> list[str]:
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
        f"wrote {settings} (grunt: no context files, no write tools, approvalMode=YOLO)",
        "WARNING: while this session is live, your own `qwen` in this repo loses "
        "CLAUDE.md context and runs in YOLO mode. `team down` restores it.",
    ]


def down(root: Path) -> list[str]:
    team = bus.team_dir(root)
    actions: list[str] = []

    exists = team.exists() or team.is_symlink()
    meta = {}
    if exists:
        _assert_safe_to_delete(team, root)
        init_json = team / "init.json"
        if init_json.exists():
            meta = bus.read_json(init_json)

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
