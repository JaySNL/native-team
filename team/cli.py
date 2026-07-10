"""Argument parsing, wiring, and the exit-code contract.

  0 ok · 1 verify FAIL (unless --lenient) · 2 pane gone
  3 refused (schema violation or invalid state) · 4 timeout
"""
import argparse
import json
import sys
from pathlib import Path

from team import bus, config, log, ops, panes, verify, wait
from team.config import StateError
from team.schema import SchemaError

OK, VERIFY_FAIL, PANE_GONE, REFUSED, TIMEOUT = 0, 1, 2, 3, 4

# `init` runs before a bus exists and `down` destroys one, so both locate the
# repo by `.git`. Every other verb addresses an existing bus and must find it by
# `.team`: a grunt running a build task sits in a git worktree under
# `.team/work/<agent>`, and `repo_root` would stop at that worktree's own `.git`
# file and address a bus that isn't there.
PRE_BUS_COMMANDS = frozenset({"init", "down"})


def _roster(root: Path) -> dict:
    return bus.read_json(bus.team_dir(root) / "roster.json")


def _pane_for(root: Path, agent: str) -> str:
    entry = _roster(root).get(agent)
    if not entry:
        raise StateError(f"no agent {agent!r} in roster.json")
    return entry["pane"]


def _digest(msg: dict) -> str:
    body = msg["body"].replace("\n", " ")
    if len(body) > 80:
        body = body[:77] + "..."
    return f"{msg['type']:<8} {msg['id']} from {msg['from']} task {msg['task']}: {body}"


def cmd_init(args, root):
    for line in config.init(root, force=args.force):
        print(line)
    return OK


def cmd_down(args, root):
    for line in config.down(root):
        print(line)
    return OK


def cmd_send(args, root):
    p = panes.Panes()
    pane = _pane_for(root, args.agent)
    if not p.exists(pane):
        print(f"pane {pane} for {args.agent} is gone", file=sys.stderr)
        return PANE_GONE

    if args.reply:
        rid = ops.reply(root, args.agent, args.reply, args.text)
        p.send_line(pane, f"do task {bus.task_path(root, args.agent, rid).relative_to(root)}")
        print(f"replied {rid} to {args.agent}")
        return OK

    tid = ops.compose_task(root, args.agent, args.question, args.scope or [],
                           supersede=args.supersede)
    p.clear_context(pane)
    p.send_line(pane, f"do task {bus.task_path(root, args.agent, tid).relative_to(root)}")
    print(f"sent task {tid} to {args.agent}")
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

    sealed, missing = wait.for_tasks(root, args.task, timeout=args.timeout)
    # A superseded task is resolved but never seals. Reporting it as neither
    # sealed nor timed out left `team wait` printing nothing and exiting 0.
    superseded = [t for t in args.task
                  if t not in sealed and t not in missing]
    for tid in sealed:
        print(f"SEALED: {tid}")
    for tid in superseded:
        print(f"SUPERSEDED: {tid}")
    for tid in missing:
        print(f"TIMEOUT: {tid}")
    return TIMEOUT if missing else OK


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
    mid = ops.result_done(root, args.task, args.agent)
    print(f"sealed {args.task}, announced as {mid}")
    return OK


def cmd_verify(args, root):
    payload = bus.read_json(bus.result_path(root, args.task))
    verdicts = verify.verify_records(root, payload["records"])
    print(verify.render_table(args.task, verdicts))
    if args.show:
        print(json.dumps(payload["records"], indent=2))
    # Fail closed. A lead running `team verify $t && use_result` must not
    # trust a fabricated citation because it forgot a flag. Measured grunt
    # accuracy: 2/5, 0/4, 3/4. `--lenient` is the deliberate opt-out.
    failed = verify.any_failed(verdicts)
    return OK if (args.lenient or not failed) else VERIFY_FAIL


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="team")
    ap.add_argument("--root", default=None, help="repo root (default: discover via .git)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init"); p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    sub.add_parser("down").set_defaults(fn=cmd_down)

    p = sub.add_parser("send")
    p.add_argument("agent")
    p.add_argument("--question", default="")
    p.add_argument("--scope", nargs="*")
    p.add_argument("--supersede", action="store_true")
    p.add_argument("--reply", metavar="MSG_ID")
    p.add_argument("text", nargs="?", default="")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("wait")
    p.add_argument("--for", dest="for_target", choices=["lead"], default=None)
    # action="extend": `--task 001 --task 002` must wait on BOTH. With a
    # bare nargs="*" the second flag silently replaced the first, so the
    # lead waited on one task while believing it waited on two.
    p.add_argument("--task", action="extend", nargs="+", default=[])
    p.add_argument("--timeout", type=float, default=3600.0)
    p.set_defaults(fn=cmd_wait)

    sub.add_parser("inbox").set_defaults(fn=cmd_inbox)

    p = sub.add_parser("show"); p.add_argument("msg_id"); p.set_defaults(fn=cmd_show)

    p = sub.add_parser("log")
    p.add_argument("agent"); p.add_argument("--tail", type=int, default=0)
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("msg")
    p.add_argument("--agent", default="grunt1")
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
    d = rsub.add_parser("done")
    d.add_argument("--task", required=True)
    d.add_argument("--agent", default="grunt1")
    p.set_defaults(fn=cmd_result)

    p = sub.add_parser("verify")
    p.add_argument("task")
    p.add_argument("--show", action="store_true")
    p.add_argument("--lenient", action="store_true",
                   help="exit 0 even when a citation fails verification")
    p.set_defaults(fn=cmd_verify)

    return ap


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.root:
            root = Path(args.root).resolve()
        elif args.cmd in PRE_BUS_COMMANDS:
            root = bus.repo_root()
        else:
            root = bus.bus_root()
        return args.fn(args, root)
    except SchemaError as exc:
        print(f"schema violation: {exc}", file=sys.stderr)
        return REFUSED
    except (StateError, bus.BusError) as exc:
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
