# TeamBus — shareable-proto packaging design

**Date:** 2026-07-12
**Repo:** `native-team` (origin `github.com/JaySNL/Claude-Teamchat`, **stays PRIVATE**)
**Status:** design approved, pre-implementation

## Goal

Turn the existing `native-team` codebase into a proto that a stranger can **download,
install, and run** — where "it works" means the **bus comms** (lead↔grunt file bus, tmux
plumbing, `verify`, MCP wrapper) work out of the box. The **inference server**, the
**guardrails**, and the **grunt provisioning** are explicitly *user-supplied* and only
*documented* here, not bundled. The repo stays private; this work makes it *shareable-clean*
so it can be flipped public later.

## Scope

**In:**
1. Decouple the grunt model + inference endpoint from the author's mlx-serve rig (env-driven,
   defaults preserve current behavior byte-for-byte).
2. Relocate the guardrail (`route_guard` hook) + a generic grunt-context sample into `examples/`.
3. Strip all personal information / rig specifics / personal paths from tracked files.
4. Docs: `SERVER.md`, `INSTALL.md`, README overhaul, `STATUS.md`, `examples/README.md`.
5. `LICENSE` (MIT) + `.github/FUNDING.yml` + README "Support" section.
6. Git: commit WIP + this work, merge `feat/named-bus`→`master`, push to private origin.

**Out (documented, not built):**
- Any bundled model server. TeamBus never ships or launches mlx-serve/ollama.
- The user's actual guardrails (they wire their own PreToolUse hook).
- Grunt behavioral rules / memory bank beyond a generic sample.
- **Git-history scrub.** History still contains personal info; a clean *public* flip needs
  `git-filter-repo`/BFG. Flagged as a separate pre-public task, **not** done here.

## Decisions (locked)

| Question | Decision |
|---|---|
| Server config | **Env/config-driven**; defaults = current mlx-serve values. |
| Guardrail | **Move to `examples/`**, document as optional/user-wired. |
| Repo scope | **Polish this repo**, keep private. |
| License | **MIT** + FUNDING.yml + README Support section. |
| Git | **Commit + merge `feat/named-bus`→`master` + push** to private origin. |

## Architecture — what ships vs what the user supplies

```
Ships (the proto = bus comms):        User supplies (documented):
  team/  (bus, protocol, verify,        - OpenAI-compat inference server
         wait, cli, ops, panes,           (mlx-serve OR ollama OR any) → SERVER.md
         config, mcp_server, ...)       - qwen-code CLI (grunt) + claude (lead)
  bin/team, bin/team-up, bin/team-mcp   - guardrails → examples/guardrails/
  TEAMCHAT.md (the `team brief`)        - grunt context/memory → examples/TEAM_GRUNT_CONTEXT.md
  examples/ (guardrail + context sample)  (copy to repo root to activate)
  tests/ (471, stdlib only)
  SERVER.md, INSTALL.md, README, LICENSE
```

Key existing facts (verified in code, do not re-derive):
- Lead binary = `--lead-command` (default `claude`); grunt binary = `--command` (default
  `qwen`). Both already overridable; nothing branches on the choice. **No code change needed
  to point at a wrapper.**
- `_grunt_env()` (`team/cli.py`) already injects `PATH` + `TEAM_BUS` into the grunt pane via
  `tmux split-window -e`. This is the injection point for the provider API-key env var.
- `team init`/`provision` write `GRUNT_SETTINGS` to `.qwen/settings.json` (lead repo + each
  grunt worktree). `team down` restores via the **`created` flag recorded in `.team/init.json`**
  (meta-driven), *not* content equality — so env changes between init and down do **not** strand
  the user's real settings. The `== grunt_settings()` compare is only a degraded fallback for a
  hand-removed `.team`.
- qwen-code provider schema (verified against a working install):
  ```json
  "modelProviders": { "openai": [ {
    "id": "<model>", "name": "<model>",
    "baseUrl": "http://host:port/v1",
    "envKey": "<NAME_OF_ENV_VAR_HOLDING_KEY>",
    "generationConfig": { "contextWindowSize": 262144, "extra_body": { "temperature": 0 } }
  } ] }
  ```
  `envKey` is the *name* of an env var, not the key itself — so no secret is ever stored in the
  repo or in settings.json.

## Component 1 — Server decoupling (`team/config.py`)

Convert the module-level constant `GRUNT_SETTINGS` into a function `grunt_settings(env=os.environ)`
that builds the dict from environment at call time. The `env` param exists for test injection.

**Env vars (all optional; defaults reproduce today's output byte-for-byte when unset):**

| Env var | Maps to | Default |
|---|---|---|
| `TEAM_GRUNT_MODEL` | `model.name` (and provider `id`/`name` when a provider is written) | `mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit-dwq-v2` |
| `TEAM_GRUNT_BASE_URL` | if set → write a `modelProviders.openai[]` entry | *(unset → no provider key)* |
| `TEAM_GRUNT_API_KEY` | provider `envKey` name + exported into pane by `_grunt_env()` | `local` |
| `TEAM_GRUNT_CONTEXT_WINDOW` | provider `generationConfig.contextWindowSize` | `262144` |
| `TEAM_GRUNT_SESSION_TOKEN_LIMIT` | `model.sessionTokenLimit` | `200000` |
| `TEAM_GRUNT_WALL_SECONDS` | `model.maxWallTimeSeconds` | `900` |

**Provider block (only when `TEAM_GRUNT_BASE_URL` is set):** `grunt_settings()` adds
`modelProviders.openai = [{ id, name, baseUrl, envKey: "TEAM_GRUNT_API_KEY",
generationConfig: { contextWindowSize, extra_body: { temperature: 0 } } }]`. `temperature: 0`
is load-bearing for grunt determinism (verbatim-copy/precise-edit work; the source comment
explains why top-level `generationConfig` is ignored under an active provider). When
`TEAM_GRUNT_BASE_URL` is **unset**, no `modelProviders` key is written — output is identical to
today, and the grunt relies on the user's own `~/.qwen` provider (current behavior).

**Key export:** extend `_grunt_env()` to also export `TEAM_GRUNT_API_KEY` (value from env,
default `local`) so the provider's `envKey` resolves inside the grunt pane. Local servers ignore
the value but qwen-code requires the env var to be non-empty.

**Call-site updates:** the 5 references to `GRUNT_SETTINGS` (3 provenance compares at ~lines
230/413 + init writer 248 + provision writer 286) call `grunt_settings()` instead. All compares
must call the function (not a stale snapshot) so a given session is internally consistent.

**Test hygiene:** `test_config` and `test_named_bus` pin the exact payload. They must run under a
controlled env (`patch.dict(os.environ, {}, clear=...)` for the relevant `TEAM_GRUNT_*` keys) so a
developer who exports these vars does not fail the suite. Add: (a) default (no-env) still equals the
pinned dict; (b) `TEAM_GRUNT_MODEL` override changes `model.name`; (c) `TEAM_GRUNT_BASE_URL` set
writes a correct `modelProviders` block with `temperature: 0` and `envKey: "TEAM_GRUNT_API_KEY"`;
(d) unset base-url writes no `modelProviders` key.

## Component 2 — Guardrail → `examples/`

- Move `hooks/team_route_guard.py` → `examples/guardrails/team_route_guard.py`; scrub personal
  paths/model names inside it to generic placeholders (it becomes reference, not a wired hook).
- Move `tests/test_route_guard.py` alongside or update its import path so it still runs.
- Add `examples/TEAM_GRUNT_CONTEXT.md` — a **generic** grunt behavioral-rules sample (the real one
  is the author's IFZ-specific file and is not shipped). A downloader copies this to their repo
  root to activate grunt context; absent, grunts run without it (they still work).
- `examples/README.md`: explains both files are optional; how to wire the guard into a
  Claude Code `settings.json` PreToolUse hook; how to activate the context file.

## Component 3 — Personal-info / rig scrub

Tracked files with leakage (11): the 6 `docs/superpowers/specs/*` design docs, `HANDOFF.md`,
`hooks/team_route_guard.py`, `skills/teamup/SKILL.md`, `team/config.py`, `team/mcp_server.py`,
`tests/test_route_guard.py`.

Rules:
- **Remove** real name/email/usernames (`user`, `redacted`, gmail, the author's real name).
- **Genericize** personal paths (`/home/<user>`, `~/.claude/...`, `/mnt/nas`, LAN IPs
  `100.70.x.x`, `:11234` when presented as *the* endpoint) → `~/path/to/...`, `<your-repo>`,
  `http://localhost:PORT/v1`.
- **De-rig** hardware boasts (`Apple Silicon 48GB`, `4090`, `LAN` as "my box") → neutral phrasing
  ("an OpenAI-compatible server on localhost or your LAN").
- **Keep** public tool/model names (`mlx-serve`, `ollama`, `qwen`, `Qwen3-Coder-*`) — legitimate
  examples, not personal data.
- Relocate `docs/superpowers/specs/*` → `docs/design-history/` and apply the same scrub (lighter —
  they are rationale, not install docs). This design doc lives there too. **Update the README
  "Docs" links** that currently point at `docs/superpowers/specs/...` to the new paths.
- Reminder in `STATUS.md`: **git history is not scrubbed** — history rewrite required before a
  public flip.

## Component 4 — Docs

- **`SERVER.md`** (new): "TeamBus does not bundle a model server." Grunt needs = the `qwen`
  (qwen-code) CLI + any OpenAI-compatible chat endpoint. Two worked examples, each showing the
  exact `TEAM_GRUNT_*` env values:
  - **mlx-serve** (Apple Silicon): serve command, `http://localhost:11234/v1`, model string.
  - **ollama** (cross-platform): `ollama pull qwen3-coder:30b` (or similar), OpenAI-compat
    `http://localhost:11434/v1`, model-name note.
  - Third path: "or just put a `modelProviders.openai` block in your own `~/.qwen/settings.json`
    and set only `TEAM_GRUNT_MODEL`."
- **`INSTALL.md`** (new): prerequisites (`tmux`, `python3 ≥ 3.10` — code uses `X | None` unions —
  `git`, `claude`, `qwen`, a server per SERVER.md); clone → `ln -sf bin/team bin/team-up ~/.local/bin`
  → `team init` → `team-up 1`; copy-paste `.mcp.json` block (with the `TEAM_ROOT` env note).
- **README** overhaul: add an "Included vs You-supply" section pointing at SERVER.md / examples/;
  keep the existing "how it works" / named-bus content; add a **Support** section.
- **`STATUS.md`** (new): current state — named-bus + `TEAM_ROOT` landed, 471 tests green, proto =
  bus comms, repo private, history-scrub pending.

## Component 5 — License + funding

- `LICENSE` = MIT, current year, author name (the *display* author name is fine to include; no
  email/personal path).
- `.github/FUNDING.yml` = GitHub Sponsors / Ko-fi placeholders for the maintainer to fill.
- README "Support" section: short, links to the funding channels.

## Component 6 — Git

All commits land on `feat/named-bus`, then the branch merges to `master`:
1. Commit the current WIP (named-bus + `TEAM_ROOT` + their tests) as a focused commit.
2. Commit the packaging work (decoupling, examples move, scrub, docs, license) — logically grouped
   into a few commits (e.g. decouple / scrub+relocate / docs+license).
3. Merge `feat/named-bus` → `master` (feature complete, 471 green, already documented in README).
4. Push `master` to private origin. **No visibility change.**

## Testing

- `python3 -m unittest discover -s tests -t .` stays green (target: still 471+, with the new
  `grunt_settings()` env tests added and env-hygiene applied).
- Manual smoke (documented, not automated here): fresh clone + ollama, set the three env vars,
  `team-up 1`, send a scoped question, `team verify` — confirm the grunt reaches the endpoint and a
  citation verifies. This is the real "it works" proof for a downloader.

## Risks

- **qwen-code provider merge semantics** — a workspace `modelProviders` may replace rather than
  merge the user's `~/.qwen` providers. Acceptable: only happens when the user opts in via
  `TEAM_GRUNT_BASE_URL`; unset = unchanged.
- **Env drift within a session** — changing `TEAM_GRUNT_*` between `init` and `down` is safe
  (meta-driven restore) except in the hand-removed-`.team` degraded path. Documented.
- **Git history** — retains personal info until a pre-public rewrite. Documented, out of scope.
