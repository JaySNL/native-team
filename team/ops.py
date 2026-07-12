"""Bus operations: compose tasks, exchange messages, seal results.

This module is the only place besides `panes.py` that is allowed to know
`panes.py` exists (see the module docstring there) -- but it does not import
it: composing a task file and marking an old one dead is pure bus bookkeeping.
Actually *cancelling* a superseded grunt's in-flight turn is done elsewhere,
by whichever caller drives both this module and `panes.Panes` together (the
`--supersede` CLI path clears the target pane, whose leading `Escape` is what
halts the turn -- see `panes.py`'s docstring). Keep tmux vocabulary out of
this module.
"""
from pathlib import Path

from team import bus, protocol, schema
from team.config import StateError


def _messages(root: Path) -> list[dict]:
    """Read the lead's inbox, skipping any file that will not parse as an
    object. One corrupt file must not brick `reply` for every agent -- the
    bus is a directory a human can edit, and `bus.open_task` already tolerates
    exactly this. Ordering is by the three-digit id, never mtime.
    """
    box = bus.lead_inbox(root)
    objs = (bus._try_read_obj(p) for p in sorted(box.glob("*.json")))
    return [o for o in objs if o is not None]


def last_message_from(root: Path, agent: str) -> dict | None:
    mine = [m for m in _messages(root) if m.get("from") == agent]
    return mine[-1] if mine else None


def _read_staging(path: Path) -> dict:
    """A staging file can be hand-written by a grunt, bypassing `result_add`.
    Surface corruption as StateError, not a raw JSONDecodeError.
    """
    obj = bus._try_read_obj(path)
    if obj is None:
        raise StateError(f"staging file {path} is unreadable or is not a JSON object")
    records = obj.get("records")
    if not isinstance(records, list):
        raise StateError(f"staging file {path} has no 'records' list")
    return obj


def _refuse_stale_scope(root: Path, agent: str, scope: list[str], wt) -> None:
    """A grunt reads its worktree, a detached checkout of HEAD. `verify` reads
    the main tree. A scope path that differs between them makes the grunt cite
    the file it actually read and `verify` call the citation fabricated.

    Only checked when the agent has a worktree: without one the pane fell back
    to the main root, reads the live file, and there is nothing to be stale.
    """
    from team import worktrees
    if not scope or not worktrees.path(root, agent).is_dir():
        return
    wt = wt if wt is not None else worktrees.Worktrees()
    lines = wt.main_dirty(root, scope)
    if lines:
        files = ", ".join(worktrees.porcelain_rel(l) for l in lines[:5])
        raise StateError(
            f"scope is dirty in the main tree ({files}). {agent} reads a "
            f"checkout of HEAD, so it would cite the committed file while "
            f"`team verify` reads yours. Commit, or pass --allow-dirty."
        )


def compose_task(root: Path, agent: str, question: str,
                 scope: list[str], supersede: bool = False,
                 allow_dirty: bool = False, wt=None) -> str:
    if not allow_dirty:
        _refuse_stale_scope(root, agent, scope, wt)

    open_tid = bus.open_task(root, agent)
    if open_tid and not supersede:
        raise StateError(
            f"{agent} already has open task {open_tid}. "
            f"Pass --supersede to kill it, or wait for its result."
        )
    if open_tid:
        # Marking the old id dead is the bus-side half of "supersede". A late
        # result for `open_tid` will now be rejected by result_done. Halting
        # the grunt's actual in-flight turn is a tmux concern (Escape via
        # panes.clear_context) and happens in the caller that wires this
        # module to panes.Panes, not here.
        bus.mark_dead(root, open_tid)

    tid = bus.alloc_id(root)
    bus.write_json(bus.task_path(root, agent, tid), {
        "id": tid,
        "kind": "task",
        "to": agent,
        "from": "lead",
        "question": question,
        "scope": scope,
        "protocol": protocol.task_body(tid, question, scope),
    })
    return tid


def compose_ask_task(root: Path, agent: str, question: str,
                     supersede: bool = False) -> str:
    """A question with no source. The grunt answers from its own weights.

    An ask task takes NO scope, and that is a fence rather than an omission.
    Naming a file is making a claim about that file, and a claim about a file
    is checkable -- so it belongs in a `find` task, where `verify` re-opens the
    file and checks it. Without this fence, `--type ask` becomes the way to
    launder an unverifiable answer about the codebase past the verifier, which
    is exactly what `--lenient` was refused for.
    """
    open_tid = bus.open_task(root, agent)
    if open_tid and not supersede:
        raise StateError(
            f"{agent} already has open task {open_tid}. "
            f"Pass --supersede to kill it, or wait for its result."
        )
    if open_tid:
        bus.mark_dead(root, open_tid)

    tid = bus.alloc_id(root)
    bus.write_json(bus.task_path(root, agent, tid), {
        "id": tid,
        "kind": "ask",
        "to": agent,
        "from": "lead",
        "question": question,
        "scope": [],
        "protocol": protocol.ask_body(tid, question),
    })
    return tid


def reply(root: Path, agent: str, msg_id: str, text: str) -> str:
    """Send a follow-up to `agent`, only when it is idle at its prompt.

    Gated on `agent`'s last message being of type "blocked" for two
    independent reasons, both load-bearing -- neither is sufficient alone:

    1. Protocol discipline: a grunt has exactly one channel out (`team msg`)
       and is expected to wait for an answer after `--blocked`. Replying to
       anything else (a `result`, a `note`, or no message at all) means the
       lead is answering a question that was never asked.
    2. Safety: delivering a reply sends `Escape` first (see `panes.py`), which
       cancels an in-flight qwen turn. A `blocked` grunt is idle at its
       prompt, so there is no turn to cancel. Loosening this guard to permit
       replying to a *working* grunt would silently kill its in-progress work
       the moment the reply is delivered.
    """
    last = last_message_from(root, agent)
    if last is None or last["type"] != "blocked":
        raise StateError(
            f"{agent}'s last message is "
            f"{'nothing' if last is None else last['type']!r}, not 'blocked'. "
            f"Only a blocked agent is idle at its prompt and safe to send to."
        )
    rid = bus.alloc_id(root)
    bus.write_json(bus.task_path(root, agent, rid), {
        "id": rid,
        "kind": "reply",
        "to": agent,
        "from": "lead",
        "in_reply_to": msg_id,
        "task": last["task"],
        "body": text,
    })
    return rid


def post_message(root: Path, sender: str, mtype: str, task: str, body: str) -> str:
    mid = bus.alloc_id(root)
    msg = {"id": mid, "from": sender, "type": mtype, "task": task, "body": body}
    schema.validate_message(msg)
    bus.write_json(bus.lead_inbox(root) / f"{mid}.json", msg)
    return mid


def result_add(root: Path, tid: str, rec: dict) -> None:
    schema.validate_record(rec)
    if bus.result_path(root, tid).exists():
        # Measured, task 013: a grunt ran `done`, then `add`, then `done`. The
        # second `done` was refused, but the `add` had already re-created a
        # staging file for a sealed task -- evidence appearing behind the back
        # of a lead that had already run `verify`. Write-once means both ends.
        raise StateError(f"task {tid} is already sealed; it takes no more records")
    path = bus.staging_path(root, tid)
    records = _read_staging(path)["records"] if path.exists() else []
    records.append(rec)
    bus.write_json(path, {"task": tid, "records": records})


def result_answer(root: Path, tid: str, text: str) -> None:
    """Stage an ask task's prose answer.

    Takes text already read from a file, never an argv string: a grunt types
    its commands into a shell inside a TUI, and a multi-paragraph answer
    carrying a quote or a newline would be truncated at the first one --
    silently, which is the failure mode this project exists to refuse.
    """
    if not text.strip():
        raise StateError(f"task {tid}: the answer is empty; nothing to stage")
    if bus.result_path(root, tid).exists():
        raise StateError(f"task {tid} is already sealed; it takes no more answers")
    path = bus.staging_path(root, tid)
    staged = _read_staging(path) if path.exists() else {"task": tid, "records": []}
    staged["answer"] = text
    bus.write_json(path, staged)


def task_kind(root: Path, agent: str, tid: str) -> str:
    """`find` | `build` | `ask`, read from the task file the lead wrote."""
    path = bus.task_path(root, agent, tid)
    if not path.is_file():
        return "find"          # hand-driven bus, or a reply; assume citations
    kind = bus.read_json(path).get("kind")
    return {"ask": "ask", "build": "build"}.get(kind, "find")


def result_done(root: Path, tid: str, agent: str) -> str:
    """Seal staged records into `results/`, then announce -- never the other
    way around. A lead woken by the announcement message must always find
    the result already readable on disk; reversing this order would let the
    lead race the seal and read a task that looks done but has no result
    file yet.
    """
    if bus.is_dead(root, tid):
        raise StateError(f"task {tid} was superseded; its result is rejected")
    if bus.result_path(root, tid).exists():
        raise StateError(f"task {tid} is already sealed; results are write-once")

    kind = task_kind(root, agent, tid)
    staging = bus.staging_path(root, tid)
    # A build task's proof is the build itself -- `verify` re-runs it. Citations
    # into the built file are optional, and for a verbatim --attach task there is
    # nothing authored to cite, so the grunt can never stage a record. Let a build
    # seal on the build alone, with no staging file at all. find/ask still must
    # stage their evidence: a find with no citations proved nothing. (Measured: a
    # temp-0 grunt copied bytes, built clean, then spiralled -- it was told to
    # `result done`, which refused for want of a citation it had no basis to make.)
    if not staging.exists():
        if kind != "build":
            raise StateError(_nothing_to_seal(tid, kind))
        payload = {"records": []}
    else:
        payload = _read_staging(staging)
    answer = payload.get("answer")
    records = payload.get("records") or []

    # The two kinds seal on different evidence, and neither accepts the
    # other's. Letting an ask task seal citations, or a find task seal prose,
    # would make `verify` answer a question it was never asked.
    if kind == "ask":
        if records:
            raise StateError(
                f"task {tid} is an ask task: it seals an answer, not citations. "
                f"A claim about a file belongs in a find task, where `verify` "
                f"re-opens the file and checks it."
            )
        if not answer:
            raise StateError(_nothing_to_seal(tid, kind))
    else:
        if answer:
            raise StateError(
                f"task {tid} is a {kind} task: prose is not a report. Cite it "
                f"with `team result add`, or post it with "
                f"`team msg --note --task {tid} \"...\"`."
            )
        if kind == "find" and not records:
            raise StateError(_nothing_to_seal(tid, kind))

    # Re-validate even though result_add already validated each record on the
    # way in: a staging file can also be written by hand, bypassing
    # result_add entirely. Never seal a record this module hasn't checked.
    for rec in records:
        schema.validate_record(rec)
    payload["kind"] = kind
    payload["agent"] = agent

    # Seal before announce: the lead must never wake to a result that
    # does not yet exist on disk.
    bus.write_json(bus.result_path(root, tid), payload)
    if staging.exists():
        staging.unlink()

    if kind == "ask":
        return post_message(root, agent, "result", tid,
                            f"answer sealed; read it with `team answer {tid}`")
    if kind == "build" and not records:
        return post_message(root, agent, "result", tid,
                            f"build sealed; run `team verify {tid}`")
    return post_message(root, agent, "result", tid,
                        f"{len(records)} record(s) sealed; run `team verify {tid}`")


def _nothing_to_seal(tid: str, kind: str) -> str:
    """The reader of this message is a 30B model with one shot at recovering.

    Measured: told only "no staged records; nothing to seal", a grunt with
    nothing to cite searched the whole repo for its subject, three times, then
    blocked. It never learned what the exits were, because nothing named them.
    """
    if kind == "ask":
        return (f"task {tid} has no staged answer; nothing to seal. Write your "
                f"answer to ANSWER.md, then: "
                f"team result answer --task {tid} --from ANSWER.md")
    return (f"task {tid} has no staged records; nothing to seal. Add a citation "
            f"with `team result add --task {tid} --file <path> --line <n> "
            f"--symbol <name> --evidence '<the exact source line>'`. If the "
            f"answer is not in your scope, do NOT go looking elsewhere: "
            f"team msg --blocked --task {tid} \"why you cannot proceed\"")


def _porcelain(root: Path, agent: str, wt) -> list[str]:
    return sorted(wt.dirty(root, agent))


def compose_build_task(root: Path, agent: str, question: str,
                       create: list[str], build_dir: str,
                       build_cmd: list[str], replace: bool = False,
                       attach_dir: str = None, wt=None) -> str:
    """Dispatch a task that writes code, and record what it was allowed to write.

    The snapshot is written *before* the task is announced, so it is the lead's
    statement of intent rather than the grunt's account of what it did. `verify`
    and `collect` read it; neither asks the worktree to describe itself.
    """
    from team import worktrees
    wt = wt if wt is not None else worktrees.Worktrees()

    if not create:
        raise StateError("a build task must declare at least one --create path")

    work = worktrees.path(root, agent)
    if not work.is_dir():
        raise StateError(
            f"no worktree for {agent!r}. Run `team worktree up` -- a build task "
            f"cannot be contained in a tree the lead and other grunts share."
        )

    # Build outputs must be invisible to the containment check. `-uall` skips
    # gitignored paths, so if bin/obj are not ignored the first compile emits
    # hundreds of untracked files and every build task fails containment for
    # ever. Refuse now, with the reason, rather than let that be debugged later.
    for out in ("obj", "bin"):
        # Trailing slash is load-bearing. `.gitignore`'s `obj/` matches only a
        # directory, and `git check-ignore probe/obj` on a path that does not
        # exist yet cannot know it would be one -- so it reports "not ignored"
        # for a repo that ignores it perfectly well. `probe/obj/` matches.
        rel = f"{build_dir}/{out}/" if build_dir not in ("", ".") else f"{out}/"
        if not wt.is_ignored(root, agent, rel):
            raise StateError(
                f"{rel} is not gitignored in the worktree, so build output "
                f"would be indistinguishable from the grunt's work. Add it to "
                f".gitignore and commit, then re-send."
            )

    resolved = []
    for rel in create:
        target = (work / rel).resolve()
        if not target.is_relative_to(work.resolve()):
            raise StateError(f"--create path escapes the worktree: {rel}")

        # The main tree, before the grunt runs. `verify_build`'s ESCAPED check
        # reads "this declared path must not exist in the main tree" -- which is
        # only sound if it did not already exist when the task was dispatched.
        # Refusing here is also what makes `collect` able to promise it will
        # never overwrite. `--replace` deletes the grunt's stale copy, never the
        # lead's file.
        outside = (root / rel).resolve()
        if outside.is_relative_to(root.resolve()) and outside.exists():
            raise StateError(
                f"--create path already exists in the main tree: {rel}. "
                f"`team collect` would refuse to overwrite it. Move it aside "
                f"or delete it yourself, then re-send."
            )

        if target.exists():
            if not replace:
                raise StateError(
                    f"--create path already exists: {rel}. A grunt never "
                    f"modifies an existing file. Pass --replace to have the "
                    f"lead delete it first, or name a different path."
                )
            target.unlink()
        resolved.append(rel)

    # Verbatim-attach: the lead ships the exact bytes as files under attach_dir,
    # mirroring the create paths, and the grunt copies them into place instead
    # of retyping them. This is the fix for the measured failure where the
    # agentic harness pulls a small model back to an idiomatic-template prior on
    # a literal-transcription task -- routing the bytes through a shell `cp`
    # bypasses model reconstruction entirely. Staged under `.attach/` (a
    # PROVISIONED path, so containment does not read it as the grunt's work).
    if attach_dir is not None:
        import shutil
        src_root = Path(attach_dir).resolve()
        stage_root = work / ".attach"
        for rel in resolved:
            src = src_root / rel
            if not src.is_file():
                raise StateError(
                    f"--attach given but no staged file for --create path {rel} "
                    f"(looked for {src}). Every create path needs its exact bytes "
                    f"under the attach dir, mirroring the same relative path."
                )
            dst = stage_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    tid = bus.alloc_id(root)
    bus.write_json(bus.snapshot_path(root, tid), {
        "task": tid,
        "agent": agent,
        "create": resolved,
        "build_dir": build_dir,
        "build_cmd": build_cmd,
        "tree": _porcelain(root, agent, wt),
    })
    bus.write_json(bus.task_path(root, agent, tid), {
        "id": tid,
        "kind": "build",
        "to": agent,
        "from": "lead",
        "question": question,
        "scope": [],
        "protocol": protocol.build_body(
            tid, question, str(work), resolved, build_dir, build_cmd,
            attach=attach_dir is not None),
    })
    return tid
