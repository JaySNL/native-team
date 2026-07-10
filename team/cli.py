"""Argument parsing, wiring, and the exit-code contract.

  0 ok · 1 verify FAIL under --strict · 2 pane gone
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
    for tid in sealed:
        print(f"SEALED: {tid}")
    for tid in missing:
        print(f"TIMEOUT: {tid}")
    return TIMEOUT if missing else OK


def cmd_inbox(args, root):
    for path in sorted(bus.lead_inbox(root).glob("*.json")):
        print(_digest(bus.read_json(path)))
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
    failed = verify.any_failed(verdicts)
    return VERIFY_FAIL if (failed and args.strict) else OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="team")
    ap.add_argument("--root", default=None, help="repo root (default: discover via .git)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init"); p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    sub.add_parser("down").set_defaults(fn=cmd_down)

    p = sub.add_parser("send")
    p.add_argument("agent")
    p.add_argument("--new-task", dest="new_task", action="store_true")
    p.add_argument("--question", default="")
    p.add_argument("--scope", nargs="*")
    p.add_argument("--supersede", action="store_true")
    p.add_argument("--reply", metavar="MSG_ID")
    p.add_argument("text", nargs="?", default="")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("wait")
    p.add_argument("--for", dest="for_target", choices=["lead"], default=None)
    p.add_argument("--task", nargs="*", default=[])
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
    p.add_argument("--strict", action="store_true")
    p.set_defaults(fn=cmd_verify)

    return ap


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = Path(args.root).resolve() if args.root else bus.repo_root()
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
