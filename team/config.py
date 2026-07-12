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
through `_assert_safe_to_delete`, which resolves symlinks and checks the target
is a real `.team*` directory sitting directly inside the invocation root (the
cwd the command was run in) before allowing anything to be removed. Do not call
`shutil.rmtree` on a bus path anywhere else in this module without going through
that guard first.
"""
import copy
import os
import shutil
from pathlib import Path

from team import bus, worktrees

BUS_SUBDIRS = ("inbox/lead", "results", "staging", "logs", "ids", "dead",
               "snapshots")
# A glob, not `.team/`: named buses (`.team-auth/`, `.team-ui/`) must be ignored
# too, and `.team*/` covers the plain `.team/` as well.
GITIGNORE_ENTRIES = (".team*/", ".qwen/")
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
    # A grunt with auto-skill ON mines its OWN session for "reusable skills" after a
    # tool-heavy task, then wedges the pane on a keep/discard review modal (the bus
    # times out waiting for a prompt that never returns). Worse, the skills it writes
    # codify its failure modes as doctrine (measured: an auto-skill told the grunt to
    # write "placeholder content for assets", to `dotnet build` — which the never-build
    # rule forbids — and to "remove external references" when deps don't resolve, which
    # is exactly how a plugin csproj gets gutted to a net472 stub). Off at the source.
    "memory": {"enableAutoSkill": False},
    # A grunt is a code transcriber/finder; it never needs the user's animation
    # (hyperframes*) or media-authoring skills. qwen renders only a skill's
    # name+description into the always-present available-skills block and loads
    # the body on invocation -- so the standing cost of leaving these in is small,
    # but a confused grunt can still *invoke* one and pull ~18k tokens of body it
    # will never use. `skills.disabled` (matched case-insensitively by name)
    # drops them from the block entirely and makes them un-invocable. This is the
    # worktree's workspace settings layer, so the exclusion is grunt-only: the
    # user's own qwen, reading its own ~/.qwen/settings.json, keeps them.
    "skills": {"disabled": [
        "hyperframes", "hyperframes-animation", "hyperframes-cli",
        "hyperframes-core", "hyperframes-creative", "hyperframes-keyframes",
        "hyperframes-registry", "media-use",
    ]},
    # Grunt-scoped model regime. This is the worktree's workspace settings layer,
    # so these override the user's own ~/.qwen for grunts only.
    #
    # `name` pins coder30. A grunt is a transcriber/finder/scaffolder; it must not
    # drift onto whatever model the user last selected for their interactive qwen
    # (measured this session: the user switched their default to run a capacity
    # test, and every grunt would have silently followed).
    #
    # `sessionTokenLimit` is a hard prompt-token ceiling: qwen refuses ("start a
    # new session") instead of shipping a 200k+ prompt at the MLX server and OOMing
    # it (the 3.6-35B melts on ~47k already). A grunt clears context per task, so a
    # legitimate task never approaches this; it only catches a runaway read.
    #
    # `maxWallTimeSeconds` is the runaway guard -- a hung pane frees itself at 15
    # minutes rather than sitting until the bus times out. `maxSessionTurns` is -1
    # (qwen's "unlimited"): a turn cap is redundant with wall-time + qwen's own
    # loop detection + the lead's bus wait-timeout, and measured, it only ever
    # false-killed real work -- a legit find over 5 files burned dozens of
    # model<->tool turns and a tight cap (30) cut it off mid-task. Bound the clock,
    # not the turn count.
    #
    # NOTE deliberately NOT setting `model.generationConfig` here: with an active
    # modelProvider (the user's openai block), qwen IGNORES top-level
    # generationConfig fields and only warns. Temperature=0 for grunt determinism
    # is therefore set where it is honored -- as `extra_body.temperature` on the
    # coder30/3.6-35B provider entries in ~/.qwen/settings.json, which the openai
    # adapter deep-merges into the request body last (so it beats the harness's
    # default 0.70). Greedy is strictly correct for verbatim-copy/precise-edit/
    # scaffold work and mlx-serve applies no repetition penalty, so temp>0 is pure
    # downside. The grunt inherits that provider config through the settings merge.
    "model": {
        "name": "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit-dwq-v2",
        "sessionTokenLimit": 200000,
        "maxSessionTurns": -1,
        "maxWallTimeSeconds": 900,
    },
}

# Defaults live in GRUNT_SETTINGS above (the probe-derived, pinned payload).
# grunt_settings() reproduces it byte-for-byte when the environment is unset, and
# only overrides fields for which a TEAM_GRUNT_* variable is present -- so the
# author's own rig (model pinned, provider in ~/.qwen) is unchanged, while a
# downloader can retarget the grunt at any OpenAI-compatible server without
# editing this file.
GRUNT_CONTEXT_WINDOW_DEFAULT = 262144
# The provider's `envKey` names an environment variable; the key itself is never
# stored in settings.json or the repo. _grunt_env (cli.py) exports this same name
# into the grunt pane so qwen can resolve it.
GRUNT_API_KEY_ENV = "TEAM_GRUNT_API_KEY"


def grunt_settings(env: dict | None = None) -> dict:
    """The `.qwen/settings.json` payload for a grunt (and the lead repo on init).

    Built fresh from `env` (default `os.environ`) each call. With no TEAM_GRUNT_*
    variables set it equals GRUNT_SETTINGS exactly, so every provenance check and
    pinned test still holds and the author's setup is untouched. Pass `env={}` in
    tests for the pure defaults regardless of the caller's shell.

    Overrides (all optional):
      TEAM_GRUNT_MODEL                grunt model name
      TEAM_GRUNT_SESSION_TOKEN_LIMIT  hard prompt-token ceiling
      TEAM_GRUNT_WALL_SECONDS         runaway wall-clock guard (seconds)
      TEAM_GRUNT_BASE_URL             if set, an OpenAI-compatible provider block
                                      is written so the grunt is self-contained;
                                      unset, the grunt uses the user's own ~/.qwen
                                      provider (original behavior, no extra key).
      TEAM_GRUNT_CONTEXT_WINDOW       provider context window (only with a base url)
    """
    env = os.environ if env is None else env
    s = copy.deepcopy(GRUNT_SETTINGS)

    model = env.get("TEAM_GRUNT_MODEL")
    if model:
        s["model"]["name"] = model
    if env.get("TEAM_GRUNT_SESSION_TOKEN_LIMIT"):
        s["model"]["sessionTokenLimit"] = int(env["TEAM_GRUNT_SESSION_TOKEN_LIMIT"])
    if env.get("TEAM_GRUNT_WALL_SECONDS"):
        s["model"]["maxWallTimeSeconds"] = int(env["TEAM_GRUNT_WALL_SECONDS"])

    base_url = env.get("TEAM_GRUNT_BASE_URL")
    if base_url:
        # temperature 0 as extra_body: it is honored under an active provider
        # (top-level generationConfig is ignored there). Greedy is correct for
        # verbatim-copy / precise-edit / scaffold work.
        name = s["model"]["name"]
        s["modelProviders"] = {"openai": [{
            "id": name,
            "name": name,
            "baseUrl": base_url,
            "envKey": GRUNT_API_KEY_ENV,
            "generationConfig": {
                "contextWindowSize": int(
                    env.get("TEAM_GRUNT_CONTEXT_WINDOW", GRUNT_CONTEXT_WINDOW_DEFAULT)),
                "extra_body": {"temperature": 0},
            },
        }]}
    return s


def grunt_backend_status(env: dict | None = None) -> tuple[str, str | None]:
    """Where a grunt will get its model — for first-launch guidance in `init`.

    Returns one of:
      ("env", base_url)      TEAM_GRUNT_BASE_URL is set; `grunt_settings` writes a
                             self-contained provider, so the grunt is ready.
      ("global", model|None) the grunt CLI's global `~/.qwen/settings.json` already
                             has a provider (or a pinned model); grunts inherit it.
      ("none", None)         no backend configured anywhere — the grunt CLI needs
                             setting up first, or TEAM_GRUNT_BASE_URL needs setting.
    """
    env = os.environ if env is None else env
    if env.get("TEAM_GRUNT_BASE_URL"):
        return ("env", env["TEAM_GRUNT_BASE_URL"])
    cfg = Path.home() / ".qwen" / "settings.json"
    obj = bus._try_read_obj(cfg) if cfg.is_file() else None
    if isinstance(obj, dict) and (obj.get("modelProviders") or (obj.get("model") or {}).get("name")):
        return ("global", (obj.get("model") or {}).get("name"))
    return ("none", None)


def _grunt_backend_note(env: dict | None = None) -> str:
    """A one-line note for `init` output: prompt for CLI setup when no backend
    exists, or surface the global profile (and how to point a grunt at a
    different model) when one does."""
    status, detail = grunt_backend_status(env)
    if status == "none":
        return (
            "SETUP NEEDED: no grunt model backend found. Your grunt CLI (qwen) has no global "
            "~/.qwen provider and TEAM_GRUNT_BASE_URL is unset. Configure your CLI of choice "
            "(run `qwen` once and set a model/provider), or set TEAM_GRUNT_BASE_URL to an "
            "OpenAI-compatible server, before adding grunts. See SERVER.md."
        )
    if status == "env":
        return f"grunt backend: TEAM_GRUNT_BASE_URL -> {detail} (a provider is written for grunts)."
    # Report the model the grunt actually runs (the pinned grunt_settings model),
    # not the caller's global default -- a grunt is pinned and does not follow the
    # interactive default in ~/.qwen.
    model = grunt_settings(env)["model"]["name"]
    return (
        f"grunt backend: using your global ~/.qwen providers; grunts run model {model}. Override the "
        "model per-team with TEAM_GRUNT_MODEL, or point elsewhere with TEAM_GRUNT_BASE_URL."
    )


class StateError(Exception):
    """A bus/config precondition was violated: a stale bus without --force,
    a stale settings backup without --force, or a delete target that could
    not be proven safe. Callers should surface this and stop."""


def _qwen_settings(root: Path) -> Path:
    return root / ".qwen" / "settings.json"


def _backup(root: Path) -> Path:
    return root / ".qwen" / "settings.json.team-backup"


def _assert_safe_to_delete(target: Path, root: Path) -> None:
    """Refuse to treat `target` as a deletable bus dir unless it is unambiguously
    a `<root>/.team` or `<root>/.team-<slug>` once symlinks are resolved, where
    `root` is the directory the command was invoked in (the cwd).

    A symlinked bus dir is refused outright, even if it happens to point back
    inside `root` -- a bus dir must always be a real directory this module
    created, never a link. For a non-symlink target, the resolved path's final
    component must be a bus dir name (catching a `bus.team_dir` that hands back
    some other directory under a misleading name) *and* `root` itself must be one
    of its resolved parents (catching one that resolves outside cwd entirely).
    The boundary is `root`, not a walked-up git repo: the bus lives where you run,
    so `down` must delete `<cwd>/.team` whether or not cwd is its own git repo.
    Any failure raises `StateError` instead of touching the filesystem.
    """
    if target.is_symlink():
        raise StateError(f"refusing to delete {target}: it is a symlink, not a real directory")
    resolved = target.resolve()
    root_resolved = root.resolve()
    if not bus.BUS_DIR_RE.fullmatch(resolved.name):
        raise StateError(
            f"refusing to delete {target}: resolves to {resolved}, "
            f"whose name is {resolved.name!r}, not '.team' or '.team-<slug>'"
        )
    if root_resolved not in resolved.parents:
        raise StateError(
            f"refusing to delete {target}: resolves to {resolved}, "
            f"which is not inside {root_resolved}"
        )


def _other_bus_dirs(root: Path, current: Path) -> list[Path]:
    """Every OTHER `.team*` bus directory in `root`, besides `current`.

    This is the ref count behind the shared `.qwen`. Both named buses live in
    one git root and so read one `.qwen/settings.json`: the FIRST bus to `init`
    backs the user's real settings up, and only the LAST bus to go `down`
    restores them. Both decisions reduce to "is any sibling bus still here?".
    """
    cur = current.resolve()
    return [p for p in root.glob(".team*")
            if p.is_dir() and bus.BUS_DIR_RE.fullmatch(p.name)
            and p.resolve() != cur]


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

    if _other_bus_dirs(root, team):
        # Not the first team in this repo. A sibling bus already provisioned the
        # shared `.qwen`, backing the user's real settings.json up. Touching the
        # backup now would clobber theirs, and deriving `created` from the file
        # would mistake their GRUNT_SETTINGS for our own doing. Own nothing: this
        # bus's `down` must not restore or remove settings another bus manages.
        created = False
    elif "created_qwen_settings" in prior_meta:
        # Re-initializing over a bus we created before (a --force re-init with
        # no `down` in between): settings.json, if present, already holds our
        # own GRUNT_SETTINGS, not fresh user content. Trust the provenance
        # recorded last time instead of re-deriving it from a file we already
        # overwrote -- that is what makes repeated --force idempotent and
        # keeps us from re-copying our own output over the *real* backup.
        created = prior_meta["created_qwen_settings"]
    elif settings.exists() and bus._try_read_obj(settings) == grunt_settings():
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

    bus.write_json(settings, grunt_settings())
    bus.write_json(team / "init.json", {"created_qwen_settings": created})
    _update_gitignore(root)

    return [
        f"bus ready at {team}",
        _grunt_backend_note(),
        f"wrote {settings} (grunt: no context files, approvalMode=YOLO). "
        f"A grunt's write tools and shell stay unrestricted; its worktree, not "
        f"this file, is what contains it.",
        "WARNING: while this session is live, your own `qwen` in this repo loses "
        "CLAUDE.md context and runs in YOLO mode. `team down` restores it.",
        *notes,
    ]


# Out-of-tree things the lead exposes at its repo root that a grunt needs but a
# detached-HEAD worktree does not inherit, because they are (and must be)
# uncommitted: the Claude project memory bank (`memory/`, a directory) and the
# context file qwen autoloads via `context.fileName` (`TEAM_GRUNT_CONTEXT.md`, a
# file -- the grunt's behavioural rules and pointer into the bank). Both are
# re-linked into the worktree on every provision, so they survive worktree
# teardown; without them a grunt starts every task blind.
PROVISIONED_LINKS = ("memory", "TEAM_GRUNT_CONTEXT.md")


def provision(work: Path, root: Path | None = None) -> Path:
    """Write the grunt settings into a worktree, whose git root -- and so whose
    qwen project root -- is the worktree itself.

    Called by `worktree up`, before any `send` snapshots the tree, so the file
    is already there when containment takes its baseline. `worktrees.dirty`
    filters `PROVISIONED` regardless, so ordering is belt and braces.

    When `root` is given, also propagates the main tree's `PROVISIONED_LINKS`
    (see `_provision_links`).
    """
    settings = work / ".qwen" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    bus.write_json(settings, grunt_settings())
    if root is not None:
        _provision_links(root, work)
    return settings


def _provision_links(root: Path, work: Path) -> None:
    """Re-create, inside the worktree, each `PROVISIONED_LINKS` entry the main
    tree exposes -- so a grunt sees the same memory bank and context file the
    lead does. Runs before the pane launches, so qwen reads the context file at
    boot.

    Each link points at the main entry's *resolved absolute* target, never a
    copied-verbatim relative target -- one that resolves from the repo root
    would break two directories deeper in `.team/work/<agent>`. Idempotent, and
    never clobbers a real file/dir a grunt happens to have under that name.
    """
    for name in PROVISIONED_LINKS:
        src = root / name
        if not src.is_symlink() and not src.exists():
            continue
        target = src.resolve()
        if not target.exists():
            continue
        link = work / name
        if link.is_symlink():
            if link.resolve() == target:
                continue
            link.unlink()
        elif link.exists():
            continue  # a real file/dir of that name in the worktree -- leave it
        link.symlink_to(target, target_is_directory=target.is_dir())


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
    if _other_bus_dirs(root, team):
        # A sibling bus is still live. The shared `.qwen` belongs to it now --
        # leave it exactly as it is. Only the last bus out gives it back.
        pass
    elif backup.exists():
        shutil.move(str(backup), str(settings))
        actions.append(f"restored {settings} from backup")
    elif settings.exists() and (
            meta.get("created_qwen_settings")
            or bus._try_read_obj(settings) == grunt_settings()):
        # Last bus out, no backup: the user had no settings.json, so the one on
        # disk is our own GRUNT_SETTINGS. `meta` covers the single-bus case; the
        # content check also catches a multi-bus teardown where the bus that
        # first recorded `created` was already removed, taking its init.json.
        settings.unlink()
        actions.append(f"removed {settings}")

    if exists:
        shutil.rmtree(team)
        actions.append(f"removed {team}")

    return actions
