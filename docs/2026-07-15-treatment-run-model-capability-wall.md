# Treatment run on the local model: the model-capability wall (Jul 15, 2026)

Follow-on to the [check-patch gate build + validation notes](2026-07-15-check-patch-gate-live-validation.md).
Same setup: `learn_loop.py` treatment pass over mruby ARVO bugs, repair agent =
OSS-CRS crs-claude-code driving a ~30B local code model (llama.cpp on a remote GPU
server over an SSH tunnel), run under `systemd-inhibit --what=idle:sleep`,
`OSS_CRS_CHECK_PATCH=1`, `OSS_CRS_RUN_TIMEOUT=7200`, `LEARN_MAX_ATTEMPTS=3`.

This session shipped behavioral countermeasures for the Jul 14–15 stalls, then ran a
full 3-attempt cycle on the first bug. **Every piece of infrastructure worked; the model
could not drive any of it.** The run was stopped after bug 1 because the result was
already conclusive.

## What shipped this session (countermeasures)

All three target the Jul 14–15 failure modes (agent stalls in read-only recon / plan
mode, never submits):

1. **Plan mode disabled at the tool level.** The agent invocation now passes
   `--disallowedTools EnterPlanMode`, and the system prompt says plan mode is off. A weak
   model can no longer trap itself in a read-only plan-mode stall.
2. **check-patch reframed as the primary loop.** The injected instruction changed from
   "before you submit, run check-patch" (a safety net) to "your primary loop is edit →
   check-patch → iterate; make your best edit as soon as you have a hypothesis, let FAIL
   output drive the next step, budget for several cycles."
3. **HEURISTICS.md made discoverable.** The task guidance file (the channel that tells the
   agent the check-patch tool exists) was previously referenced nowhere; the agent only
   found it by accident. The CLAUDE.md template now tells the agent to read it first.

Also corrected a stale test assertion in the agent invocation test and committed the
pending prompt-template edits (investigation discipline, trace-driven reading, correctness
oracle checks) first so the countermeasures layered on a known state.

## What the run proved: infrastructure is all green

Across three attempts on the first bug, every mechanism fired correctly:

- **Plan-mode block held.** The agent actually reached for plan mode once (called
  `ExitPlanMode`) and was refused because `EnterPlanMode` no longer exists, confirming the
  disabling works rather than just discourages.
- **HEURISTICS.md was read early** in every attempt (the discoverability fix landed).
- **The wall-clock cap fired** at the 2h mark and correctly recorded the attempt, then
  advanced the repair loop.
- **The check-patch enforcement gate fired for the first time ever.** Attempt 2 submitted
  a patch without self-checking; the gate rejected it as `unchecked` *without paying for a
  verify build*, and fed a targeted rebuke ("you submitted without validating; run
  check-patch until PASS, don't submit until it passes") into attempt 3.
- **The ledger recorded its first real local-model verdict:** bug 1 → `unchecked`,
  n_attempts 3.

## The decisive finding: two capability walls prompt work can't move

With the behavioral blockers removed, the run exposed two deeper limits, both confirmed
repeatedly across all three attempts (and the pattern began repeating on bug 2 before the
run was stopped):

1. **The agent fixes blind.** It is given the raw proof-of-crash input but *not* the
   sanitizer crash trace, and it cannot rebuild the fuzzer inside its own container to
   reproduce the crash (every build/run-pov attempt failed with "does not exist" / "build
   environment not set up"). With no trace to follow, it guessed the vulnerability class
   from the input structure and edited the **fuzz harness** repeatedly, never the actual
   faulting subsystem. The harness is exactly what the `patch_touches_harness` guard
   exists to reject.

2. **The agent will not operationalize check-patch.** Even after a rejection whose entire
   purpose was to force check-patch usage, with the exact command spelled out in its
   instructions, attempt 3 tried to build manually (`cmake`, `build.sh`), failed,
   concluded "I have enough information," and submitted blind. It never connected "I need
   to build and validate" to "run the check-patch command in front of me." Zero check-patch
   invocations across all three attempts.

Interpretation: the model operates in "understand everything, submit once" mode and cannot
adopt the edit → check → iterate loop the pipeline now rewards. The binding constraint is
model capability, not the harness, prompts, or gate.

## Turn-latency data (measured, corrected)

Sampled the agent conversation externally (no timestamps in the stream log; derived
turn latency from wall-clock vs. assistant-message count). Result: latency is **fast early
but still decays at long context**, roughly ~1 min/turn under ~110 KB of context,
degrading to ~6 min/turn past ~300 KB. That is close to the earlier runs' worst case; the
serving tuning helps the early phase but does not eliminate the long-context slowdown. (An
earlier "stays flat" reading was a sampling artifact, a turn completing just after a poll
tick, and was corrected.)

## Next levers (for a stronger model)

The two capability walls suggest concrete pipeline changes, but their importance depends on
the model. With a more capable model tomorrow:

1. **Inject the sanitizer crash trace at startup.** This is the highest-value change and
   worth doing regardless of model: the harness already has the trace (the PoC was captured
   from a known crash), yet the agent is made to reproduce it blind and fails. Handing it
   the trace directs reading to the real faulting frame instead of the harness, and makes
   the retry feedback ("use the crash trace") actionable. A stronger model may still
   benefit enormously from this even if it *can* reproduce, because it skips the wasted
   reproduction phase.
2. **Make check-patch the unavoidable build path**, not an optional documented tool. A
   capable model may reach for it unprompted; if not, consider surfacing it as the only
   sanctioned way to build/validate so "I'll build manually" is not an option.
3. **Watch whether a capable model even needs the timeout feedback loop.** The
   `timed_out` → "commit to a fix" feedback did change behavior (attempt 2 became
   action-oriented and actually submitted), but a stronger model may converge before the
   cap and never need it.

If the stronger model closes bugs where this one could not, that confirms capability was
the wall and the infrastructure is ready. If it *also* fixes blind or skips validation,
then levers 1 and 2 become the priority regardless of model tier.

## State at stop

- Bug 1 recorded as `unchecked` (3 attempts, no fix). Run stopped before bugs 2–3 to avoid
  ~4–6h each re-confirming the same result.
- All campaign processes and containers torn down cleanly.
- Per-attempt agent logs, the submitted harness patch, turn-latency CSVs, and the ledger
  verdict archived under the bug's results dir.
- Caveat: the results tree (archives + ledger) is gitignored, so these artifacts are not
  versioned; copy anything worth keeping into a tracked location before the next cleanup.
