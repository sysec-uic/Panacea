# arvo-eval

The active system: orchestrator, verifier, learning loop, oracle, tests. See the
[root README](../README.md) for the big picture (what Panacea is, the research
questions, how the pieces fit together). This file is the runbook.

## Repo layout

| Path | What's there |
|---|---|
| `learn_loop.py` | The chronological control/treatment repair-and-learn loop (main entry point) |
| `arvo_oss_crs.py` | Drives the OSS-CRS repair agent on one bug (wall-clock cap, docker image cleanup, `HEURISTICS.md` injection) |
| `repair_loop.py` | Per-bug retry loop with deployment-faithful feedback between attempts |
| `verify_fix.py` | Real verification: build, re-run PoC, run tests, classify |
| `differential_oracle.py` | Post-hoc lesson-quality grader vs the `-fix` image |
| `playbook_store.py` / `injector.py` | Playbook state (load/save/render) and `HEURISTICS.md` injection |
| `extract_heuristic.py` / `contrastive_extract.py` | LLM calls that distill a solved bug into a reusable lesson |
| `llm.py` | LLM backend used by the extractor/curator/grader (Claude Code CLI, API key, or local model) |
| `check_server.py` | Host-side responder for the agent's in-turn self-check (in progress) |
| `mruby_bugs.py` / `build_instance.py` | Bug ordering and per-bug ARVO instance loading |
| `results/` | Git-ignored. Per-bug outputs and `results/learn/ledger.jsonl` |
| `playbook/` | Tracked. Accumulated `playbook_state_<pass>.json` per pass |
| `tests/` | Pure-logic unit tests, no Docker/network |
| [`legacy/`](legacy/README.md) | Pre-`learn_loop.py` single-bug runner (mini-SWE-agent). Superseded; kept for reference |
| [`transfer/`](transfer/README.md) | Paused cross-project heuristic-transfer experiment |

---

# OSS-CRS Pipeline (crs-claude-code)

Uses [OSS-CRS](https://github.com/ossf/oss-crs) with Claude Code as the patching agent. Won DARPA AIxCC. This is the repair-agent mechanism underneath both the learning loop and the legacy single-bug runner.

### Prerequisites

- Clone OSS-CRS: `git clone https://github.com/ossf/oss-crs ~/oss-crs`
- Run `uv run oss-crs prepare --compose-file ~/oss-crs/example/crs-claude-code/compose-oauth.yaml` once
- Set `CLAUDE_CODE_OAUTH_TOKEN` in your shell (Claude Pro/Max OAuth token)
- **Ubuntu 20.04+ ARVO images only** — older images (e.g. wget2, bug 42470179) use Ubuntu 16.04 and are incompatible with OSS-CRS's install step

### Running

```bash
OSS_CRS_BUG_ID=435781342 python3 arvo_oss_crs.py
```

On subsequent runs, skip the Docker build step (reuses cached snapshot):

```bash
OSS_CRS_BUG_ID=435781342 python3 arvo_oss_crs.py --skip-build
```

If using a different database (e.g. `arvo_new.db`, available at https://github.com/sysec-uic/Panacea/releases/tag/ARVO_New_in_prog):
```bash
ARVO_DB_PATH=arvo_new.db OSS_CRS_BUG_ID=439279102 python3 arvo_oss_crs.py
```

Results are saved to `results/<bug_id>/oss_crs_result.json`. Patches are copied to `results/<bug_id>/oss_crs_patch_N.diff`. The agent's stdout log is saved to `results/<bug_id>/oss_crs_claude_stdout.log`. When running via `learn_loop.py` with `LEARN_PASS` set, results are namespaced under `results/<pass>/<bug_id>/` so control and treatment runs don't overwrite each other.

### How it works

ARVO images don't match OSS-Fuzz's expected project format, so `arvo_oss_crs.py` generates a fake OSS-Fuzz project directory wrapping the ARVO Docker image (no-op `build.sh` since binaries are pre-compiled), extracts the POC from `/tmp/poc`, and drives OSS-CRS build + agent run. OSS-CRS handles the incremental build loop internally — after the first build it snapshots the container so patch attempts are fast.

### Token counts

Token usage is captured automatically per bug in `results/learn/ledger.jsonl` (the `tokens`
field). To view a summary across all runs:

```bash
cat results/learn/ledger.jsonl | python3 -c "
import json, sys
print(f'{'BUG_ID':<15} {'INPUT':>8} {'OUTPUT':>8} {'CACHE_READ':>12} {'CACHE_WRITE':>12}')
for line in sys.stdin:
    r = json.loads(line)
    t = r.get('tokens', {})
    print(f\"{r['bug_id']:<15} {t.get('input_tokens',0):>8,} {t.get('output_tokens',0):>8,} {t.get('cache_read_tokens',0):>12,} {t.get('cache_write_tokens',0):>12,}\")
"
```

### Cost

Each run uses Claude Opus 4.8. Typical token usage for a successful patch (~300–700 s),
measured across 8 mruby control bugs:

| Metric | Typical range |
|--------|--------------|
| Input tokens | ~2.5K–9K |
| Output tokens | ~500–1K |
| Cache-read tokens | ~1M–3.5M |
| Cache-write tokens | ~35K–90K |

Output tokens are low because Claude Code works by making short tool calls rather than
generating long text. Cache-read dominates cost as the conversation grows across turns.
Use `--skip-build` on reruns to avoid rebuilding the Docker snapshot.

---

# Heuristic Learning Loop (mruby)

A self-improving agent memory over mruby's 30 ARVO bugs, run in chronological
(`localId`) order. After each verified-correct fix, an LLM extracts a reusable
heuristic into a playbook that is injected into the agent's context on later bugs.
Because a bug's lesson is only added *after* it is evaluated, no bug is ever tested
against a playbook containing its own answer (chronological holdout).

Design: `docs/superpowers/specs/2026-06-29-mruby-heuristic-learning-loop-design.md`
Plan:   `docs/superpowers/plans/2026-06-29-mruby-heuristic-learning-loop.md`

### Prerequisites

- `arvo_new.db` present in this directory (download from the
  [ARVO_New release](https://github.com/sysec-uic/Panacea/releases/tag/ARVO_New_in_prog)).
- `CLAUDE_CODE_OAUTH_TOKEN` — the OSS-CRS patching agent (see the OSS-CRS section above).
- **Extractor/curator/grader backend** (`llm.py`) — selected automatically:
  - *Subscription only (no API key):* `llm.py` drives the **Claude Code CLI** (`claude -p`)
    on the same login/subscription as the repair agent — no separate API key needed. This
    is the default whenever `ANTHROPIC_API_KEY` and `LLM_BASE_URL` are unset and `claude`
    is on `PATH`. (The raw `/v1/messages` API rejects subscription OAuth tokens with a 429,
    so do **not** rely on the OAuth token for the extractor's raw-API path — use the CLI.)
  - *API key:* set `ANTHROPIC_API_KEY` to bill the extractor to the API instead. You may
    export it alongside the OAuth token — `llm.py` prefers the key and only ever sends one
    credential per request, so there's no dual-auth rejection.
  - *Local model:* set `LLM_BACKEND=openai` (or just export `OPENAI_BASE_URL`) to use an
    OpenAI-compatible server — defaults to `http://localhost:8080/v1`, the llama.cpp SSH
    tunnel the repair agent's local-model setup uses. For an Anthropic-compatible server
    set `LLM_BASE_URL` instead.
  - Force a backend explicitly with `LLM_BACKEND=api|openai|claude_cli`.
- The mruby correctness gate runs `cd /src/mruby && rake test`
  (see `MRUBY_TEST_CMD` in `verify_fix.py`).
- The playbook is injected as `HEURISTICS.md` written by `injector.py` into the
  per-bug project dir, then copied by `arvo_oss_crs.py` into the agent's actual
  working directory (the OSS-CRS `target-source` dir) before the run starts
  (see `inject_heuristics` in `arvo_oss_crs.py`).

### Repair loop & learning

Each bug gets up to `LEARN_MAX_ATTEMPTS` (default 5) repair attempts. Between attempts
the agent is re-run with **deployment-faithful feedback only** — the crash trace and the
failing `make test` output, never the `-fix` image (the system must keep working on bugs
that have no known fix). A lesson is learned only from solved bugs:

- solved after a failure → a **contrastive** lesson from the agent's own rejected vs
  accepted attempts (`contrastive_extract.py`); rendered as a "Don't / Do" entry.
- solved on the first try → a plain success lesson (`extract_heuristic.py`).

The ledger records `n_attempts` per bug — watch it trend down on later bugs if the
playbook is helping. See `demo_retry_learn.py` for a stub-driven walk-through.

Set `OSS_CRS_RUN_TIMEOUT=<seconds>` to cap the wall-clock time of a single agent run
(unset = no cap). On a timeout the orphaned OSS-CRS compose containers are torn down
and the attempt is recorded as a no-patch, `timed_out` attempt — the next attempt gets
feedback telling the agent to commit to a fix. This stops one flailing attempt from
eating many hours (the local-model campaign had a single attempt run 36h; see
`docs/2026-07-13-learn-loop-local-model-campaign.md`).

### Running the experiment

Two passes over the same chronological ordering — a control (no playbook injected)
and a treatment (injected):

```bash
# Smoke-test on a few bugs first (cheap) before the full multi-hour run:
ARVO_DB_PATH=arvo_new.db LEARN_PASS=treatment python3 learn_loop.py --limit 3
# or specific bugs:
ARVO_DB_PATH=arvo_new.db LEARN_PASS=treatment python3 learn_loop.py --bugs 439494108,449429295

# Full experiment:
ARVO_DB_PATH=arvo_new.db LEARN_PASS=control   python3 learn_loop.py
ARVO_DB_PATH=arvo_new.db LEARN_PASS=treatment python3 learn_loop.py
```

Per-run records accumulate in `results/learn/ledger.jsonl`. The accumulated playbook
state is `playbook/playbook_state_<pass>.json`.

**Local model:** `llm.py` reads `LLM_MODEL` and `LLM_BASE_URL` from the environment, so
point it at a local Anthropic-compatible endpoint without code changes. For an
OpenAI-compatible server (llama.cpp via the SSH tunnel, or a LiteLLM proxy) set
`LLM_BACKEND=openai` — it defaults to `http://localhost:8080/v1` with a dummy key,
matching the repair agent's `litellm-config-local.yaml`; override with
`OPENAI_BASE_URL`/`OPENAI_API_KEY`.

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

3. Run as usual (`arvo_oss_crs.py`, ...) — the local compose is
   now the default. Claude Code inside the CRS talks to OSS-CRS's LiteLLM proxy,
   which rewrites the Claude model aliases to `openai/local` at
   `http://172.17.0.1:8080/v1` (see `oss-crs-local/litellm-config-local.yaml`).

To switch back to Claude via OAuth for a run:

    export CLAUDE_CODE_OAUTH_TOKEN=...
    OSS_CRS_COMPOSE_FILE=$HOME/oss-crs/example/crs-claude-code/compose-oauth.yaml python arvo_oss_crs.py

The learning/extractor side (`llm.py`) is unaffected and stays on Claude.

> **Note:** `results/` is git-ignored, so the ledger is a local artifact. The
> `playbook/` directory is tracked — commit the resulting playbook if you want to
> share what the system learned.

### Reading the result

Compare the verified-correct rate on the later ~two-thirds of bugs (where an
accumulated playbook exists) between the two passes:

```bash
PYTHONPATH=. python3 -c "
from ledger import read_records
r = read_records('results/learn/ledger.jsonl')
for p in ('control','treatment'):
    rows = [x for x in r if x['pass']==p]
    tail = rows[len(rows)//3:]   # later two-thirds
    ok = sum(1 for x in tail if x['classification']=='verified_correct')
    print(p, 'tail verified_correct:', ok, '/', len(tail))
"
```

N=30 (≈20 in the holdout tail) is a **pilot** — read the control/treatment delta as
directional signal, not statistical proof. See [`../EVALUATION.md`](../EVALUATION.md)
for current results.

---

## Unit tests

The pure-logic pieces (store, ledger, injector, verifier classification, extractor,
curator, orchestrator, transfer filter/eval-set/analysis) are covered without Docker
or network:

```bash
PYTHONPATH=. python3 -m pytest tests -q
```
