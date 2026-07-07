# OSS-CRS repair agent on a local OpenAI-compatible model

**Date:** 2026-07-07
**Status:** Approved design, pending implementation

## Goal

Run the expensive side of the pipeline — the OSS-CRS repair agent (crs-claude-code,
driven by `arvo-eval/arvo_oss_crs.py`) — against a local model that exposes an
OpenAI-compatible API, instead of Claude via the OAuth subscription. The cheap,
quality-critical learning/extractor side (`llm.py`, `learn_loop.py`) stays on Claude
and is untouched.

## Context

- The local model is served remotely and reached through an SSH tunnel bound to the
  host's `localhost:8080` (llama.cpp-style server, Qwen-based 9B GGUF
  "Qwythos-9B…Q4_K_M"). Verified to handle OpenAI-format `tools` /
  `tool_calls` correctly.
- OSS-CRS already runs an internal LiteLLM proxy per CRS
  (`llm_config.litellm.mode: internal`). The current `compose-oauth.yaml` bypasses it
  by setting `CLAUDE_CODE_OAUTH_TOKEN`; removing that token routes Claude Code through
  LiteLLM, which translates Anthropic-format requests to any OpenAI-compatible
  backend.
- The LiteLLM container runs on private compose networks with no `extra_hosts` and no
  host networking, so it cannot reach `127.0.0.1`-bound host ports directly.

## Design

Approach chosen: **LiteLLM reroute** — keep crs-claude-code and the whole
`arvo_oss_crs.py` pipeline unchanged; swap only the model backend.

### 1. `arvo-eval/oss-crs-local/litellm-config-local.yaml` (new, canonical copy)

LiteLLM `model_list` mapping the model names Claude Code requests to the local
server:

- `claude-opus-4-8` → `openai/local`, `api_base: http://172.17.0.1:8080/v1`,
  dummy `api_key` (llama.cpp ignores both the model id and key).
- Same mapping for `claude-haiku-4-5-20251001` (Claude Code background calls) and
  `claude-sonnet-4-6` (subagent default), so no request 404s at the proxy.

### 2. `arvo-eval/oss-crs-local/compose-local.yaml` (new, canonical copy)

Copy of `example/crs-claude-code/compose-oauth.yaml` with:

- `CLAUDE_CODE_OAUTH_TOKEN` removed — this is the switch that sends Claude Code
  through the OSS-CRS LiteLLM proxy.
- `llm_config.litellm.internal.config_path` pointed at the local LiteLLM config.
- `ANTHROPIC_MODEL` left as `claude-opus-4-8` (an alias the proxy rewrites; nothing
  downstream changes).

### 3. File placement / install step

OSS-CRS resolves `config_path` relative to its own checkout, so both files are
copied into `~/oss-crs/example/crs-claude-code/` before use. Canonical,
version-controlled copies live in `arvo-eval/oss-crs-local/` with a small
`install.sh` (or documented `cp`) that copies them over. If implementation shows
absolute `config_path` works, the copy step for the LiteLLM config can be dropped.

### 4. `arvo_oss_crs.py` change

`COMPOSE_FILE` becomes env-overridable:

```python
COMPOSE_FILE = Path(os.environ.get(
    "OSS_CRS_COMPOSE_FILE",
    str(OSS_CRS_DIR / "example/crs-claude-code/compose-local.yaml")))
```

Local is the default; set `OSS_CRS_COMPOSE_FILE=.../compose-oauth.yaml` to flip back
to Claude-via-OAuth per run. Requires a one-time
`uv run oss-crs prepare --compose-file <compose-local.yaml>`.

### 5. Networking: SSH tunnel reachable from the LiteLLM container

The tunnel gains a second listen address on the Docker bridge gateway so containers
can reach it, while nothing is exposed to the LAN:

```bash
ssh -L 8080:localhost:8080 -L 172.17.0.1:8080:localhost:8080 user@llm-server
```

`api_base` uses `http://172.17.0.1:8080/v1`. Documented in `arvo-eval/README.md`.

## Error handling

- Local server down / tunnel not rebound → LiteLLM returns connection errors, the CRS
  run fails, and `arvo_oss_crs.py` already surfaces nonzero exits. No new retry
  logic.
- Model-quality failures (bad patches from the 9B model) are absorbed by the existing
  `repair_loop.py` retry-with-feedback and `verify_fix` verification — that is the
  accepted trade-off of this switch.

## Testing / verification

1. `curl http://172.17.0.1:8080/v1/models` from the host (tunnel rebind works).
2. `uv run oss-crs prepare --compose-file .../compose-local.yaml`.
3. Run bug 444773339 end-to-end via `arvo_oss_crs.py` with the local compose;
   confirm requests hit the tunnel (server/LiteLLM logs), a patch artifact is
   produced, and the result JSON still parses.
4. Flip `OSS_CRS_COMPOSE_FILE` back to `compose-oauth.yaml` and confirm the OAuth
   path still works (no regression).

## Out of scope

- The learning/extractor side (`llm.py` backends) — unchanged, stays on Claude.
- Any tuning of crs-claude-code prompts for the smaller model.
- swe-agent-eval result analysis.
