"""An MCP server exposing the three verbs the lead's control flow depends on.

`TEAMCHAT.md` documents three traps, and all three are properties of the shell,
not of this tool: `team wait ...; echo` destroys `$?`; `argparse` exits `2`,
colliding with `PANE_GONE`; `send` prints an id the lead has to parse. A tool
call has no `$?` to destroy and returns a value rather than a line of text.

Newline-delimited JSON-RPC 2.0 on stdin/stdout, stdlib only. The wire shape
follows a known-good Claude Code MCP stdio server, matched against a build that
works rather than merely the documented spec.

NOTHING may write to stdout but `_send`. A stray `print` anywhere below is a
corrupt frame and a dead session; `api` returns strings and never prints, which
is why the rendering lives here.
"""
import json
import sys
from pathlib import Path

from team import api, bus, buildverify, panes, verify, worktrees
from team.config import StateError
from team.schema import SchemaError

NAME = "team"
VERSION = "1.0.0"
FALLBACK_PROTOCOL = "2025-06-18"

# A failed verification is a SUCCESSFUL call: the tool was asked whether the
# citations hold and answered "no". `isError` is for being unable to answer at
# all. Conflating them teaches the lead that `verify` "errored" and can be
# retried. It cannot. It reported.
REFUSALS = (StateError, SchemaError, bus.BusError, panes.PaneError,
            worktrees.WorktreeError, FileNotFoundError, KeyError, ValueError)

FAIL_BANNER = ("VERIFY FAILED — do not use these citations, do not open the "
               "file. Re-ask the grunt with a correction.")

TOOLS = [
    {
        "name": "team_send",
        "description": (
            "Send a task to a grunt. Clears its context first. Returns the "
            "task id -- never guess it: task ids and message ids share one "
            "counter, so the next task after 007 need not be 008."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string",
                          "description": "grunt name, e.g. grunt1"},
                "question": {"type": "string", "description": "ask for ONE thing"},
                "scope": {"type": "array", "items": {"type": "string"},
                          "description": "files or dirs the grunt should read. "
                                         "Advice, not a fence."},
                "supersede": {"type": "boolean",
                              "description": "cancel the grunt's current turn"},
                "allow_dirty": {"type": "boolean",
                                "description": "dispatch even though a scope path "
                                               "is uncommitted; the grunt reads "
                                               "the committed version"},
                "kind": {"type": "string", "enum": ["find", "build", "ask", "free"],
                         "description": "find (cite code, default), build "
                                        "(write code), or ask (answer a question "
                                        "from your own knowledge, no scope)"},
                "create": {"type": "array", "items": {"type": "string"},
                           "description": "build: files the grunt may create"},
                "build_dir": {"type": "string"},
                "build_cmd": {"type": "array", "items": {"type": "string"},
                              "description": "build: argv, never a shell string"},
            },
            "required": ["agent"],
        },
    },
    {
        "name": "team_wait",
        "description": ("Block until the named tasks seal. A superseded task is "
                        "resolved, not lost; only a timeout is a miss."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tasks": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "number", "description": "seconds, default 600"},
            },
            "required": ["tasks"],
        },
    },
    {
        "name": "team_verify",
        "description": (
            "Re-open every cited file and compare the cited line to the quoted "
            "evidence. A grunt's citation is not a fact until this returns "
            "ok=true. Grunts reproduce source text faithfully and locate it "
            "poorly, so the quote passing says nothing about the line number."),
        "inputSchema": {
            "type": "object",
            "properties": {"task": {"type": "string"}},
            "required": ["task"],
        },
    },
]


# --- rendering: the text a lead reads, beside the JSON it acts on -----------

def _verdict_dict(v: verify.Verdict) -> dict:
    rec = v.record if isinstance(v.record, dict) else {}
    return {"file": rec.get("file"), "line": rec.get("line"),
            "symbol": rec.get("symbol"), "evidence": rec.get("evidence"),
            "status": v.status, "detail": v.detail}


def _send(result: api.SendResult) -> tuple[str, dict]:
    verb = "replied" if result.kind == "reply" else "sent task"
    return (f"{verb} {result.id} to {result.agent}",
            {"task_id": result.id, "agent": result.agent, "kind": result.kind})


def _wait(result: api.WaitResult) -> tuple[str, dict]:
    lines = [f"SEALED: {t}" for t in result.sealed]
    # The answer travels back in the result. This is the "Claude only renders
    # it" path: an ask task's prose reaches the lead here, so it does not call
    # `team answer` and does not re-read anything. A find task's records are
    # deliberately NOT carried -- keeping the decompile out of context is why.
    for tid, text in result.answers.items():
        lines.append(f"\nANSWER {tid}:\n{text}")
    lines += [f"SUPERSEDED: {t}" for t in result.superseded]
    for m in result.blocked:
        lines.append(f"BLOCKED: {m['task']} ({m['type']} {m['id']}) {m['body']}")
        lines.append(f"  reply with team_send agent={m['from']} reply={m['id']}")
    lines += [f"TIMEOUT: {t}" for t in result.timed_out]
    return ("\n".join(lines) or "nothing to wait for",
            {"sealed": result.sealed, "superseded": result.superseded,
             "timed_out": result.timed_out, "blocked": result.blocked,
             "answers": result.answers, "ok": result.ok})


def _verify(result: api.VerifyResult) -> tuple[str, dict]:
    if result.kind == "ask":
        # No claim about the code, so nothing to verify and no PASS to give.
        # The answer itself rode back on team_wait; here we only say so.
        return (f"ask {result.task}: nothing to verify (0 citations). An ask "
                f"answer is prose, not a claim about the code.",
                {"task": result.task, "kind": "ask", "ok": True,
                 "verifiable": False, "citations": [], "build": None})
    parts = []
    if result.build is not None:
        parts.append(buildverify.render(result.build))
    if result.verdicts or result.kind == "find":
        parts.append(verify.render_table(result.task, result.verdicts))
    if not result.ok:
        parts.insert(0, FAIL_BANNER)
    return ("\n".join(parts),
            {"task": result.task, "kind": result.kind, "ok": result.ok,
             "verifiable": True,
             "citations": [_verdict_dict(v) for v in result.verdicts],
             "build": None if result.build is None else
                      {"status": result.build.status,
                       "detail": result.build.detail}})


# --- tools ------------------------------------------------------------------

def _root() -> Path:
    """Resolved per call, never cached: a server started before `team bootstrap`
    would otherwise answer for the wrong directory for the rest of the session."""
    return bus.bus_root()


def call_tool(name: str, arguments: dict) -> tuple[str, dict]:
    if name == "team_send":
        return _send(api.send(
            _root(), arguments["agent"],
            question=arguments.get("question", ""),
            scope=arguments.get("scope") or [],
            supersede=bool(arguments.get("supersede")),
            allow_dirty=bool(arguments.get("allow_dirty")),
            kind=arguments.get("kind", "find"),
            create=arguments.get("create") or [],
            build_dir=arguments.get("build_dir", "."),
            build_cmd=arguments.get("build_cmd")))
    if name == "team_wait":
        return _wait(api.wait_tasks(
            _root(), list(arguments["tasks"]),
            timeout=float(arguments.get("timeout", api.DEFAULT_WAIT_TIMEOUT))))
    if name == "team_verify":
        return _verify(api.verify_task(_root(), arguments["task"]))
    raise ValueError(f"unknown tool: {name}")


# --- wire -------------------------------------------------------------------

def handle(msg: dict) -> dict | None:
    """A response, or None for a notification. Never raises."""
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    if mid is None:
        return None                     # notification: answer nothing, ever

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion") or FALLBACK_PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": NAME, "version": VERSION}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        try:
            text, structured = call_tool(params.get("name"),
                                         params.get("arguments") or {})
        except REFUSALS as exc:
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": f"refused: {exc}"}],
                "isError": True}}
        except Exception as exc:        # never take the session down with us
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text",
                             "text": f"error: {type(exc).__name__}: {exc}"}],
                "isError": True}}
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": text}],
            "structuredContent": structured}}

    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(stdin, stdout) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue                    # a bad frame is not a dead session
        if not isinstance(msg, dict):
            continue
        try:
            response = handle(msg)
        except Exception as exc:        # belt: handle() is not supposed to raise
            mid = msg.get("id")
            if mid is None:
                continue
            response = {"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32603, "message": str(exc)}}
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def main() -> int:
    serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
