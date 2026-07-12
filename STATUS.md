# Status

**What this is:** a working prototype of the **bus comms** — a `claude` lead and interactive
`qwen` grunts in adjacent tmux panes, coordinated by a directory, with every grunt citation
re-read and verified byte-for-byte. Download it, install it (see [INSTALL.md](INSTALL.md)), point a
grunt at your own server (see [SERVER.md](SERVER.md)), and it runs.

**Repo is private.** This is a shareable-clean snapshot, not yet a public release.

## Working

- Lead↔grunt file bus: inbox/result files, seal-then-announce, no daemon, no status field.
- `team verify` — re-reads every cited line, compares evidence, fails closed (exit 1).
- `team wait` — blocks; exit code is the wake signal (0 ok / 1 fail / 2 pane gone / 3 refused / 4 timeout).
- tmux pane lifecycle, worktree-per-grunt isolation, death hooks.
- Named buses (`.team-<slug>/`) — several teams in one working tree; `TEAM_BUS`.
- `TEAM_ROOT` — address a bus from outside its tree (the MCP server case).
- MCP wrapper — `team_send` / `team_verify` / `team_wait`.
- **Env-driven grunt server config** — `TEAM_GRUNT_*` retargets the grunt at any OpenAI-compatible
  server with no source edit; unset behaves exactly as before.
- **475 tests**, stdlib only, no pytest. The e2e test drives a real tmux session.

## You supply (documented, not bundled)

- The inference server — mlx-serve / ollama / anything OpenAI-compatible → [SERVER.md](SERVER.md).
- The grunt CLI (`qwen`) and lead CLI (`claude`).
- Guardrails and grunt behavioral rules → [`examples/`](examples/) has references to copy.

## Before a public flip (not done here)

- **Scrub git history.** The working tree is clean, but earlier commits still contain personal
  paths/identifiers. A public flip needs a history rewrite (`git-filter-repo` / BFG).
- Fill in `.github/FUNDING.yml` with real funding handles.
- Optional: short feature-loop GIFs (named-bus, MCP) and a real terminal capture to complement the
  hero demo.

## Layout

```
team/        the CLI package (bus, protocol, verify, wait, cli, ops, panes, config, mcp_server, ...)
bin/         team, team-up, team-mcp
hooks/       team_route_guard.py — optional guardrail reference (not wired by `team init`)
examples/    guardrail wiring + a generic TEAM_GRUNT_CONTEXT.md sample
tests/       475 stdlib tests
docs/        design-history/ (rationale), tmux-capabilities, validation notes
TEAMCHAT.md  the lead's ground rules (`team brief`)
SERVER.md · INSTALL.md · README.md · LICENSE
```
