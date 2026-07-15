# Evaluation Plan & Results

## Question

Does injecting a playbook of lessons, extracted from earlier solved bugs, help
Claude Code fix later bugs faster or more often, compared to giving it no memory at
all?

## Dataset

30 mruby bugs from [ARVO](https://github.com/n132/ARVO), a dataset of real,
reproducible OSS-Fuzz bugs with known fixes. Each bug ships a vulnerable Docker
image (`n132/arvo:{id}-vul`), a crashing proof-of-concept input, and (for bugs
where one exists) a fix image (`n132/arvo:{id}-fix`) used only for independent
grading, never shown to the agent.

Bugs are processed in **chronological order** (`localId`, matching OSS-Fuzz issue
ids) rather than random order. This matters for the treatment arm: a heuristic is
only added to the playbook *after* the bug it was learned from is solved, so no bug
is ever tested against a playbook containing its own answer.

## Method: two passes, same bugs, same order

- **Control**: the agent gets no memory between bugs. This is the baseline.
- **Treatment**: after each solved bug, an LLM extracts a reusable lesson (a plain
  success lesson, or a contrastive "don't do X, do Y" lesson if the bug needed a
  failed attempt first) and appends it to a playbook. The playbook is rendered and
  injected into the agent's working directory as `HEURISTICS.md` on every
  subsequent bug in that pass.

Both passes use the identical repair loop: up to 5 attempts per bug, with
deployment-faithful feedback (crash trace and failing test output, never the fix
image) fed back between attempts. Retries are symmetric across both arms; a bug
needing multiple attempts is not special-cased, and `n_attempts` is recorded for
every bug.

## Metrics

| Metric | What it measures |
|--------|-------------------|
| **Fix rate** (`verified_correct` / total attempted) | The primary metric. A patch is `verified_correct` only if it applies cleanly to a fresh vulnerable container, rebuilds, makes the crash go away, and passes mruby's own correctness test suite (`rake test`), not just "the agent produced a diff." |
| **`oracle_confirmed` rate** | An independent cross-check: the patch is also compared against the real `-fix` image's behavior (6 probe scripts + the original PoC), never shown to the agent. Confirms the fix isn't a false positive (e.g. a patch that silences the crash by weakening the test harness itself, rather than fixing the bug). |
| **`n_attempts`** (solved bugs only) | Efficiency: how many of the 5 allowed attempts it took. Lower is better; watch whether treatment trends down relative to control as the playbook accumulates. |
| **Token counts** (input, output, cache-read, cache-write) | Cost/effort proxy, recorded per bug in the ledger. Cache-read dominates and scales with conversation length (more tool calls, more retries), so it's a more sensitive efficiency signal than fix rate at this sample size. |

## Current results (in progress, 2026-07-15)

The full 30-bug pass isn't finished for either arm yet. Numbers below cover the
14 bugs both arms have completed **identically** (matched pairs), each
independently re-verified under a corrected verification gate (see
"Verification methodology" below):

| | Bugs confirmed | Fix rate | `oracle_confirmed` |
|---|---|---|---|
| **Control** | 14 / 30 | 14 / 14 (100%) | 14 / 14 |
| **Treatment** | 14 / 30 | 14 / 14 (100%) | 14 / 14 |

Both arms are still 100% fix rate with no failures recorded, so there's no
fix-rate delta to report yet (a ceiling effect isn't surprising this early --
these are individually well-scoped bugs). The signal so far is in effort, not
outcome:

| Metric (avg. per bug, matched pairs) | Control | Treatment |
|---|---|---|
| Attempts | 1.29 | 1.00 |
| Input tokens | 3,765 | 3,295 |
| Output tokens | 857 | 903 |
| Cache-read tokens | 2,656,600 | 2,475,208 |
| Wall-clock | 778s | 657s |

That gap isn't flat across the run -- it grows as the playbook accumulates
content. Splitting the same 14 bugs at chronological position 10 (of 30):
early on (positions 1-10, playbook has 0-9 heuristics) attempts go from 1.11
to 1.00 and cache-read is essentially a wash (treatment is actually 1.3%
*higher*, since the injected playbook adds fixed context weight before it's
paid for itself). By position 11+ (playbook has 11+ heuristics), attempts go
from 1.60 to 1.00 (-37%) and cache-read drops 15%. At n=5 for the late split,
that trend is directional, not conclusive -- but it's the strongest evidence
so far that the mechanism is doing something, not just adding noise.

## Verification methodology

Every classification above is independently re-verified against a fresh
vulnerable container -- never taken on the agent's own say-so. For each
accepted patch: apply the diff, rebuild clean (no fuzzing instrumentation for
the correctness gate), re-run the original crash PoC, and run the project's
own test suite (`rake test`). Only a patch that clears all of that is
`verified_correct`. The differential oracle then re-grades it a second way,
independently: rebuild the patch in isolation and compare its behavior
against the real upstream `-fix` image (never shown to the agent) on the PoC
plus 6 deterministic probe scripts, catching a patch that silences the crash
without actually fixing the underlying bug.

## Why N=30 is a pilot, not proof

Even at 30/30 for both arms, this is a **pilot-scale** comparison. Read any
control/treatment delta as directional signal, not statistical proof. The value
here is establishing whether the mechanism works at all and building the
infrastructure (holdout-safe playbook injection, independent verification, a
differential oracle) needed to scale the same method to a larger bug set or more
projects later. See `arvo-eval/transfer/` for the (currently paused) cross-project
extension of this idea.

## Running it yourself

See [`arvo-eval/README.md`](arvo-eval/README.md) for setup and the exact commands.
