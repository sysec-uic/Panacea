# Panacea

> *Panacea* — a remedy for all difficulties; a cure-all.

An agentic system for **automatic vulnerability repair in C/C++**. Panacea takes real,
reproducible crashes from fuzzing (the [ARVO](https://github.com/n132/arvo) dataset of
OSS-Fuzz bugs), drives an AI repair agent to fix each one, verifies the fix for real, and
— the research bet — **learns from each fix so it gets better at the next one**.

---

## Current status (2026-07-13)

**A live campaign is running** and the pipeline is in its best shape yet. The headline
work this session:

| Area | State |
|---|---|
| Repair + learning pipeline | ✅ Built and hardened (real verification, harness-patch rejection, stale-file guards, docker cleanup) |
| Per-run wall-clock cap | ✅ Shipped & validated live — one attempt can no longer run 36h (`OSS_CRS_RUN_TIMEOUT`) |
| Local-LLM serving speed | ✅ Tuned (flash-attention + KV-quant + 2 slots). Early result: **~1.5 min/cycle vs the old ~25 min**. High-context (>100k) confirmation in progress via the running campaign |
| In-turn self-check (`check-patch`) | 🚧 Engine + host responder built & tested; final wiring pending live validation |
| Differential `-fix` oracle | ✅ Implemented (post-hoc lesson-quality gate) |
| Cross-project transfer experiment | 📋 Designed, not yet run |

**Why it matters:** a July 10–13 campaign on a local model was killed after ~57h having
solved 0 of 3 bugs — but the diagnosis was that the *model was too slow*, not that the
loop was wrong. This session attacked both remaining blockers: **runaway attempts**
(fixed with the timeout cap) and **serving speed** (tuned, now being confirmed at long
context). The next blocker in line is that the agent **can't test its own patches**, which
the in-progress `check-patch` tool addresses.

See [`docs/2026-07-13-learn-loop-local-model-campaign.md`](docs/2026-07-13-learn-loop-local-model-campaign.md)
for the full postmortem and next steps.

---

## How it works

For each bug, in chronological order:

```
   playbook of past lessons ──inject──▶ [ AI repair agent (OSS-CRS / Claude Code) ]
                                                     │ writes a patch
                                                     ▼
                        verify: fresh build ▸ re-run the crash input ▸ run the test suite
                                                     │
                          ┌──────────── solved? ─────┴─── not solved ───────┐
                          ▼                                                  ▼
        differential oracle grades the fix                   feed back what went wrong,
        against the canonical upstream `-fix`                retry (up to N attempts)
                          │
             extract a reusable lesson ──▶ add to the playbook ──▶ next bug sees it
```

Two guarantees hold the research together:

- **Holdout / no leakage.** A bug is only ever tested against lessons learned from
  *strictly earlier* bugs — a bug never sees its own answer.
- **Deployment faithfulness.** The agent only ever gets signals a real developer would
  have (the crash, the failing tests). It **never** sees the known-good upstream fix; that
  `-fix` image is used only *after the fact*, by a grader the agent can't reach, to judge
  whether a learned lesson is trustworthy.

---

## The research questions

Each has an approved design doc under [`docs/superpowers/specs/`](docs/superpowers/specs/):

1. **Does an agent memory help?** — the [mruby heuristic learning loop](docs/superpowers/specs/2026-06-29-mruby-heuristic-learning-loop-design.md).
   mruby has 30 bugs with a dominant recurring bug family, so accumulated lessons should
   lift the later fix rate. Measured control (no memory) vs treatment (memory).
2. **Are the learned lessons trustworthy?** — the [differential `-fix` oracle](docs/superpowers/specs/2026-06-29-differential-fix-oracle-design.md).
   "Crash gone + tests pass" can still bless a subtly-wrong patch; the oracle compares
   against the canonical upstream fix and suppresses lessons drawn from divergent patches.
3. **Do lessons transfer across projects?** — the [cross-project transfer experiment](docs/superpowers/specs/2026-06-30-cross-project-transfer-experiment-design.md).
   Most projects have too few bugs for their own memory; this pilot tests whether a lesson
   from one project's bug helps a *different* project's bug of the same crash class.
4. **Can it run cheaply on a local model?** — the [OSS-CRS local-LLM setup](docs/superpowers/specs/2026-07-07-oss-crs-local-llm-design.md).
   The expensive repair agent runs against a local GPU-served model via an SSH tunnel and
   LiteLLM; the quality-critical lesson extraction stays on Claude.

---

## Repository map

| Path | What's there |
|---|---|
| `arvo-eval/` | The system. Orchestrator, verifier, learning loop, oracle, tests. **Start with [`arvo-eval/README.md`](arvo-eval/README.md) to run anything.** |
| `arvo-eval/learn_loop.py` | The chronological repair-and-learn loop |
| `arvo-eval/arvo_oss_crs.py` | Drives the OSS-CRS repair agent on one bug (timeout cap, docker cleanup live here) |
| `arvo-eval/verify_fix.py` | Real verification: build ▸ re-run PoC ▸ run tests ▸ classify. Also the `run_check` engine |
| `arvo-eval/check_server.py` | Host-side responder for the agent's in-turn self-check (in progress) |
| `arvo-eval/differential_oracle.py` | Post-hoc lesson-quality grader vs the `-fix` image |
| `docs/` | The postmortem, design specs, and implementation plans |

---

## Running it

The full, current runbook — prerequisites, the SSH tunnel, and the exact launch
commands — lives in **[`arvo-eval/README.md`](arvo-eval/README.md)**. In short:

```bash
cd arvo-eval
# smoke test first (a few bugs, short cap), then the real thing:
ARVO_DB_PATH=arvo_new.db LEARN_PASS=treatment \
OSS_CRS_RUN_TIMEOUT=7200 LEARN_MAX_ATTEMPTS=3 \
python3 learn_loop.py --limit 3
```

Results accumulate in `arvo-eval/results/learn/ledger.jsonl`; the accumulated playbook of
learned lessons is under `arvo-eval/playbook/`.
