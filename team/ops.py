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


def compose_task(root: Path, agent: str, question: str,
                 scope: list[str], supersede: bool = False) -> str:
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
    path = bus.staging_path(root, tid)
    records = _read_staging(path)["records"] if path.exists() else []
    records.append(rec)
    bus.write_json(path, {"task": tid, "records": records})


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

    staging = bus.staging_path(root, tid)
    if not staging.exists():
        raise StateError(f"task {tid} has no staged records; nothing to seal")

    payload = _read_staging(staging)
    # Re-validate even though result_add already validated each record on the
    # way in: a staging file can also be written by hand, bypassing
    # result_add entirely. Never seal a record this module hasn't checked.
    for rec in payload["records"]:
        schema.validate_record(rec)
    payload["agent"] = agent

    # Seal before announce: the lead must never wake to a result that
    # does not yet exist on disk.
    bus.write_json(bus.result_path(root, tid), payload)
    staging.unlink()

    count = len(payload["records"])
    return post_message(root, agent, "result", tid,
                        f"{count} record(s) sealed; run `team verify {tid}`")
