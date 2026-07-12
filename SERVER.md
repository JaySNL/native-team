# The inference server (you supply this)

**TeamBus does not bundle, launch, or manage a model server.** The bus comms — the
lead↔grunt file bus, the tmux plumbing, `team verify`, the MCP wrapper — are the working
proto. The model a grunt actually runs on is yours to provide. This keeps TeamBus a small,
stdlib-only coordination layer instead of shipping (and pinning) a multi-gigabyte model stack.

A grunt needs two things you install:

1. **A grunt CLI** — by default the [`qwen-code`](https://github.com/QwenLM/qwen-code) CLI,
   invoked as `qwen`. Nothing branches on which binary it is; point at any wrapper with
   `team up --command <bin>` / `team grunt add --command <bin>`.
2. **An OpenAI-compatible chat endpoint** for that CLI to call — mlx-serve, ollama, vLLM,
   llama.cpp's server, LM Studio, a hosted API, anything that speaks `/v1/chat/completions`.

## How TeamBus points the grunt at your server

Two ways, pick one.

### Option A — let TeamBus write the provider (env vars)

Set these before `team init` (and before launching `claude`, if you use the MCP tools). With
`TEAM_GRUNT_BASE_URL` set, `team` writes a self-contained `modelProviders.openai` block into each
grunt's `.qwen/settings.json`, so a fresh clone works without you hand-editing any qwen config.

| Env var | Meaning | Default |
|---|---|---|
| `TEAM_GRUNT_MODEL` | model name/id the grunt runs | `mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit-dwq-v2` |
| `TEAM_GRUNT_BASE_URL` | OpenAI-compatible endpoint. **If set → provider is written.** | *(unset — see Option B)* |
| `TEAM_GRUNT_API_KEY` | key value, exported into the grunt pane | `local` |
| `TEAM_GRUNT_CONTEXT_WINDOW` | provider context window (tokens) | `262144` |
| `TEAM_GRUNT_SESSION_TOKEN_LIMIT` | hard prompt-token ceiling (grunt refuses past it) | `200000` |
| `TEAM_GRUNT_WALL_SECONDS` | runaway wall-clock guard per grunt turn | `900` |

The API key is never written to a file: TeamBus stores only the *name* of the env var
(`TEAM_GRUNT_API_KEY`) as the provider's `envKey`, and exports the value into the grunt pane at
launch. Local servers ignore the value, but qwen requires the variable to be present.

With **no** `TEAM_GRUNT_*` set, TeamBus writes exactly what it always did — no provider block — and
the grunt falls back to your own `~/.qwen` config (Option B). Unset behavior is unchanged.

### Option B — put the provider in your own `~/.qwen/settings.json`

If you already run qwen against a server, add the provider there and set only `TEAM_GRUNT_MODEL`
(or nothing, if the pinned default is your model). Shape:

```json
{
  "modelProviders": {
    "openai": [{
      "id": "your-model",
      "name": "your-model",
      "baseUrl": "http://localhost:PORT/v1",
      "envKey": "OPENAI_API_KEY",
      "generationConfig": { "contextWindowSize": 262144, "extra_body": { "temperature": 0 } }
    }]
  }
}
```

`extra_body.temperature: 0` is deliberate: a grunt transcribes, finds, and edits verbatim —
greedy decoding is correct, and it is honored as `extra_body` under an active provider (top-level
`generationConfig` is ignored there).

---

## Worked example — mlx-serve (Apple Silicon)

[mlx-serve](https://github.com/madroidmaq/mlx-serve) (or `mlx_lm.server`) exposes an
OpenAI-compatible endpoint for MLX models on Apple Silicon.

```sh
# 1. serve a coder model (example)
mlx_lm.server --model mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit-dwq-v2 --port 11234

# 2. point TeamBus at it
export TEAM_GRUNT_BASE_URL=http://localhost:11234/v1
export TEAM_GRUNT_MODEL=mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit-dwq-v2
export TEAM_GRUNT_API_KEY=local        # mlx-serve ignores the value

# 3. run a team
team init && team-up 1
```

Serving on another machine on your LAN? Use its address (`http://<host>:11234/v1`) as the base url.

## Worked example — ollama (cross-platform)

[ollama](https://ollama.com) exposes an OpenAI-compatible endpoint at `http://localhost:11434/v1`.

```sh
# 1. pull a coder model (any works; use what your box can hold)
ollama pull qwen3-coder:30b        # or: ollama pull qwen2.5-coder:32b

# 2. point TeamBus at it
export TEAM_GRUNT_BASE_URL=http://localhost:11434/v1
export TEAM_GRUNT_MODEL=qwen3-coder:30b
export TEAM_GRUNT_API_KEY=ollama    # ollama ignores the value

# small box? shrink the window (and match ollama's num_ctx)
export TEAM_GRUNT_CONTEXT_WINDOW=40960
export TEAM_GRUNT_SESSION_TOKEN_LIMIT=32000

# 3. run a team
team init && team-up 1
```

## Notes on model choice

The grunt's whole job is bounded transcription/finding/scaffolding under a lead that verifies every
citation — so a small, fast *coder* model beats a big reasoning model here. Anything in the
Qwen-Coder family (or similar) is a reasonable grunt. The lead (`claude`) is where judgement lives;
the grunt is the cheap, verifiable hand.
