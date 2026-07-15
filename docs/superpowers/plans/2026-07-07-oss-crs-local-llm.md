# OSS-CRS Local-Model Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the OSS-CRS repair agent (crs-claude-code) against the local OpenAI-compatible model on `localhost:8080` (SSH tunnel) instead of Claude via OAuth, leaving the learning/extractor side on Claude.

**Architecture:** Keep crs-claude-code unchanged and swap only its model backend: a new compose file omits `CLAUDE_CODE_OAUTH_TOKEN`, which makes `crs-claude-code/agents/claude_code.py` route Claude Code through OSS-CRS's internal LiteLLM proxy; a new LiteLLM config maps the Claude model aliases to `openai/local` at `http://172.17.0.1:8080/v1` (the host as seen from inside the container — the SSH tunnel gains a second `-L` binding there). `arvo_oss_crs.py` selects the compose file via `OSS_CRS_COMPOSE_FILE`, defaulting to local.

**Tech Stack:** OSS-CRS (`~/oss-crs`), LiteLLM proxy (internal mode), llama.cpp-style OpenAI server, Python 3 + pytest.

**Spec:** `docs/superpowers/specs/2026-07-07-oss-crs-local-llm-design.md`

---

### Task 1: Canonical config files (LiteLLM + compose)

Pure config, no tests. Canonical copies live in this repo; Task 2 installs them into `~/oss-crs`.

**Files:**
- Create: `arvo-eval/oss-crs-local/litellm-config-local.yaml`
- Create: `arvo-eval/oss-crs-local/compose-local.yaml`

- [ ] **Step 1: Write the LiteLLM config**

`arvo-eval/oss-crs-local/litellm-config-local.yaml`:

```yaml
# Routes the Claude model aliases crs-claude-code requests to a local
# OpenAI-compatible server (llama.cpp behind an SSH tunnel).
#
# 172.17.0.1 is the HOST as seen from inside the LiteLLM container (docker0
# bridge gateway). The SSH tunnel must also listen there:
#   ssh -L 8080:localhost:8080 -L 172.17.0.1:8080:localhost:8080 user@llm-server
#
# All aliases map to the same backend; llama.cpp ignores the model id and key.
# The haiku/sonnet aliases cover Claude Code's background + subagent calls.
model_list:
- model_name: claude-opus-4-8
  litellm_params:
    model: openai/local
    api_base: http://172.17.0.1:8080/v1
    api_key: sk-local-dummy
- model_name: claude-sonnet-4-6
  litellm_params:
    model: openai/local
    api_base: http://172.17.0.1:8080/v1
    api_key: sk-local-dummy
- model_name: claude-haiku-4-5-20251001
  litellm_params:
    model: openai/local
    api_base: http://172.17.0.1:8080/v1
    api_key: sk-local-dummy

litellm_settings:
  drop_params: true   # silently drop Anthropic-only params the local server rejects
```

- [ ] **Step 2: Write the compose file**

`arvo-eval/oss-crs-local/compose-local.yaml` (derived from
`~/oss-crs/example/crs-claude-code/compose-oauth.yaml`; the load-bearing change is
the ABSENT `CLAUDE_CODE_OAUTH_TOKEN` — see `crs-claude-code/agents/claude_code.py:102-115`,
which falls back to `ANTHROPIC_BASE_URL` = the LiteLLM proxy when no OAuth token is set):

```yaml
##############################################################################
#                        CRS Compose Configuration                           #
#   crs-claude-code driven by a LOCAL OpenAI-compatible model via LiteLLM    #
##############################################################################
# Same as compose-oauth.yaml EXCEPT:
#   * CLAUDE_CODE_OAUTH_TOKEN is intentionally NOT set -> Claude Code routes
#     through the OSS-CRS internal LiteLLM proxy instead of Anthropic.
#   * llm_config points at litellm-config-local.yaml, which maps the Claude
#     model aliases to the local server on the host (SSH tunnel).

# --- General Settings -------------------------------------------------------
run_env: local
docker_registry: local

# --- Infrastructure ---------------------------------------------------------
oss_crs_infra:
  cpuset: "0-1"
  memory: "8G"

# --- CRS (crs-claude-code) -------------------------------------------------
crs-claude-code:
  cpuset: "2-7"
  memory: "16G"
  llm_budget: 10
  additional_env:
    CRS_AGENT: claude_code
    # An alias rewritten by LiteLLM (see litellm-config-local.yaml); nothing
    # downstream changes relative to the OAuth setup.
    ANTHROPIC_MODEL: claude-opus-4-8
    # AGENT_TIMEOUT: "3600"                # Optional: seconds, 0 = no limit

# --- LLM Configuration -----------------------------------------------------
llm_config:
  litellm:
    mode: internal
    internal:
      config_path: ./example/crs-claude-code/litellm-config-local.yaml
```

- [ ] **Step 3: Commit**

```bash
cd <repo-root>
git add arvo-eval/oss-crs-local/
git commit -m "feat(oss-crs): compose + LiteLLM configs routing repair agent to local model"
```

---

### Task 2: install.sh and install into ~/oss-crs

OSS-CRS resolves `config_path` relative to its own checkout, so the files must
physically live there. This repo keeps the canonical copies.

**Files:**
- Create: `arvo-eval/oss-crs-local/install.sh`

- [ ] **Step 1: Write install.sh**

```bash
#!/usr/bin/env bash
# Copy the local-model compose + LiteLLM config into the ~/oss-crs checkout.
# OSS-CRS resolves the litellm config_path relative to its own root, so the
# files must live there; this repo keeps the canonical copies. Re-run after
# editing either file.
set -euo pipefail
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${OSS_CRS_DIR:-$HOME/oss-crs}/example/crs-claude-code"
[ -d "$DEST" ] || { echo "error: $DEST not found (is ~/oss-crs cloned?)" >&2; exit 1; }
cp -v "$SRC_DIR/compose-local.yaml" "$SRC_DIR/litellm-config-local.yaml" "$DEST/"
```

- [ ] **Step 2: Make executable and run it**

Run: `chmod +x arvo-eval/oss-crs-local/install.sh && arvo-eval/oss-crs-local/install.sh`
Expected output: two `cp -v` lines ending in `~/oss-crs/example/crs-claude-code/...`

- [ ] **Step 3: Verify the files landed**

Run: `ls ~/oss-crs/example/crs-claude-code/`
Expected: `compose-local.yaml  compose-oauth.yaml  compose.yaml  litellm-config-local.yaml  litellm-config.yaml`

- [ ] **Step 4: Commit**

```bash
cd <repo-root>
git add arvo-eval/oss-crs-local/install.sh
git commit -m "feat(oss-crs): install script for local-model configs"
```

---

### Task 3: Env-overridable compose file in arvo_oss_crs.py (TDD)

**Files:**
- Modify: `arvo-eval/arvo_oss_crs.py:26` (and docstring prerequisites, lines 11-14)
- Test: `arvo-eval/tests/test_arvo_oss_crs.py` (new)

- [ ] **Step 1: Write the failing test**

`arvo-eval/tests/test_arvo_oss_crs.py`:

```python
"""Compose-file selection: local default, OSS_CRS_COMPOSE_FILE override."""
from pathlib import Path

import arvo_oss_crs


def test_compose_file_defaults_to_local(monkeypatch):
    monkeypatch.delenv("OSS_CRS_COMPOSE_FILE", raising=False)
    assert arvo_oss_crs._compose_file() == (
        arvo_oss_crs.OSS_CRS_DIR / "example/crs-claude-code/compose-local.yaml")


def test_compose_file_env_override(monkeypatch):
    monkeypatch.setenv("OSS_CRS_COMPOSE_FILE", "/tmp/other-compose.yaml")
    assert arvo_oss_crs._compose_file() == Path("/tmp/other-compose.yaml")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo-root>/arvo-eval && .venv/bin/python -m pytest tests/test_arvo_oss_crs.py -v`
Expected: FAIL — `AttributeError: module 'arvo_oss_crs' has no attribute '_compose_file'`

- [ ] **Step 3: Implement**

In `arvo-eval/arvo_oss_crs.py`, replace line 26:

```python
COMPOSE_FILE = OSS_CRS_DIR / "example/crs-claude-code/compose-oauth.yaml"
```

with:

```python
def _compose_file() -> Path:
    """OSS_CRS_COMPOSE_FILE overrides (e.g. compose-oauth.yaml to use Claude
    via OAuth); the default is the local-model compose."""
    return Path(os.environ.get(
        "OSS_CRS_COMPOSE_FILE",
        str(OSS_CRS_DIR / "example/crs-claude-code/compose-local.yaml")))


COMPOSE_FILE = _compose_file()
```

(Note: `_compose_file` must be defined AFTER `OSS_CRS_DIR` (line 25). `os` and
`Path` are already imported.)

Also update the module docstring prerequisites (lines 11-14) to:

```
Prerequisites:
    - ~/oss-crs cloned from https://github.com/ossf/oss-crs
    - arvo-eval/oss-crs-local/install.sh run once (installs compose-local.yaml +
      litellm-config-local.yaml into ~/oss-crs); the SSH tunnel to the local model
      must also listen on 172.17.0.1:8080 -- see arvo-eval/README.md
    - Run `uv run oss-crs prepare --compose-file <COMPOSE_FILE>` once first
    - To use Claude via OAuth instead: export CLAUDE_CODE_OAUTH_TOKEN and
      OSS_CRS_COMPOSE_FILE=$HOME/oss-crs/example/crs-claude-code/compose-oauth.yaml
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd <repo-root>/arvo-eval && .venv/bin/python -m pytest tests/test_arvo_oss_crs.py -v`
Expected: 2 passed

- [ ] **Step 5: Run the full test suite (no regressions)**

Run: `cd <repo-root>/arvo-eval && .venv/bin/python -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
cd <repo-root>
git add arvo-eval/arvo_oss_crs.py arvo-eval/tests/test_arvo_oss_crs.py
git commit -m "feat(oss-crs): OSS_CRS_COMPOSE_FILE selects backend; default local model"
```

---

### Task 4: README documentation

**Files:**
- Modify: `arvo-eval/README.md` (after the existing "Local model" paragraph, ~line 203)

- [ ] **Step 1: Add the section**

Insert after the existing local-model paragraph (README.md ~line 203):

```markdown
### Repair agent on a local model (OSS-CRS)

The repair agent is the expensive side; it can run against a local
OpenAI-compatible server (e.g. llama.cpp reached through an SSH tunnel on
`localhost:8080`) instead of Claude:

1. Make the tunnel reachable from Docker containers — add a second `-L`
   binding on the docker bridge gateway (only containers can reach it):

       ssh -L 8080:localhost:8080 -L 172.17.0.1:8080:localhost:8080 user@llm-server

2. Install the configs into `~/oss-crs` and prepare once:

       arvo-eval/oss-crs-local/install.sh
       cd ~/oss-crs && uv run oss-crs prepare --compose-file example/crs-claude-code/compose-local.yaml

3. Run as usual (`arvo_oss_crs.py`, `run_single.py`, ...) — the local compose is
   now the default. Claude Code inside the CRS talks to OSS-CRS's LiteLLM proxy,
   which rewrites the Claude model aliases to `openai/local` at
   `http://172.17.0.1:8080/v1` (see `oss-crs-local/litellm-config-local.yaml`).

To switch back to Claude via OAuth for a run:

    export CLAUDE_CODE_OAUTH_TOKEN=...
    OSS_CRS_COMPOSE_FILE=$HOME/oss-crs/example/crs-claude-code/compose-oauth.yaml python arvo_oss_crs.py

The learning/extractor side (`llm.py`) is unaffected and stays on Claude.
```

- [ ] **Step 2: Commit**

```bash
cd <repo-root>
git add arvo-eval/README.md
git commit -m "docs: local-model repair agent setup (tunnel, install, compose flip)"
```

---

### Task 5: End-to-end verification

Requires the SSH tunnel rebind (user-side) and Docker. Do not claim success
without these outputs.

- [ ] **Step 1: Verify the tunnel is reachable on the bridge address**

Run: `curl -s -m 5 http://172.17.0.1:8080/v1/models | head -c 200`
Expected: JSON model list (same as `localhost:8080`).
If `connection refused`: the tunnel lacks the second binding — ask the user to
restart it with `-L 172.17.0.1:8080:localhost:8080` added, then re-run.

- [ ] **Step 2: Prepare OSS-CRS with the local compose**

Run: `cd ~/oss-crs && uv run oss-crs prepare --compose-file example/crs-claude-code/compose-local.yaml`
Expected: completes without error (builds/pulls infra images).

- [ ] **Step 3: Run one bug end-to-end on the local model**

Run: `cd <repo-root>/arvo-eval && OSS_CRS_BUG_ID=444773339 .venv/bin/python arvo_oss_crs.py`
Expected: build-target + run complete; watch for LiteLLM proxy configured
message in the agent log (`Claude Code configured with LiteLLM proxy`), not OAuth.

- [ ] **Step 4: Confirm artifacts and that traffic hit the local server**

Run: `cat arvo-eval/results/444773339/oss_crs_result.json | head -30`
Expected: valid JSON with `patches` >= 0 and `meta` populated (a 9B model may
produce 0 patches — the pipeline completing and the model being exercised is
what this verifies, not patch quality).
Also check the llama.cpp server / tunnel terminal shows completed requests
during the run.

- [ ] **Step 5: Confirm the OAuth flip still selects the right compose**

Run: `cd <repo-root>/arvo-eval && OSS_CRS_COMPOSE_FILE=$HOME/oss-crs/example/crs-claude-code/compose-oauth.yaml .venv/bin/python -c "import arvo_oss_crs; print(arvo_oss_crs.COMPOSE_FILE)"`
Expected: `$HOME/oss-crs/example/crs-claude-code/compose-oauth.yaml`

- [ ] **Step 6: Final commit if anything was adjusted during verification**

```bash
cd <repo-root> && git status --short
# commit any fixes with an explanatory message
```
