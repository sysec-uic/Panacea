# Panacea

> *Panacea*: a remedy for all difficulties; a cure-all.

An agentic system for **automatic vulnerability repair in C/C++**. Panacea takes real,
reproducible crashes from fuzzing (the [ARVO](https://github.com/n132/ARVO) dataset of
OSS-Fuzz bugs), drives an AI repair agent to fix each one, verifies the fix for real,
and, as the research bet, **learns from each fix so it gets better at the next one**.

## Current status (2026-07-15)

Two parallel efforts are running the same experiment on different backends:

- **Claude/OAuth pass:** control 13/30 confirmed, treatment 3/30, both 100% fix rate
  after independent re-verification. Two control bugs are being re-run after a real
  correctness-gate bug (spurious failures on `afl`/`msan` bugs) was found and fixed.
  See [`EVALUATION.md`](EVALUATION.md) for the full methodology and current results.
- **Local-model campaign:** the bottleneck has moved twice as each layer was fixed.
  A July 10-13 run (0 of 3 bugs in ~57h) pointed at raw serving speed. Two Jul 14-15
  runs pointed at agent behavior: the model stalled in read-only recon, once trapping
  itself in plan mode for 26+ minutes, and never submitted a patch. A Jul 15 campaign
  shipped countermeasures for that (see below) and surfaced the next layer: even when
  the agent works productively, it is fixing blind. It receives the raw proof-of-crash
  input but not the sanitizer trace, and it cannot rebuild the fuzzer in its own
  container to reproduce the crash, so it guesses the wrong subsystem instead of
  following the trace to the faulting frame. See the postmortems
  [`docs/2026-07-13-learn-loop-local-model-campaign.md`](docs/2026-07-13-learn-loop-local-model-campaign.md)
  and
  [`docs/2026-07-15-check-patch-gate-live-validation.md`](docs/2026-07-15-check-patch-gate-live-validation.md).

Infrastructure hardened along the way, shared by both passes:

- **Per-run wall-clock cap** (`OSS_CRS_RUN_TIMEOUT`) so one flailing attempt cannot
  eat 36h. Shipped and validated live.
- **In-turn self-check** (`check-patch`, behind `OSS_CRS_CHECK_PATCH=1`): a host-side
  responder builds the sanitizer target with the agent's current edits, re-runs the
  crash input, and runs the test suite, giving the agent a PASS/FAIL signal mid-session
  instead of only at the end of a run. Built, enforced, and validated live at the
  infrastructure level (channel wiring, warm check container, stale-directory race fix).
- **Agent-behavior fixes** (Jul 15): plan mode disabled at the tool level so a weak
  model cannot stall in it, the `check-patch` guidance reframed as the primary
  edit-then-check-then-iterate loop, and `HEURISTICS.md` made discoverable so the agent
  reads its task guidance instead of overlooking it. The Jul 15 run confirmed all three
  hold in practice, and that turn latency now stays flat (~2 to 4 min per turn) as
  context grows past 250 KB, versus the old decay to ~6.5 min per turn.

The open lever for the next run: hand the agent the sanitizer crash trace at startup
(the harness already has it) rather than making it reproduce the crash blind.

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
  *strictly earlier* bugs; a bug never sees its own answer.
- **Deployment faithfulness.** The agent only ever gets signals a real developer would
  have (the crash, the failing tests). It **never** sees the known-good upstream fix; that
  `-fix` image is used only *after the fact*, by a grader the agent can't reach, to judge
  whether a learned lesson is trustworthy.

## The research questions

Each has an approved design doc under [`docs/superpowers/specs/`](docs/superpowers/specs/):

1. **Does an agent memory help?** The [mruby heuristic learning loop](docs/superpowers/specs/2026-06-29-mruby-heuristic-learning-loop-design.md).
   mruby has 30 bugs with a dominant recurring bug family, so accumulated lessons should
   lift the later fix rate. Measured control (no memory) vs treatment (memory); see
   [`EVALUATION.md`](EVALUATION.md) for current numbers.
2. **Are the learned lessons trustworthy?** The [differential `-fix` oracle](docs/superpowers/specs/2026-06-29-differential-fix-oracle-design.md).
   "Crash gone + tests pass" can still bless a subtly-wrong patch; the oracle compares
   against the canonical upstream fix and suppresses lessons drawn from divergent patches.
3. **Do lessons transfer across projects?** The [cross-project transfer experiment](docs/superpowers/specs/2026-06-30-cross-project-transfer-experiment-design.md).
   Most projects have too few bugs for their own memory; this pilot tests whether a lesson
   from one project's bug helps a *different* project's bug of the same crash class.
   Currently paused; see `arvo-eval/transfer/`.
4. **Can it run cheaply on a local model?** The [OSS-CRS local-LLM setup](docs/superpowers/specs/2026-07-07-oss-crs-local-llm-design.md).
   The expensive repair agent runs against a local GPU-served model via an SSH tunnel and
   LiteLLM; the quality-critical lesson extraction stays on Claude.

## Repo layout

| Path | What's there |
|---|---|
| [`arvo-eval/`](arvo-eval/) | **The system.** Orchestrator, verifier, learning loop, oracle, tests. **Start with [`arvo-eval/README.md`](arvo-eval/README.md) to run anything.** Two subfolders hold work outside the active pipeline: `legacy/` (the pre-`learn_loop.py` single-bug runner) and `transfer/` (the paused cross-project experiment); each has its own README. |
| [`arvo-expanded/`](arvo-expanded/) | Tooling that builds the ARVO bug database (`arvo_new.db`) from ARVO's Docker image tags, across all projects. Run once and upload the result to GitHub Releases; `arvo-eval/` downloads it from there. |
| [`early-investigations/`](early-investigations/) | Manual/semi-automated investigations of individual ARVO bugs, from before the control/treatment pipeline existed. Superseded by `arvo-eval/` for anything it covers; kept for the write-ups. |
| [`docs/`](docs/) | Postmortems, design specs, and implementation plans. |

Notable files inside `arvo-eval/`:

| Path | What's there |
|---|---|
| `learn_loop.py` | The chronological repair-and-learn loop |
| `arvo_oss_crs.py` | Drives the OSS-CRS repair agent on one bug (wall-clock cap, docker image cleanup) |
| `verify_fix.py` | Real verification: build, re-run PoC, run tests, classify. Also the `run_check` engine shared with the in-turn self-check |
| `check_server.py` | Host-side responder for the agent's in-turn `check-patch` self-check |
| `differential_oracle.py` | Post-hoc lesson-quality grader vs the `-fix` image |

## Running it

The full, current runbook (prerequisites, the OAuth/local-model split, and the exact
launch commands) lives in **[`arvo-eval/README.md`](arvo-eval/README.md)**. In short:

```bash
cd arvo-eval
# smoke test first (a few bugs, short cap), then the real thing:
ARVO_DB_PATH=arvo_new.db LEARN_PASS=treatment \
OSS_CRS_RUN_TIMEOUT=7200 \
python3 learn_loop.py --limit 3
```

Results accumulate in `arvo-eval/results/learn/ledger.jsonl`; the accumulated playbook of
learned lessons is under `arvo-eval/playbook/` (committed only as a finished snapshot, not
mid-experiment).
