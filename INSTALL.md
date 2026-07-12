# Install

TeamBus is a small, stdlib-only Python CLI. There is nothing to build.

## Prerequisites

| Need | Why |
|---|---|
| `tmux` | grunts run as adjacent panes; the lead drives them via tmux |
| `python3` ≥ 3.10 | the CLI (uses `X \| None` type unions); stdlib only, no pip deps |
| `git` | the bus lives in a repo; `team init` refuses outside one |
| `claude` (Claude Code CLI) | the **lead** — override with `--lead-command` |
| `qwen` ([qwen-code](https://github.com/QwenLM/qwen-code)) | the **grunt** — override with `--command` |
| an OpenAI-compatible model server | what the grunt runs on — **you supply it**, see [SERVER.md](SERVER.md) |

## 1. Get the code and put `team` on your PATH

```sh
git clone <this-repo> native-team
cd native-team
ln -sf "$PWD/bin/team"    ~/.local/bin/team
ln -sf "$PWD/bin/team-up" ~/.local/bin/team-up
```

`team` and `team-up` run from inside whatever repo they manage, not from the `native-team`
checkout. (`bin/team-mcp` is not symlinked — the MCP server is referenced by absolute path in
`.mcp.json`, see below.)

## 2. Point the grunt at your server

See [SERVER.md](SERVER.md). Shortest path (ollama):

```sh
export TEAM_GRUNT_BASE_URL=http://localhost:11434/v1
export TEAM_GRUNT_MODEL=qwen3-coder:30b
export TEAM_GRUNT_API_KEY=ollama
```

## 3. Run a team

From inside the repo you want a team to work on (must be a git repo):

```sh
team init                 # create .team/, write grunt qwen settings
team-up 1                 # tmux: lead + 1 grunt
team send grunt1 --question "Where is X defined?" --scope src/A.py
team wait --task 001 --timeout 600     # background this from the lead
team verify 001           # re-reads every cited line; exit 1 on any FAIL
team down                 # restore .qwen/settings.json, remove the bus
```

> **`team init` changes the repo it runs in.** It writes `.qwen/settings.json`, which puts your own
> `qwen` in that repo into YOLO mode with no `CLAUDE.md` context until `team down` restores it. The
> `init` output says so. See the README for the full "two things to know before you run it".

## 4. (optional) MCP tools for the lead

To call `team_send` / `team_verify` / `team_wait` as MCP tools from Claude Code instead of Bash,
register the server in your project's `.mcp.json`. Give it `TEAM_ROOT` (the repo that holds
`.team/`) because the server inherits the lead's launch directory as its cwd:

```json
{
  "mcpServers": {
    "team": {
      "type": "stdio",
      "command": "/abs/path/to/native-team/bin/team-mcp",
      "env": { "TEAM_ROOT": "/abs/path/to/your/repo" }
    }
  }
}
```

`TEAM_ROOT` (and `TEAM_BUS`, for a named bus) are read when the server spawns, so restart `claude`
after changing them. The plain `team …` CLI needs no reload — each call is a fresh process.

## 5. (optional) guardrails and grunt context

These are **yours to build** — TeamBus ships references, not a wired setup. See
[`examples/`](examples/): an optional route-guard hook and a generic `TEAM_GRUNT_CONTEXT.md`
behavioral-rules sample you can copy to your repo root.

## Verify it works

```sh
python3 -m unittest discover -s tests -t .
```

475 tests, stdlib only. The end-to-end test drives a real tmux session with a scripted grunt, and
is skipped only if tmux is absent.
