# Panacea

*A remedy for all diseases, evils, or difficulties; a cure-all.*

An agentic tool for automatic vulnerability repair in C/C++, evaluated against
[ARVO](https://github.com/n132/ARVO) (a dataset of real, reproducible OSS-Fuzz bugs
with known fixes).

## Current focus: does a learned playbook help an agent fix bugs faster?

The active work is a heuristic-learning experiment in [`arvo-eval/`](arvo-eval/):
we run [OSS-CRS](https://github.com/ossf/oss-crs) (with Claude Code as the patching
agent) over 30 chronologically-ordered mruby bugs from ARVO, once for control, and once for treatment.

- **Control** — no memory between bugs (baseline)
- **Treatment** — after each solved bug, an LLM extracts a reusable lesson and
  injects the accumulated playbook into later bugs' agent context (chronological
  holdout: a bug never sees its own lesson)

See [`EVALUATION.md`](EVALUATION.md) for the full methodology and current results,
and [`arvo-eval/README.md`](arvo-eval/README.md) for how to run it.

## Repo layout

| Path | What it is |
|------|------------|
| [`arvo-eval/`](arvo-eval/) | **Active experiment.** The control/treatment heuristic-learning pipeline, oracle, and playbook. Two subfolders hold work outside the active pipeline: `legacy/` (the pre-`learn_loop.py` single-bug runner) and `transfer/` (a paused cross-project transfer-learning track) — each has its own README. |
| [`arvo-expanded/`](arvo-expanded/) | Tooling that builds the ARVO bug database (`arvo_new.db`) from ARVO's Docker image tags, across all projects. Run once and upload the result to GitHub Releases; `arvo-eval/` downloads it from there. See its own README. |
| [`early-investigations/`](early-investigations/) | Manual/semi-automated investigations of individual ARVO bugs, from before the control/treatment pipeline existed. Superseded by `arvo-eval/` for anything it covers; kept for the write-ups. |
| [`docs/superpowers/`](docs/superpowers/) | Design specs and plans for the pipeline components (differential oracle, heuristic-learning loop, local-LLM setup, cross-project transfer). |

## Setup

Each subproject has its own prerequisites. See `arvo-eval/README.md` for the
current one. In short: Python 3, Docker, an ARVO bug database (download from
[releases](https://github.com/sysec-uic/Panacea/releases)), and a Claude
Pro/Max subscription (OAuth) or Anthropic API key.
