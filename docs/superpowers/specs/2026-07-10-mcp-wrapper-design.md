# MCP wrapper — design

**Status:** implemented; verified through the shim; registered in this repo's
`.mcp.json`.

**Goal:** give the lead `team_send` / `team_wait` / `team_verify` as tools, so
its control flow stops depending on shell exit codes it is documented to get
wrong.

---

## Why, precisely

`TEAMCHAT.md` has a section called **"Three traps that will bite you"**. All
three are artifacts of driving this tool through a shell:

- `team wait ...; echo done` destroys `$?`. The lead concludes a task sealed
  when it timed out.
- `argparse` exits `2` on a bad flag, colliding with `PANE_GONE`. A typo looks
  like a dead grunt.
- `send` prints `sent task 007`; the lead must *parse* the id, because task ids
  and message ids share one counter and `008` may not be the next task.

A tool call has no `$?` to destroy, no argv to mistype into a collision, and
returns a value rather than a line of text to parse. Every one of those traps
is a property of the transport, not of the tool.

`verify` is the sharpest case. It answers a question — *is this citation real?*
— and today it answers by exiting `1`, which the lead must remember not to
swallow. As a tool it returns `ok: false` next to the failing citation and the
true line number.

## What this is not

Not a replacement for the CLI. A grunt has no MCP client; `team result add` and
`team msg` are typed by qwen into a shell, and stay shell verbs. The wrapper
covers exactly the three verbs the **lead** uses in its loop.

Not a second implementation. That is the whole design constraint below.

---

## Architecture: one core, two renderings

Today the logic lives in `cli.cmd_verify`: is this a build task, resolve
citations against the worktree rather than the main tree, fail closed. An MCP
server that re-implemented that would drift from the CLI, and the drift would be
silent — two answers to *is this citation real?*

So the logic moves down into a new module and both surfaces sit on top:

```
        team/api.py          <- decides. returns objects. raises. no printing.
       /            \
cli.cmd_verify    mcp_server.team_verify
  renders text      serializes JSON
```

`api` never prints and never exits. It raises the exceptions `cli.main` already
maps to exit codes (`StateError`, `bus.BusError`, `panes.PaneError`,
`WorktreeError`), so the CLI's behaviour is unchanged by construction — the
existing 398 tests are the proof.

### `team/api.py`

```python
@dataclass
class SendResult:   kind: str          # "task" | "reply"
                    id: str
                    agent: str

@dataclass
class WaitResult:   sealed: list[str]
                    superseded: list[str]
                    timed_out: list[str]
                    @property ok -> not self.timed_out

@dataclass
class VerifyResult: task: str
                    kind: str          # "find" | "build"
                    build: TaskVerdict | None
                    verdicts: list[Verdict]
                    ok: bool
```

`send(root, agent, ...)` raises `panes.PaneError` when the grunt's pane is gone.
`cmd_send` catches it, prints the message it always printed, and returns
`PANE_GONE` — the mapping stays where it is.

### `team/mcp_server.py`

Newline-delimited JSON-RPC 2.0 on stdin/stdout, stdlib only, no SDK. Shape
copied from `~/.claude/tools/ifz-code-search-mcp.mjs`, which is known to work
with this Claude Code build rather than merely documented to:

- `initialize` → echo the client's `protocolVersion`, `capabilities: {tools:{}}`
- `ping` → `{}`
- `tools/list` → the three tools
- `tools/call` → `{content: [{type: "text", ...}], structuredContent: {...}}`
- notifications (`notifications/*`) → no response, ever
- unknown method with an id → `-32601`

The bus root is resolved **per call** from the server's cwd, not at startup: a
server that cached `bus_root()` before `team bootstrap` ran would answer for the
wrong directory forever.

## The tools

| tool | arguments | returns |
|---|---|---|
| `team_send` | `agent`, `question`, `scope[]`, `supersede`, `allow_dirty`, `kind` (`find`\|`build`), `create[]`, `build_dir`, `build_cmd[]` | `{task_id, agent}` |
| `team_wait` | `tasks[]`, `timeout` (default 600) | `{sealed[], superseded[], timed_out[], ok}` |
| `team_verify` | `task` | `{ok, kind, citations[], build}` |

`kind: build` is included rather than deferred. Omitting it would make the tool
surface strictly weaker than the CLI, pushing the lead back to `Bash` for build
tasks — back into the exit-code traps this exists to remove.

`lenient` is deliberately absent. It is a way to make a shell `&&` proceed; a
tool call that returns `ok: false` needs no such escape, and offering one would
be offering the lead a way to launder a fabricated citation.

## `isError` vs `ok: false` — the load-bearing distinction

A failed *verification* is a **successful** tool call. The tool was asked
whether the citations hold; it answered "no". `isError` is reserved for the tool
being unable to answer at all: no bus, no such task, the pane is gone.

Conflating them would teach the lead that `verify` "errored" and can be retried.
It cannot. It reported.

To stop a failing verification reading as a footnote, the text content leads
with the verdict:

```
VERIFY FAILED — do not use these citations, do not open the file. Re-ask.
<the same table `team verify` prints>
```

## Registration

Global registration is wrong for the same reason it was wrong for the route
guard: an MCP server in `~/.claude.json` starts in *every* project, finds no bus,
and errors on every call. It belongs in the `.mcp.json` of a repo that runs a
team.

Three things about `.mcp.json`, all measured with `claude mcp list`, none of them
guessable:

- **The server's cwd is the directory `claude` was started in, not the repo
  root.** A session opened in `team/` launches the server in `team/`. So a
  relative `"command": "./bin/team-mcp"` connects from the root and *fails to
  connect* from any subdirectory.
- **`${CLAUDE_PROJECT_DIR}` is not expanded** in `.mcp.json`. It is passed
  through literally and the exec fails.
- A project server is **pending approval** until it is accepted interactively or
  listed in `.claude/settings.local.json` under `enabledMcpjsonServers`. That
  file is per-machine approval state, and is gitignored.

An absolute path would work and would also bake one developer's home directory
into a committed file. So the repo resolves itself:

```json
{"mcpServers": {"team": {
  "command": "sh",
  "args": ["-c", "exec \"$(git rev-parse --show-toplevel)/bin/team-mcp\""]}}}
```

Connects from the repo root and from any subdirectory. `bus_root()` walks up, so
a session started in a subdirectory still finds the bus.

One edge, accepted: inside a linked worktree `--show-toplevel` returns the
worktree, which has no `bin/`. A lead never starts a session in
`.team/work/<agent>` — that is where a grunt stands, and a grunt has no MCP
client.

Verified live: `claude -p` in this repo reports `mcp__team__team_send`,
`mcp__team__team_wait`, `mcp__team__team_verify`.

## Verified through the shim

A sealed task citing line 3 of a symbol on line 2, driven exactly as a client
would drive it — `initialize`, a `notifications/initialized` that gets no reply,
`tools/list`, `tools/call`:

```
initialize  -> {'name': 'team', 'version': '1.0.0'} 2025-06-18
tools/list  -> ['team_send', 'team_wait', 'team_verify']
team_verify -> isError: False | ok: False
citation    -> {'file': 'src/A.cs', 'line': 3, 'symbol': 'two',
                'status': 'OFF_BY', 'detail': 'cited 3, actual 2 (off by -1)'}
text[0]     -> VERIFY FAILED — do not use these citations, do not open the file.
```

Not an error. A report. Mutants M47–M53 killed, including M47 (mark a failing
verification `isError`) and M52 (count a superseded task as a miss).

## Test plan

`api` and the wire protocol are separately testable; neither needs a model.

| test | kills |
|---|---|
| `api.verify` on a find task with a bad line → `ok False`, `OFF_BY` | |
| `api.verify` on a build task resolves citations against the **worktree** | a server that re-implements the branch |
| `cmd_verify` output unchanged (existing 398 tests) | drift between the two surfaces |
| `api.send` raises `PaneError`; `cmd_send` still returns `PANE_GONE` | moving the mapping |
| `initialize` echoes the client's protocolVersion | a hardcoded version |
| `tools/list` names exactly three tools | surface creep |
| `tools/call` unknown tool → `isError` | |
| `team_verify` on failing citations → **not** `isError`, `ok: false` | the conflation above |
| `team_verify` with no bus → `isError` | |
| notification (no `id`) → no bytes written | a server that replies to notifications |
| garbage line → no crash, server keeps serving | one bad frame killing the session |
| unknown method with id → `-32601` | |
