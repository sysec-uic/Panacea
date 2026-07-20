# Submission wall cleared — first local-model verified solve (Jul 17–20, 2026)

Follow-on to the [Jul 15 capability-wall writeup](2026-07-15-treatment-run-model-capability-wall.md).
Same shape as before: `learn_loop.py` treatment pass over mruby ARVO bugs, repair agent =
OSS-CRS crs-claude-code driving a local model served over an SSH tunnel, run under
`systemd-inhibit --what=idle:sleep`, `OSS_CRS_CHECK_PATCH=1`, `OSS_CRS_RUN_TIMEOUT=7200`,
`LEARN_MAX_ATTEMPTS=3`.

This span moved the bottleneck one more layer down and then broke through it. The Jul 15
run said the model couldn't do the task. A stronger local model (a GLM-class code model in
place of the earlier ~30B) showed it *could* — it repeatedly produced the correct fix — but
the pipeline kept dropping that fix on the last mile. This writeup covers finding that
last-mile wall, the fixes for it, and the first **`verified_correct`** a local model has
ever earned in this pipeline.

## What the stronger model changed

With the upgraded model the earlier failure modes (endless read-only recon, never
committing to an edit, stalling in plan mode) were gone. On the bigint pool-escape bug it
went crash → correct root cause → a clean, upstream-shaped patch, and it did this
repeatedly and quickly. The Jul 15 "the model can't drive the loop" wall was cleared.

But across several attempts it still produced **zero accepted submissions**, despite having
a correct, validated fix in hand each time. Every loss was on the *mechanics of submitting*,
not the reasoning:

- **Overrunning the cap mid-validation.** On one attempt the in-turn self-check
  (`check-patch`) returned PASS — crash gone, `rake test` passing — and the agent then spent
  its remaining budget on an extra hand-computed correctness check that the task guidance
  asks for. The 2h wall-clock cap fired in the gap between "validated" and "written to the
  submit directory," and a genuinely-correct fix was recorded as a no-patch timeout.
- **Drowning in patch/diff plumbing.** On other attempts the agent never ran `check-patch`
  at all. It wandered out of its working git tree, re-initialized the wrong directory,
  downloaded a second copy of the source, and argued with itself about diff path-prefixes —
  burning the better part of an hour on git mechanics while the actual fix sat finished on
  disk.

Diagnosis: **the model is no longer the wall — the submission path is.** A turnstile we
built (the enforcement gate) sat downstream of a runner that kept tripping on its own laces
before reaching it.

## The fixes

Three changes, each targeting one facet of the last-mile problem, all on
`fix/verify-pipeline-e2e`.

1. **Auto-submit a validated fix.** The `check-patch` responder now saves the exact diff
   that PASSed; when a run ends with a PASS on record but nothing written to the submit
   directory, the harness promotes that saved diff as the submission. The promoted patch
   still flows through the authoritative fresh-container verify (rebuild, re-run the crash
   input, run the test suite) and the post-hoc differential oracle — this rescues only the
   *submission step*, never the correctness judgement. This directly addresses the
   overran-the-cap loss.

2. **Make `check-patch` the one build/validate/submit path, cooperatively.** The task
   guidance injected into the agent was reworked so a PASS is the explicit finish line
   ("when it prints PASS you are done — the validated patch is recorded and submitted for
   you"), steering the agent away from hand-writing diffs or the manual build path. An
   important correction happened here: a first version of this guidance told the agent to
   edit in the read-only reference tree, which *contradicted* the harness's own authoritative
   instructions (which prescribe downloading a clean copy and editing there). Fighting those
   instructions made things worse in a live run; the shipped version instead *cooperates*
   with that workflow and simply names the exact editable git repo and where to run
   `check-patch` from.

3. **Per-bug crash isolation in the loop.** The campaign loop had no exception handling
   around a single bug's work, so any infrastructure hiccup (a failed build, a docker error)
   propagated up and killed the *entire* remaining batch, leaving the in-progress bug with
   no ledger entry. The loop now records a crashed bug as an `error` verdict and continues to
   the next one, so one hard bug can no longer silently end an overnight run. (This one was
   not exercised by a real crash this span — no bug crashed — but it closes a latent
   batch-ending gap seen in earlier runs.)

An operational note worth recording: a long-running campaign process holds the code it
imported at startup. An initial relaunch was accidentally left running from before the
prompt/isolation fixes landed and quietly shipped the *old* guidance; the fixes only took
effect once the campaign was restarted from the updated code. Verify the injected guidance
in a live run rather than assuming a code change is active.

## The result

A fresh campaign launched from the fixed code solved bug **439291659** (mruby, an MSan
bigint issue) on the **first attempt**:

- Ledger: `verified_correct`, `n_attempts=1`, `oracle_label=oracle_confirmed`,
  `n_divergences=0`.
- Run record: `check_passed=true`, `timed_out=true`, `auto_submitted=true`, `patches=1`.

Read together, those are the whole story in one line: the agent got its `check-patch` PASS,
the 2h cap then fired *before* it wrote a submission — the exact loss mode from before — and
**auto-submit rescued it**. The promoted patch was independently re-verified in a fresh
vulnerable container (crash gone, test suite green) and the differential oracle confirmed it
matches the canonical upstream fix with zero divergences across all probes plus the crash
input. It is a real source fix (a pool-memory-ownership guard in the bigint allocation
path), not a harness dodge or a symptom silence.

Two secondary confirmations:

- **The cooperative guidance worked as intended.** The agent briefly repeated the old
  mistake (re-initializing the wrong directory), then *read the injected guidance, corrected
  itself, and got back on track in about ten minutes* — versus roughly fifty minutes lost to
  the same confusion with no guidance before.
- **The learning loop closed.** Because this was a treatment solve, a lesson was extracted
  from it and added to the playbook — the first time the system has learned from a
  local-model fix.

## Where the walls stand now

- **Jul 15:** the model can't drive the loop. → cleared by the stronger model.
- **Jul 17:** the model can, but the harness drops the validated fix on the last mile. →
  cleared by the three fixes above.
- **Jul 20:** a local model solves a bug first-try, oracle-confirmed, and the pipeline
  captures it.

The infrastructure is now demonstrably ready end-to-end on a local model for at least this
bug class. Open threads for the next run:

- This solve landed on a *new* bug, not a direct replay of the specific bug that motivated
  the fixes (that one is already recorded, so the loop skips it on resume). To re-prove the
  exact overran-the-cap scenario, clear that bug's ledger entry or target it explicitly.
- The crash-isolation fix is shipped but still unexercised by a real crash — worth watching
  on a longer run that hits a genuinely broken bug image.
- Only a handful of bugs have run under the fixed pipeline. The value now is scaling the same
  method across the full bug set to see whether the treatment/learning signal holds.

## Operational notes

- **Serving setup:** the repair model runs on a multi-GPU server reached over an SSH tunnel
  (referred to here generically as the LLM server rather than by address), with multiple
  serving slots so the agent's interleaved background calls no longer evict the main
  conversation's cache — the long-context turn-latency collapse from earlier single-slot runs
  is much reduced at the start of a run.
- **The results tree is git-ignored** (ledger + per-bug outputs are local only), and the
  playbook is committed only as a finished snapshot, not mid-experiment — so this run's
  ledger verdict and the freshly-learned lesson live locally for now. Copy anything worth
  keeping into a tracked location before the next cleanup.
- **Always relaunch from current code.** See the startup-import note above: a restart is the
  only way a code fix reaches a running campaign.
