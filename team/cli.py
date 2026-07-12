"""Argument parsing, wiring, and the exit-code contract.

  0 ok · 1 verify FAIL (unless --lenient) · 2 pane gone
  3 refused (schema violation or invalid state) · 4 timeout
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from team import (__version__, api, bus, buildverify, collect, config, log,
                  ops, panes, verify, wait, worktrees)
from team.config import StateError
from team.schema import SchemaError

OK, VERIFY_FAIL, PANE_GONE, REFUSED, TIMEOUT, BLOCKED = 0, 1, 2, 3, 4, 5

# The bus is RELATIVE TO THE DIRECTORY YOU RUN IN. `bootstrap`/`init` create it at
# cwd; `down` removes it at cwd. Not up (never climbing to $HOME by `.git`), not
# sideways. So all three take cwd verbatim -- NOT `repo_root()`, which walked up
# for a `.git` and, from a scratch dir under $HOME, tore down `$HOME/.team` (or
# nothing) while leaving the real bus untouched.
#
#   bootstrap  -- porcelain, the sole setup verb users see: `_pin_repo_here` makes
#                 cwd its own git repo (creating or nesting one), then writes the
#                 bus and claims the lead -- repo + bus + lead in one call.
#   init       -- plumbing, hidden: writes the paneless bus at cwd (named buses,
#                 headless e2e); assumes the dir exists.
#   down       -- removes the bus runtime at cwd; leaves the project .qwen in place.
#
# The OTHER verbs (send/verify/wait/result) address an EXISTING bus and find it by
# `.team` via `bus_root`, which stops at cwd when cwd holds the bus and only climbs
# for a grunt whose cwd is its worktree under `.team/work/<agent>`. From the lead's
# own directory that never leaves cwd either.
CWD_COMMANDS = frozenset({"bootstrap", "init", "down"})

# Never `build.sh`: it deploys shared libraries into the game directory before
# compiling, and a grunt is an unattended process with an unrestricted shell.
# It does not get a command that writes outside the repo. The lead runs build.sh
# after `verify` passes and `collect` has moved the files across.
DEFAULT_BUILD_CMD = ("dotnet", "build", "-v", "q", "--nologo")

# `brief` prints the lead's ground rules. It must work from anywhere -- a lead
# that has lost the path after a /compact is exactly who runs it -- so it
# resolves no root at all, not even a git one.
NO_ROOT_COMMANDS = frozenset({"brief"})

BRIEF = Path(__file__).resolve().parent.parent / "TEAMCHAT.md"
TEAM_BIN = Path(__file__).resolve().parent.parent / "bin" / "team"
DEFAULT_SESSION = "team"


def _roster(root: Path) -> dict:
    return api.roster(root)


def _write_roster(root: Path, roster: dict) -> None:
    bus.write_json(bus.roster_path(root), roster)


def _pane_for(root: Path, agent: str) -> str:
    return api.pane_for(root, agent)


def _next_grunt(roster: dict) -> str:
    n = 1
    while f"grunt{n}" in roster:
        n += 1
    return f"grunt{n}"


def _default_agent() -> str:
    """Infer the grunt's name from its cwd -- a grunt runs in `<bus>/work/<name>`.

    Grunts are unreliable at passing `--agent`, and a missing one silently
    attributed a grunt's message/seal to `grunt1` (measured: grunt2's note landed
    as `from: grunt1`). Reading the worktree path fixes attribution with no flag.
    Falls back to `grunt1` outside a worktree (the lead's own context)."""
    try:
        parts = Path.cwd().resolve().parts
        for i in range(len(parts) - 2):
            if parts[i].startswith(".team") and parts[i + 1] == "work":
                return parts[i + 2]
    except Exception:
        pass
    return "grunt1"


def _grunt_env() -> dict:
    """`team` on the new pane's PATH, and the bus it belongs to.

    A grunt calls `team result add`. Today PATH resolves only because the shell
    which ran `team-up` happened to export PYTHONPATH, and panes inherit it --
    an accident of the dogfood setup. `split-window -e` (measured: reaches the
    new pane and nothing else) makes it explicit.

    `TEAM_BUS` is the resolved bus dir name (`.team`, `.team-auth`, ...). The
    grunt's cwd is `<busdir>/work/<agent>`, so the walk-up in `resolve_bus_name`
    would find the right bus on its own -- but a grunt that `cd`s elsewhere, or a
    tool that runs `team` from a different directory, must still land on this
    team's bus, so it is pinned explicitly.

    `TEAM_GRUNT_API_KEY` (default `local`) is exported so a grunt whose
    `.qwen/settings.json` names it as a provider `envKey` (written by
    `config.grunt_settings` when `TEAM_GRUNT_BASE_URL` is set) can resolve it.
    Local servers ignore the value but qwen requires the variable to be present;
    the key is never stored in a file.
    """
    return {"PATH": f"{TEAM_BIN.parent}{os.pathsep}{os.environ.get('PATH', '')}",
            "TEAM_BUS": bus.resolve_bus_name(),
            config.GRUNT_API_KEY_ENV: os.environ.get(config.GRUNT_API_KEY_ENV, "local")}


def _digest(msg: dict) -> str:
    body = msg["body"].replace("\n", " ")
    if len(body) > 80:
        body = body[:77] + "..."
    return f"{msg['type']:<8} {msg['id']} from {msg['from']} task {msg['task']}: {body}"


def _add_provider_flags(p):
    """`--copy-provider` / `--skip-copy`: consent for copying the user's ~/.qwen
    model provider into the project (see `config.init`). Mutually exclusive."""
    g = p.add_mutually_exclusive_group()
    g.add_argument("--copy-provider", action="store_true", dest="copy_provider",
                   help="copy your ~/.qwen model provider into this project's .qwen "
                        "so grunts are self-contained (the config then lives in the "
                        "project; edit it there to retarget models)")
    g.add_argument("--skip-copy", action="store_true", dest="skip_copy",
                   help="do not copy a provider; you will configure the project "
                        ".qwen yourself")


def _resolve_copy_provider(args) -> bool:
    """Resolve the copy-provider consent for `config.init`.

    Explicit flags win. With neither and a provider actually present to copy,
    prompt on a TTY; OFF a TTY (the lead runs `team` from a subprocess) refuse
    rather than silently guess -- the lead is meant to ask the user and pass a
    flag. When there is nothing to copy (no global provider, or the
    TEAM_GRUNT_BASE_URL path writes its own), consent is moot -> False."""
    if getattr(args, "copy_provider", False):
        return True
    if getattr(args, "skip_copy", False):
        return False
    if config.grunt_backend_status()[0] != "global":
        return False  # nothing to copy: unconfigured, or self-contained via base url
    if sys.stdin.isatty():
        ans = input("Copy your ~/.qwen model provider into this project so grunts "
                    "are self-contained? [y/N] ").strip().lower()
        return ans in ("y", "yes")
    raise SystemExit(
        "consent needed: a ~/.qwen provider exists. Re-run with --copy-provider to "
        "copy it into this project's .qwen (self-contained grunts that live in the "
        "project), or --skip-copy to configure the project .qwen yourself.")


def cmd_init(args, root):
    """PLUMBING, hidden from `--help`. Write the bus at `root` WITHOUT a lead pane
    or any git setup -- it assumes the directory already exists. `bootstrap` is the
    porcelain users run: it does the git-init/pin and then claims the lead. `init`
    stays for the two workflows that need a paneless bus: named buses (`init --bus
    auth` -- several independent teams in one repo, each led separately), and the
    headless end-to-end tests. Resolved to cwd, never `repo_root()`, so a bare
    `team init` writes here and can never climb to $HOME the way it once did."""
    for line in config.init(root, force=args.force,
                            copy_provider=_resolve_copy_provider(args)):
        print(line)
    busname = bus.resolve_bus_name(getattr(args, "bus", None))
    if busname != bus.TEAM:
        # A named bus. The lead's later commands need to know which one; the
        # cleanest handoff is one exported var its whole shell picks up, so
        # `team send`/`verify`/`wait` need no repeated --bus.
        print(f"\nnamed bus {busname} is live. Adopt it in this shell so every "
              f"later `team` command targets it:\n    export TEAM_BUS={busname}")
    return OK


def _pin_repo_here(root: Path, wt) -> tuple[list[str], str]:
    """Make `root` -- the directory the user ran `bootstrap` in -- the git
    repo the bus will live in, so the bus can NEVER resolve above where they stand.

    The rule is absolute (the user has said so, repeatedly): everything lives WHERE
    YOU START IT. `repo_root()`/`bus_root()` walk UP, so without pinning, a bus in a
    scratch dir under a bigger repo resolves to that parent -- and all of $HOME is a
    git repo, so `team init` in ~/teamTest wrote ~/.team and rewrote the global
    ~/.qwen. Three cases:

      - no repo anywhere        -> `git init` here.
      - inside a bigger repo    -> `git init` here anyway (nested; git treats the
                                   inner .git as the boundary, so every verb resolves
                                   here, never climbing to the parent). Returns a
                                   notice -- a call made on the user's behalf.
      - `root` IS a repo root   -> nothing to do.

    Then guarantees a HEAD (an empty first commit) so a grunt worktree can check one
    out later. Returns (actions_done, notice_or_empty)."""
    actions: list[str] = []
    notice = ""
    top = wt.toplevel(root)
    if top is None:
        wt.init_repo(root)
        actions.append(f"git init {root}")
    elif top.resolve() != root:
        wt.init_repo(root)
        actions.append(f"git init {root} (its own repo, nested inside {top})")
        home = (f" {top} is your HOME directory, so a bus there would scatter grunt "
                f"worktrees across it and rewrite your global ~/.qwen -- kept here "
                f"instead." if top.resolve() == Path.home().resolve() else "")
        notice = (f"NOTE: {root} is inside the git repo at {top}. The bus must live "
                  f"where you started it, so {root} is now its own repo and the bus "
                  f"lands HERE, not at {top}.{home}\n"
                  f"If you meant the bus at the enclosing repo, run from there "
                  f"instead:\n  cd {top}")
    if not wt.has_commit(root):
        try:
            wt.empty_commit(root, "team: first commit")
        except worktrees.WorktreeError as exc:
            raise StateError(
                f"could not create the first commit: {exc}\n"
                f"A worktree cannot check out an unborn HEAD. If git is asking who "
                f"you are, set user.email and user.name and re-run."
            ) from exc
        actions.append("created an empty first commit")
    return actions, notice


def cmd_brief(args, root):
    if not BRIEF.is_file():
        print(f"no brief at {BRIEF}", file=sys.stderr)
        return REFUSED
    print(BRIEF.read_text() if args.show else BRIEF)
    return OK


def cmd_worktree_up(args, root):
    """Give every grunt in the roster a private worktree, and put the grunt
    settings inside it -- the pane's cwd is the worktree, so that is the git
    root qwen reads its config from.

    Idempotent: an agent that already has one keeps it, and is re-provisioned,
    so re-running after `init` rewrote the settings is safe. The lead never gets
    a worktree -- it works in the main tree."""
    wt = worktrees.Worktrees()
    existing = set(wt.agents(root))
    made = 0
    for agent in sorted(_roster(root)):
        if agent == "lead":
            continue
        work = worktrees.path(root, agent) if agent in existing else wt.add(root, agent)
        config.provision(work, root)
        if agent not in existing:
            print(f"worktree for {agent}: {work}")
            made += 1
    if not made:
        print("all grunts already have a worktree")
    return OK


def _grunt_worktree(root, name, wt, notes):
    """The grunt's cwd. A repo with no commits has no HEAD to check out, so it
    gets the main tree and a warning -- `find` tasks work there, and
    `send --type build` refuses later on its own terms."""
    try:
        work = (worktrees.path(root, name) if name in set(wt.agents(root))
                else wt.add(root, name))
    except worktrees.WorktreeError as exc:
        notes.append(f"warning: no worktree for {name} ({exc}). "
                     f"find tasks work; build tasks will refuse.")
        return root
    config.provision(work, root)
    return work


def cmd_bootstrap(args, root, p=None):
    """Everything between an empty directory and a lead that can dispatch work.

    Idempotent by construction: each step asks the world what it is before
    changing it, so running `bootstrap` twice is running `up` twice.

    It does NOT read TEAMCHAT.md into the lead -- there is no way to do that
    from a subprocess. The `/teamup` skill exists for exactly that half.
    """
    p = p if p is not None else panes.Panes()
    wt = worktrees.Worktrees()

    # Pin the repo here: the bus lives WHERE YOU STARTED IT, never up the tree. The
    # notice (a nested `git init` done on the user's behalf) is suppressed only when
    # they asked for it explicitly with --here.
    actions, notice = _pin_repo_here(root, wt)
    if notice and not getattr(args, "here", False):
        print("\n" + notice, file=sys.stderr)

    if not bus.team_dir(root).exists() or args.force:
        actions += config.init(root, force=args.force,
                               copy_provider=_resolve_copy_provider(args))
    else:
        actions.append(f"bus already at {bus.team_dir(root)}")

    for line in actions:
        print(line)
    if shutil.which("team") is None:
        print(f"\nwarning: `team` is not on PATH. A grunt calls it, and gets it "
              f"from the pane env -- but you will want it too:\n"
              f"    ln -s {TEAM_BIN} ~/.local/bin/team", file=sys.stderr)

    busname = bus.resolve_bus_name(getattr(args, "bus", None))
    if busname != bus.TEAM:
        # A named bus. The lead's later shell commands need to know which one; the
        # cleanest handoff is one exported var its whole shell picks up, so
        # `team send`/`verify`/`wait` need no repeated --bus.
        print(f"\nnamed bus {busname} is live. Adopt it in this shell so every "
              f"later `team` command targets it:\n    export TEAM_BUS={busname}",
              file=sys.stderr)

    return cmd_up(args, root, p=p)


def cmd_grunt_add(args, root, p=None):
    """Create one grunt: worktree, pane, log, death hook, roster entry.

    In that order. The pane must be launched *in* its worktree (a pane rooted in
    the main tree makes qwen's file tools address the main tree), so the
    worktree has to exist first. The roster entry is written before the readiness
    wait: a grunt whose TUI never draws still owns a pane, and the lead needs to
    be able to find and remove it.
    """
    p = p if p is not None else panes.Panes()
    roster = _roster(root)

    name = args.name or _next_grunt(roster)
    if name == "lead":
        raise StateError("'lead' is not a grunt name")
    if name in roster:
        raise StateError(f"{name!r} is already in the roster. "
                         f"Run `team grunt rm {name}` first.")

    target = args.window or os.environ.get("TMUX_PANE")
    if not target:
        raise StateError(
            "not inside tmux, and no --window given. There is no way to guess "
            "which window you mean, and splitting the wrong one is worse than "
            "refusing."
        )
    if not shutil.which(args.command):
        raise StateError(f"{args.command!r} is not on PATH")

    notes: list[str] = []
    wt = worktrees.Worktrees()
    work = _grunt_worktree(root, name, wt, notes)

    pane = p.split(target, work, args.command, env=_grunt_env())
    try:
        p.pipe_pane(pane, bus.team_dir(root) / "logs" / f"{name}.log")
        p.install_death_hook(pane, panes.write_death_hook(
            TEAM_BIN, root, name, bus_name=bus.resolve_bus_name()))
    except Exception:
        # The pane exists but is not in the roster, so nothing else will ever
        # find it. An orphaned agent left running in a worktree is worse than
        # the error that got us here.
        p.kill(pane)
        raise

    roster[name] = {"pane": pane, "backend": args.command, "cwd": str(work)}
    _write_roster(root, roster)

    for note in notes:
        print(note, file=sys.stderr)
    p.wait_ready(pane, timeout=args.timeout)
    print(f"{name}: pane {pane} in {work}")
    return OK


def cmd_grunt_rm(args, root, p=None):
    p = p if p is not None else panes.Panes()
    roster = _roster(root)
    if args.name == "lead" or args.name not in roster:
        raise StateError(f"no grunt {args.name!r} in the roster")

    wt = worktrees.Worktrees()
    has_worktree = args.name in set(wt.agents(root))
    if has_worktree and not args.force:
        dirty = wt.dirty(root, args.name)
        if dirty:
            raise StateError(
                f"{args.name} holds {len(dirty)} uncollected file(s), e.g. "
                f"{worktrees.porcelain_rel(dirty[0])}. Run `team collect <tid>`, "
                f"or pass --force to discard them."
            )

    # A task left open would make a re-added grunt of the same name refuse its
    # first dispatch ("already has open task"), and `wait --task` on it would
    # never return. Same bookkeeping as --supersede.
    open_tid = bus.open_task(root, args.name)
    if open_tid:
        bus.mark_dead(root, open_tid)

    p.kill(roster[args.name]["pane"])
    if has_worktree:
        wt.remove(root, args.name)
        wt.prune(root)
    del roster[args.name]
    _write_roster(root, roster)
    print(f"removed {args.name}" + (f" (task {open_tid} marked dead)" if open_tid else ""))
    return OK


def cmd_up(args, root, p=None):
    """Register the lead and add `n` grunts.

    Inside tmux the lead is the pane you are in -- the lead runs this through
    its own shell, so $TMUX_PANE is its pane. Outside tmux a session is created.
    Grunts default to 0: they are spawned on demand with `team grunt add`.
    """
    p = p if p is not None else panes.Panes()
    roster = _roster(root)
    if roster and not args.force:
        raise StateError(
            f"roster.json already names {', '.join(sorted(roster))}. "
            f"`team up` would orphan those panes. Pass --force to overwrite it."
        )

    lead = args.lead_pane or os.environ.get("TMUX_PANE")
    if os.environ.get("TMUX") and not lead:
        raise StateError("$TMUX is set but $TMUX_PANE is not; pass --lead-pane <id>")

    if not lead:
        lead = p.new_session(args.session, root, args.lead_command)
        print(f"session {args.session} created. Attach: tmux attach -t {args.session}")

    # Pipe before registering. The lead's pane already exists, so a failure here
    # orphans nothing -- but a roster written first would make the retry demand
    # --force to overwrite the half-finished state it left behind.
    p.pipe_pane(lead, bus.team_dir(root) / "logs" / "lead.log")
    _write_roster(root, {"lead": {"pane": lead, "backend": args.lead_command,
                                  "cwd": str(root)}})
    print(f"lead: pane {lead}")

    for _ in range(args.grunts):
        cmd_grunt_add(argparse.Namespace(
            name=None, window=lead, command=args.command,
            timeout=args.timeout), root, p=p)

    if not args.grunts:
        print("no grunts yet — the lead spawns them with `team grunt add`")
    print(f"\nIn the lead pane, paste this once:\n    Read {BRIEF} and follow it.")
    return OK


def cmd_collect(args, root):
    for line in collect.collect(root, args.task):
        print(line)
    return OK


def cmd_down(args, root, p=None):
    # `killer` is injected, not imported: config.py must keep working if tmux
    # were swapped out. Without it, `down` would delete every grunt's worktree
    # and leave its qwen running in a directory that no longer exists.
    # `down` deletes worktrees and kills panes -- doing that to a bus your pane
    # did not start would tear down another project. Refuse a foreign bus.
    api.assert_own_bus(root)
    p = p if p is not None else panes.Panes()
    lines = config.down(root, force=args.force, killer=p.kill)
    for line in lines:
        print(line)
    if not lines:
        # config.down does nothing when there is no bus at cwd. Say so out loud: an
        # empty tool result reads to the lead as success -- it narrated "bus torn
        # down" over a bus (at $HOME, via the old walk-up) that was never touched.
        print(f"nothing to tear down: no bus at {bus.team_dir(root)}")
    return OK


def cmd_send(args, root, p=None):
    # `api.send` raises PaneError; the exit-code mapping stays here, where the
    # other exit codes live, rather than leaking into the shared core.
    try:
        r = api.send(root, args.agent, question=args.question,
                     scope=args.scope or [], supersede=args.supersede,
                     allow_dirty=args.allow_dirty, reply=args.reply,
                     text=args.text, kind=args.type, create=args.create,
                     replace=args.replace, build_dir=args.build_dir,
                     build_cmd=args.build_cmd, attach_dir=args.attach, p=p)
    except panes.PaneError as exc:
        print(exc, file=sys.stderr)
        return PANE_GONE
    # Name the bus and pane, always. The misroute this guards against was silent
    # because dispatch only ever said "sent task NNN to grunt1" -- never WHERE.
    box = bus.team_dir(root)
    pane = api.pane_for(root, r.agent)
    if r.kind == "reply":
        print(f"replied {r.id} to {r.agent} (pane {pane}) in {box}")
    else:
        print(f"sent task {r.id} to {r.agent} (pane {pane}) in {box}")
    return OK


def cmd_wait(args, root):
    if args.for_target == "lead":
        msgs = wait.for_lead(root, timeout=args.timeout)
        if not msgs:
            print(f"TIMEOUT: no message for lead within {args.timeout}s")
            return TIMEOUT
        for m in msgs:
            print(_digest(m))
        return OK

    r = api.wait_tasks(root, args.task, timeout=args.timeout)
    for tid in r.sealed:
        reaped = (bus._try_read_obj(bus.result_path(root, tid)) or {}).get("sealed_by") == "reap"
        print(f"SEALED (reaped): {tid}" if reaped else f"SEALED: {tid}")
    for tid in r.superseded:
        print(f"SUPERSEDED: {tid}")
    for msg in r.blocked:
        # The reply command, spelled out. A lead that has to derive it will
        # instead go and do the work itself, which is the one thing this tool
        # exists to stop.
        print(f"BLOCKED: {msg['task']} ({msg['type']} {msg['id']}) {msg['body']}")
        print(f"  team send {msg['from']} --reply {msg['id']} \"<your answer>\"")
    for tid in r.timed_out:
        print(f"TIMEOUT: {tid}")
    if r.blocked:
        return BLOCKED
    return OK if r.ok else TIMEOUT


def cmd_inbox(args, root):
    # One corrupt file must not hide every other message. `ops._messages`
    # already skips these; the lead's own listing must agree.
    for path in sorted(bus.lead_inbox(root).glob("*.json")):
        msg = bus._try_read_obj(path)
        if msg is None:
            print(f"{path.stem} <unreadable>")
            continue
        print(_digest(msg))
    return OK


def cmd_show(args, root):
    print(bus.read_json(bus.lead_inbox(root) / f"{args.msg_id}.json")["body"])
    return OK


def cmd_log(args, root):
    path = bus.team_dir(root) / "logs" / f"{args.agent}.log"
    if not path.exists():
        print(f"no log for {args.agent}", file=sys.stderr)
        return REFUSED
    lines = log.render(path.read_text(errors="replace")).splitlines()
    print("\n".join(lines[-args.tail:] if args.tail else lines))
    return OK


def cmd_msg(args, root):
    mtype = "note" if args.note else "blocked" if args.blocked else "failed"
    mid = ops.post_message(root, args.agent, mtype, args.task, args.text)
    print(f"posted {mtype} {mid}")
    return OK


def cmd_result(args, root):
    if args.result_cmd == "add":
        ops.result_add(root, args.task, {
            "file": args.file, "line": args.line,
            "symbol": args.symbol, "evidence": args.evidence,
        })
        print(f"staged record for {args.task}")
        return OK
    if args.result_cmd == "answer":
        # Read from a file, never an argv string: a grunt types this into a
        # shell, and a quote or newline in the prose would truncate it silently.
        text = Path(args.from_file).read_text(encoding="utf-8")
        mid = ops.result_answer(root, args.task, text, args.agent)
        print(f"answered and sealed {args.task} ({len(text)} chars), announced as {mid}")
        return OK
    mid = ops.result_done(root, args.task, args.agent)
    print(f"task {args.task} already sealed" if mid is None
          else f"sealed {args.task}, announced as {mid}")
    return OK


def _open_tty():
    """The controlling terminal as a writable file, or None if this process has
    none. Split out so a test can substitute a buffer for `/dev/tty`.

    A lead runs `team answer` through its own shell, and its harness captures the
    command's stdout as context tokens. The controlling terminal is a separate
    device: in the lead's tmux pane it is the very screen the human is watching,
    but it is not the piped stdout the lead ingests. Writing the prose there puts
    the answer in front of the human at zero token cost to the lead.
    """
    try:
        return open("/dev/tty", "w")
    except OSError:
        return None


def cmd_answer(args, root, open_tty=_open_tty):
    text = api.answer(root, args.task)
    if text is None:
        print(f"task {args.task} has no sealed answer "
              f"(not an ask task, or not sealed yet)", file=sys.stderr)
        return REFUSED

    # An ask grunt's prose IS the deliverable; the lead's job is to put it in
    # front of the human, not to read it and re-type it. `team answer 007` renders
    # it to the terminal and hands the lead only a one-line marker, so the whole
    # answer never lands in the lead's context to be relayed. `--capture` is the
    # deliberate exception: pull the prose into context to QA a specific point.
    if args.capture:
        print(text)
        return OK

    tty = open_tty()
    if tty is None:
        # No controlling terminal (piped, headless, some MCP setups). There is
        # nowhere to render but stdout; fall back rather than drop the answer.
        print(text)
        return OK
    try:
        tty.write(text.rstrip("\n") + "\n")
        tty.flush()
    finally:
        tty.close()
    print(f"ANSWER {args.task} rendered to your terminal ({len(text)} chars) -- "
          f"not pulled into your context. It is the grunt's finished work, now on "
          f"screen for the human: leave it there, do not relay it. Re-run with "
          f"--capture only if you must read it yourself to QA a point.")
    return OK


def _records(root: Path, tid: str) -> list[dict]:
    path = bus.result_path(root, tid)
    return bus.read_json(path)["records"] if path.is_file() else []


def cmd_verify(args, root):
    r = api.verify_task(root, args.task)
    if r.kind == "ask":
        # `PASS` is reserved for a citation that survived re-reading the file.
        # An ask task carries no claim about any file, so there is nothing to
        # pass -- and saying PASS would teach the lead that prose was checked.
        print(f"ask {args.task}: NOTHING TO VERIFY — 0 citations. "
              f"An ask answer is not a claim about the code; read it with "
              f"`team answer {args.task}`.")
    elif r.kind == "build":
        print(buildverify.render(r.build))
        # No citations, or a task-level failure that left no sound tree to
        # resolve them against: `api.verify_task` returns [] for both.
        if r.verdicts:
            print(verify.render_table(args.task, r.verdicts))
    else:
        print(verify.render_table(args.task, r.verdicts))
        if args.show:
            print(json.dumps(api.task_records(root, args.task), indent=2))
    # Fail closed. A lead running `team verify $t && use_result` must not
    # trust a fabricated citation because it forgot a flag. Measured grunt
    # accuracy: 2/5, 0/4, 3/4. `--lenient` is the deliberate opt-out.
    return OK if (args.lenient or r.ok) else VERIFY_FAIL


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="team")
    ap.add_argument("--version", action="version",
                    version=f"team {__version__}")
    ap.add_argument("--root", default=None,
                    help="bus root (default: nearest ancestor holding a bus dir)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # `--bus <slug>` on any verb a lead runs directly. Shared via a parent parser
    # rather than the top parser so it reads `team init --bus auth` (after the
    # verb), matching --root's mirror image `team --root X init` (before it), and
    # so a verb the lead never types can simply omit it. Empty/`default` -> the
    # plain `.team`; anything else -> `.team-<slug>`.
    bus_parent = argparse.ArgumentParser(add_help=False)
    bus_parent.add_argument(
        "--bus", default=None, metavar="SLUG",
        help="address the named bus .team-<slug> (default: .team, or $TEAM_BUS)")

    # PLUMBING beneath `bootstrap` (the porcelain): a paneless bus for named buses
    # and headless e2e. No `help=`, so it carries no description line in `--help` --
    # only `bootstrap` does -- and a user scanning the help meets one setup verb.
    p = sub.add_parser("init", parents=[bus_parent])
    p.add_argument("--force", action="store_true")
    _add_provider_flags(p)
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("down", parents=[bus_parent])
    p.add_argument("--force", action="store_true",
                   help="discard uncollected grunt work in the worktrees")
    p.set_defaults(fn=cmd_down)

    p = sub.add_parser("brief")
    p.add_argument("--show", action="store_true", help="print the brief, not its path")
    p.set_defaults(fn=cmd_brief)

    p = sub.add_parser("collect", parents=[bus_parent])
    p.add_argument("task")
    p.set_defaults(fn=cmd_collect)

    p = sub.add_parser("worktree").add_subparsers(dest="wtcmd", required=True)
    p.add_parser("up", parents=[bus_parent]).set_defaults(fn=cmd_worktree_up)

    for verb, fn, helptext in (
        ("up", cmd_up, "register the lead pane; optionally add grunts"),
        ("bootstrap", cmd_bootstrap, "the setup verb: git init + commit + bus + "
                                     "become lead, all pinned to THIS directory"),
    ):
        p = sub.add_parser(verb, help=helptext, parents=[bus_parent])
        p.add_argument("grunts", nargs="?", type=int, default=0)
        p.add_argument("--session", default=DEFAULT_SESSION)
        p.add_argument("--lead-pane", dest="lead_pane", default=None,
                       help="override $TMUX_PANE")
        p.add_argument("--force", action="store_true",
                       help="overwrite a live roster (and, for bootstrap, the bus)")
        p.add_argument("--timeout", type=float, default=60.0)
        # The two agent binaries. Named, not hardcoded, so the pane and roster
        # machinery can be tested without booting a real model -- and so a lead
        # can point at a wrapper. Nothing branches on which one is chosen.
        p.add_argument("--lead-command", dest="lead_command", default="claude")
        p.add_argument("--command", default="qwen", help="grunt binary")
        if verb == "bootstrap":
            p.add_argument("--here", action="store_true",
                           help="silence the notice when THIS directory sits inside "
                                "another git repo: bootstrap nests a repo here and "
                                "pins the bus here either way; --here says you meant "
                                "that, so the explanatory NOTE is suppressed")
            _add_provider_flags(p)
        p.set_defaults(fn=fn)

    g = sub.add_parser("grunt").add_subparsers(dest="gcmd", required=True)
    a = g.add_parser("add", parents=[bus_parent])
    a.add_argument("name", nargs="?", default=None)
    a.add_argument("--window", default=None, help="tmux target; default $TMUX_PANE")
    a.add_argument("--command", default="qwen")
    a.add_argument("--timeout", type=float, default=60.0)
    a.set_defaults(fn=cmd_grunt_add)
    r = g.add_parser("rm", parents=[bus_parent])
    r.add_argument("name")
    r.add_argument("--force", action="store_true", help="discard uncollected work")
    r.set_defaults(fn=cmd_grunt_rm)

    p = sub.add_parser("send", parents=[bus_parent])
    p.add_argument("agent")
    p.add_argument("--question", default="")
    p.add_argument("--scope", nargs="*")
    p.add_argument("--supersede", action="store_true")
    p.add_argument("--allow-dirty", action="store_true", dest="allow_dirty",
                   help="find: dispatch even though a --scope path is "
                        "uncommitted; the grunt reads the committed version")
    p.add_argument("--reply", metavar="MSG_ID")
    p.add_argument("--type", choices=["find", "build", "ask"], default="find",
                   help="ask: a question with no source; the grunt answers from "
                        "its own knowledge and takes no --scope")
    p.add_argument("--create", action="extend", nargs="+", default=[],
                   metavar="PATH", help="build: files the grunt may create")
    p.add_argument("--replace", action="store_true",
                   help="build: let the lead delete the --create paths first")
    p.add_argument("--build-dir", default=".", dest="build_dir")
    # default=None, not the list: `action="extend"` APPENDS to its default, so
    # a non-empty default turns `--build-cmd make` into
    # ["dotnet","build",...,"make"]. Same trap as `--task` once had.
    p.add_argument("--build-cmd", action="extend", nargs="+", dest="build_cmd",
                   default=None, help="build: argv, never a shell string")
    p.add_argument("--attach", default=None, dest="attach", metavar="DIR",
                   help="build: a dir mirroring the --create paths with their "
                        "exact bytes; the grunt copies them verbatim instead of "
                        "retyping (bypasses model reconstruction)")
    p.add_argument("text", nargs="?", default="")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("wait", parents=[bus_parent])
    p.add_argument("--for", dest="for_target", choices=["lead"], default=None)
    # action="extend": `--task 001 --task 002` must wait on BOTH. With a
    # bare nargs="*" the second flag silently replaced the first, so the
    # lead waited on one task while believing it waited on two.
    p.add_argument("--task", action="extend", nargs="+", default=[])
    p.add_argument("--timeout", type=float, default=3600.0)
    p.set_defaults(fn=cmd_wait)

    sub.add_parser("inbox", parents=[bus_parent]).set_defaults(fn=cmd_inbox)

    p = sub.add_parser("show", parents=[bus_parent])
    p.add_argument("msg_id"); p.set_defaults(fn=cmd_show)

    p = sub.add_parser("log", parents=[bus_parent])
    p.add_argument("agent"); p.add_argument("--tail", type=int, default=0)
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("msg")
    p.add_argument("--agent", default=_default_agent())
    p.add_argument("--note", action="store_true")
    p.add_argument("--blocked", action="store_true")
    p.add_argument("--failed", action="store_true")
    p.add_argument("--task", required=True)
    p.add_argument("text")
    p.set_defaults(fn=cmd_msg)

    p = sub.add_parser("result")
    rsub = p.add_subparsers(dest="result_cmd", required=True)
    a = rsub.add_parser("add")
    a.add_argument("--task", required=True)
    a.add_argument("--file", required=True)
    a.add_argument("--line", type=int, required=True)
    a.add_argument("--symbol", required=True)
    a.add_argument("--evidence", required=True)
    ans = rsub.add_parser("answer", help="ask: stage a prose answer from a file")
    ans.add_argument("--task", required=True)
    ans.add_argument("--from", dest="from_file", required=True, metavar="FILE",
                     help="a file holding the answer; never an argv string")
    ans.add_argument("--agent", default=_default_agent())
    d = rsub.add_parser("done")
    d.add_argument("--task", required=True)
    d.add_argument("--agent", default=_default_agent())
    p.set_defaults(fn=cmd_result)

    p = sub.add_parser("answer", help="render a sealed ask task's answer to your "
                       "terminal (not into your context)", parents=[bus_parent])
    p.add_argument("task")
    p.add_argument("--capture", action="store_true",
                   help="print the prose to stdout instead of the terminal, so it "
                        "enters your context -- only to QA a specific point")
    p.set_defaults(fn=cmd_answer)

    p = sub.add_parser("verify", parents=[bus_parent])
    p.add_argument("task")
    p.add_argument("--show", action="store_true")
    p.add_argument("--lenient", action="store_true",
                   help="exit 0 even when a citation fails verification")
    p.set_defaults(fn=cmd_verify)

    return ap


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        # A `--bus` flag becomes `$TEAM_BUS` for the rest of this process, so it
        # wins over any inherited env (the flag is the most explicit choice) and
        # reaches every `bus.resolve_bus_name()` downstream -- root resolution,
        # `team_dir`, and the env we hand to grunt panes -- through one channel.
        bus_flag = getattr(args, "bus", None)
        if bus_flag is not None:
            os.environ["TEAM_BUS"] = bus.resolve_bus_name(bus_flag)
        if args.cmd in NO_ROOT_COMMANDS:
            return args.fn(args, None)
        if args.root:
            root = Path(args.root).resolve()
        elif args.cmd in CWD_COMMANDS:          # bootstrap/init/down -> cwd, verbatim
            root = Path.cwd().resolve()
        else:
            root = bus.bus_root()
        return args.fn(args, root)
    except SchemaError as exc:
        print(f"schema violation: {exc}", file=sys.stderr)
        return REFUSED
    except (StateError, bus.BusError, worktrees.WorktreeError) as exc:
        # A failing git worktree operation is a refusal, not a crash: `team up`
        # runs `worktree up` in repos that may have no commits yet, and prints
        # its own warning on a non-zero exit. A traceback there is noise.
        print(f"refused: {exc}", file=sys.stderr)
        return REFUSED
    except panes.PaneError as exc:
        print(f"pane error: {exc}", file=sys.stderr)
        return PANE_GONE
    except FileNotFoundError as exc:
        # A typo'd id, or `send` before `init`. Exiting 1 here would be
        # indistinguishable from VERIFY_FAIL, and a traceback is never a
        # user-facing error.
        print(f"refused: no such file: {exc.filename}", file=sys.stderr)
        return REFUSED
    except (OSError, json.JSONDecodeError) as exc:
        print(f"refused: unreadable bus file: {exc}", file=sys.stderr)
        return REFUSED
