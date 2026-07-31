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

## Current results (in progress, snapshot 2026-07-29)

**This is a live snapshot, not a final result.** Treatment has completed every
runnable bug (27/27; 30 minus 3 permanent environment-level skips). Control has
completed 26/27 -- one bug, `455612769`, is mid-rerun (see note below) and is
excluded from the numbers here until it lands. Numbers below cover the 26 bugs
both arms have completed **identically** (matched pairs):

| | Bugs confirmed | Fix rate | `oracle_confirmed` |
|---|---|---|---|
| **Control** | 26 / 30 | 26 / 26 (100%) | 26 / 26 |
| **Treatment** | 27 / 30 | 27 / 27 (100%) | 27 / 27 |

Both arms remain at 100% fix rate on every bug either has actually completed.
That's a real limitation worth stating plainly: with zero failures on either arm,
this dataset so far can only speak to **efficiency** (attempts, tokens), not to
whether the playbook changes whether a bug gets solved at all.

| Metric (avg. per bug, matched pairs, n=26) | Control | Treatment |
|---|---|---|
| Attempts | 1.19 | 1.00 |
| Total tokens (sum, all 26) | 64,767,282 | 61,254,096 |
| Mean tokens/bug | 2,491,049 | 2,355,927 |

**Where the token gap actually comes from:** splitting the 26 matched bugs into
"both arms solved in 1 attempt" (22 bugs) vs "at least one arm needed a retry"
(4 bugs: `448702064`, `472538295`, `472567524`, `473582282`) shows the savings
are NOT a general per-attempt efficiency gain. On the 22 one-shot-both bugs, the
two arms are within ~3% of each other in aggregate tokens (control 56.1M vs
treatment 54.4M) -- close to a coin flip, not a systematic per-attempt
advantage. All of the net savings come from the handful of bugs where control
needed extra attempts and treatment didn't -- treatment has not needed a retry
on any matched bug so far (avg attempts across all 26 matched pairs is exactly
1.00). This reframes the mechanism: **the playbook isn't making individual
attempts cheaper, it's preventing some bugs from needing a retry at all** --
closer to "pattern-matching against a known bug family" than "generally sharper
reasoning."

**Note on `462331852`:** this bug originally needed 3 attempts on control and
surfaced two of the confirmed retry-causes below (a safeguard refusal and a
container OOM). It was later rerun from a clean slate to get fully auto-archived
per-attempt logs (the original run predated that capability and only the last
attempt's logs survived); the rerun solved in 1 attempt on both control and
treatment, and is reflected as a 1-attempt bug in the table above. The refusal
and OOM findings below remain real, confirmed observations from that earlier
run -- they just no longer show up in `462331852`'s own attempt count, which is
itself more evidence for the non-determinism point made below.

**Retry-cause investigation:** manually reading archived per-attempt transcripts
(a capability added 2026-07-23; earlier multi-attempt runs only kept the last
attempt's logs) surfaced that "needed a retry" conflates several very different
causes that the ledger's `n_attempts` field cannot currently distinguish:
- a genuine Anthropic cyber-safeguard refusal, confirmed directly in two
  transcripts (`462331852`'s original run, `472567524` attempt 1 -- both
  control, both triggered on ordinary, benign actions like decoding PoC bytes)
- an apparent container OOM-kill (exit code 137) mid-verification on
  `462331852`'s original run, after the agent had already root-caused the bug
  correctly and built what looks like a working fix
- two bugs (`446362556`, `462331852`) that each took multiple attempts on one
  run and only 1 attempt on an identical rerun -- confirmation that `n_attempts`
  has real run-to-run non-determinism, not just a fixed property of a given bug
- a real, pipeline-level bug (fixed in PR #18, `fix/usage-limit-diff-check`):
  `repair_loop.py` was silently discarding an entire attempt -- including a real,
  working diff -- whenever a `usage_limit` signal arrived, before checking
  whether the agent had actually produced a patch. This hit `462331852` and
  `473582282`, both of which had already produced correct, oracle-confirmed
  fixes that would otherwise have been lost with no ledger trace. Fixed and
  validated with a real run (`475661865`) before merging.

None of this has yet been done systematically across every retry in the dataset
(it's manual transcript reading so far, not an automated classifier -- a
deliberate choice to avoid new pipeline tooling this late in the project), so the
efficiency numbers above should be read as **not yet corrected for
refusal/infra-driven retries** -- the true "genuine reasoning" efficiency gap
could be smaller, larger, or about the same once that's accounted for. Revisit
this section once the full run is done (2 control bugs remain).

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
