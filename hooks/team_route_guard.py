#!/usr/bin/env python3
"""team-route-guard — PreToolUse hook. Enforces "do not do the work yourself".

`TEAMCHAT.md` tells the lead not to open the file it sent a grunt to read. A
lead whose grunt just returned FABRICATED is one `grep` away from the answer,
and it will take that grep -- at which point the decompile is in its context and
it has paid for the grunt as well.

So while a task is in flight, the lead may not reach into that task's `--scope`.
The guard lifts by itself the moment the task seals: the bus already knows, and
this hook keeps no state of its own.

Deliberately narrow. It denies reaching INTO a scope; it does not deny a
repo-wide `Grep` that merely contains one, because that would block the lead
from reading its own source while any task is open, and a guard that blocks
ordinary work gets switched off. Hard-deny the clear case, nudge on ambiguity --
the same rule as ~/.claude/hooks/route-guard.py, from which this borrows its
stdin/stdout contract.

FAILS OPEN. Any error, any surprise, and the tool call proceeds. A broken guard
must never break a tool call. Disable with TEAM_ROUTE_GUARD=0.

Two ways a guard installed globally can hurt a session that never heard of this
tool, both measured, both closed here:

- A PreToolUse hook exiting `2` BLOCKS the tool call. `python3 <missing file>`
  exits 2. So the settings entry must not invoke this file directly -- see the
  `sh -c 'test -r ...'` wrapper in the spec. This script itself only ever
  exits 0.
- A lead that crashes leaves a task file with no result and no dead marker, and
  `open_scopes` would call it in flight forever: that scope becomes unreadable
  in every future session in that repo. Hence STALE_AFTER.
"""
import json
import os
import shlex
import sys
import time
from pathlib import Path

TEAM = ".team"

# A task file's mtime is its dispatch time. The documented wait is
# `--timeout 600`; a grunt turn is minutes. Past this age the task is not
# in flight, it is wreckage -- a lead that died, or a tmux server that was
# killed -- and the guard must not hold its scope hostage forever.
STALE_AFTER = 3600.0
SKIP_TOOLS = frozenset()          # every matched tool is considered
PATH_TOOLS = {"Read": "file_path", "Grep": "path", "Glob": "path"}

# `team send grunt1 --scope src/foo` names src/foo on its own command line. Once
# that task is open, a guard without this allowlist denies every subsequent team
# command touching the same scope -- including `team verify`, the one verb that
# resolves the situation. First word only: `team` never needs a `cd`.
ALLOWED_COMMANDS = frozenset({"team"})


def emit(decision: str, reason: str = "", ctx: str = "") -> None:
    hso = {"hookEventName": "PreToolUse", "permissionDecision": decision}
    if reason:
        hso["permissionDecisionReason"] = reason
    if ctx:
        hso["additionalContext"] = ctx
    print(json.dumps({"hookSpecificOutput": hso}))
    sys.exit(0)


def bus_root(start: Path) -> Path | None:
    for cand in [start, *start.parents]:
        if (cand / TEAM).is_dir():
            return cand
    return None


def _obj(path: Path):
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _stale(task_file: Path, now: float) -> bool:
    """Too old to be a running turn. Unreadable mtime counts as stale: the
    guard's default must be to let go, never to hold."""
    try:
        return (now - task_file.stat().st_mtime) > STALE_AFTER
    except OSError:
        return True


def open_scopes(root: Path, now: float | None = None) -> list[tuple[str, str, str]]:
    """(task_id, agent, scope_path) for every scope of every in-flight task.

    In flight = a task file exists, is younger than STALE_AFTER, and has neither
    a sealed result nor a dead marker. Build tasks carry an empty scope and so
    guard nothing.
    """
    now = time.time() if now is None else now
    team = root / TEAM
    out: list[tuple[str, str, str]] = []
    inbox = team / "inbox"
    if not inbox.is_dir():
        return out
    for agent_dir in inbox.iterdir():
        if not agent_dir.is_dir() or agent_dir.name == "lead":
            continue
        for task_file in agent_dir.glob("*.json"):
            tid = task_file.stem
            if (team / "results" / f"{tid}.json").exists():
                continue
            if (team / "dead" / tid).exists() or (team / "dead" / f"{tid}.json").exists():
                continue
            if _stale(task_file, now):
                continue
            task = _obj(task_file)
            if not task:
                continue
            for rel in task.get("scope") or []:
                if isinstance(rel, str) and rel:
                    out.append((tid, task.get("to", agent_dir.name), rel))
    return out


def _resolve(root: Path, raw: str) -> Path | None:
    try:
        p = Path(raw)
        return (p if p.is_absolute() else root / p).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _literal_prefix(pattern: str) -> str:
    """`src/**/*.cs` -> `src`. A glob is a path until its first wildcard."""
    parts = []
    for part in Path(pattern).parts:
        if any(ch in part for ch in "*?["):
            break
        parts.append(part)
    return str(Path(*parts)) if parts else ""


def _targets(tool: str, inp: dict) -> list[str]:
    if tool in PATH_TOOLS:
        raw = inp.get(PATH_TOOLS[tool])
        if tool == "Glob":
            # Glob's `path` is a root; the pattern carries the reach.
            pats = [p for p in (_literal_prefix(inp.get("pattern") or ""),) if p]
            return [raw] * bool(raw) + pats
        return [raw] if raw else []
    if tool == "Bash":
        command = inp.get("command") or ""
        try:
            words = shlex.split(command)
        except ValueError:
            return []
        if words and Path(words[0]).name in ALLOWED_COMMANDS:
            return []
        return [w for w in words[1:] if not w.startswith("-")]
    return []


def decide(payload: dict, env: dict | None = None) -> tuple[str, str, str]:
    """(decision, reason, additional_context). Never raises."""
    env = os.environ if env is None else env
    if env.get("TEAM_ROUTE_GUARD") == "0":
        return "allow", "", ""

    tool = payload.get("tool_name") or ""
    inp = payload.get("tool_input") or {}
    if not isinstance(inp, dict):
        return "allow", "", ""

    cwd = payload.get("cwd") or os.getcwd()
    root = bus_root(Path(cwd).resolve())
    if root is None:
        return "allow", "", ""

    scopes = open_scopes(root)
    if not scopes:
        return "allow", "", ""

    targets = [t for t in (_resolve(root, raw) for raw in _targets(tool, inp) if raw)
               if t is not None]
    if not targets:
        return "allow", "", ""

    nudges = []
    for tid, agent, rel in scopes:
        scope = _resolve(root, rel)
        # A scope that escapes the repo guards nothing. It is a grunt's reading
        # list, not a filesystem ACL.
        if scope is None or not scope.is_relative_to(root):
            continue
        for target in targets:
            if target == scope or target.is_relative_to(scope):
                return "deny", _deny_message(tid, agent, rel), ""
            if scope.is_relative_to(target):
                nudges.append(f"{agent} is reading {rel} (task {tid})")

    if nudges:
        return "allow", "", (
            "In flight: " + "; ".join(sorted(set(nudges))) +
            ". Your search covers that scope. Prefer `team wait` then "
            "`team verify` over reading it yourself."
        )
    return "allow", "", ""


def _deny_message(tid: str, agent: str, rel: str) -> str:
    return (
        f"{agent} is reading {rel} right now (task {tid}). Reading it yourself "
        f"spends the grunt: the file lands in your context and you have paid "
        f"for both.\n\n"
        f"    team wait --task {tid} --timeout 600\n"
        f"    team verify {tid}\n\n"
        f"If {tid} failed, re-ask with a correction. Measured: a grunt whose two "
        f"citations both failed returned 2/2 PASS after one correction naming "
        f"what was wrong.\n\n"
        f"Genuinely need it? TEAM_ROUTE_GUARD=0, or `team grunt rm {agent}`."
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        decision, reason, ctx = decide(payload)
    except Exception:
        # Fail open, loudly to nobody. A guard that breaks tool calls when the
        # bus is malformed is worse than no guard.
        emit("allow")
        return
    emit(decision, reason, ctx)


if __name__ == "__main__":
    main()
