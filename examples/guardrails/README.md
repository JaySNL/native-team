# Guardrail — the route guard (optional)

`team verify` stops a *false* grunt answer. This guard stops a *wasted* one: it keeps the **lead**
from opening a file into a scope that a grunt is currently working, so the whole point of
delegating the read — keeping that file out of the lead's context window — is not defeated.

It is a Claude Code **PreToolUse hook**, not part of the bus, and `team init` never wires it. You
opt in.

## Reference implementation

The working hook lives at [`hooks/team_route_guard.py`](../../hooks/team_route_guard.py) in this
repo. It:

- reads the tool call on stdin, checks it against the open tasks in the active bus,
- **hard-denies** a read that reaches *into* an open scope, **nudges** on the ambiguous case,
- **fails open** — any error and the tool call proceeds; a broken guard must never break a call,
- lifts by itself the moment a task seals (it keeps no state of its own),
- can be disabled at any time with `TEAM_ROUTE_GUARD=0`.

## Wiring it

Add a `PreToolUse` hook to your Claude Code `settings.json` pointing at the script by absolute
path. The `test -r` guard makes it a no-op if the path is ever missing, so it degrades safely:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Read|Grep|Glob",
        "hooks": [
          {
            "type": "command",
            "command": "sh -c 'test -r \"$0\" || exit 0; exec python3 \"$0\"' /abs/path/to/native-team/hooks/team_route_guard.py"
          }
        ]
      }
    ]
  }
}
```

Adjust the `matcher` to the read-shaped tools you want gated. This is a starting point — the policy
of *what* counts as "reaching into a scope" is yours to tune in the script.
