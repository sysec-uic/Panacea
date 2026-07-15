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

## Current results (in progress, 2026-07-15)

The full 30-bug pass isn't finished for either arm yet. Numbers below cover only
bugs whose classification has been **independently re-verified** under a corrected
verification gate (see "Verification integrity" below):

| | Bugs confirmed | Fix rate | `oracle_confirmed` |
|---|---|---|---|
| **Control** | 14 / 30 | 14 / 14 (100%) | 14 / 14 |
| **Treatment** | 14 / 30 | 14 / 14 (100%) | 14 / 14 |

Both arms are still 100% fix rate with no failures recorded, so there's no
fix-rate delta to report yet (a ceiling effect isn't surprising this early --
these are individually well-scoped bugs). The more informative signal so far is
efficiency: comparing the 4 bugs both arms have completed within the same
chronological window (the last ~20 of the 30 bugs, where the playbook has had
time to accumulate real content -- see `arvo-eval`'s CLAUDE.md), treatment
solved all 4 on the first attempt, while control needed 2-3 attempts on two of
them. That's directional, not conclusive, at n=4. This table and the efficiency
comparison will be updated as both passes progress.

## Verification integrity

Two real correctness-gate bugs were found and fixed while validating results, both
worth understanding before trusting any fix-rate number from this pipeline:

1. **The verification gate wasn't gating.** An earlier version of the pipeline
   classified any non-empty diff as `verified_correct` without actually rebuilding
   and re-testing it. Fixed upstream; every result reported above was independently
   re-verified against a fresh vulnerable container (rebuild, PoC re-run, `rake test`)
   after the fix landed.
2. **The correctness gate itself had a bug specific to non-default sanitizers.**
   `rake test` was reusing the fuzzer/sanitizer-instrumented `libmruby.a` from the
   patch-rebuild step, so a plain test binary with no fuzzing driver failed to link
   against it, spuriously flagging 6 genuinely-correct patches (every `afl`- or
   `msan`-tagged bug) as failing. Fixed by rebuilding cleanly for the test step
   with no fuzzing instrumentation; confirmed empirically against both an AFL and
   an MSan bug. One of those 6 flagged bugs (`440058794`) turned out to be a
   **real** regression even after the fix. This was confirmed by reproducing the same
   failure with the patch applied but not against the unpatched baseline, and is
   being re-run through the full repair loop rather than left as a single-shot
   verdict.

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
