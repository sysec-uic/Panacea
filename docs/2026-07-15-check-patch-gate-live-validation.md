# check-patch enforcement gate — build + live-validation attempt (Jul 14–15, 2026)

Follow-on to the [Jul 13 postmortem](2026-07-13-learn-loop-local-model-campaign.md).
Same setup: `learn_loop.py --limit 3` (treatment pass) on mruby ARVO bugs, agent =
OSS-CRS crs-claude-code driving Qwen3-Coder-30B (llama.cpp on a remote GPU server
over an SSH tunnel). This session built the in-turn **check-patch self-check** and its
**enforcement gate**, then tried twice to validate it live on bug 439237851.

## What was built (all committed on `fix/verify-pipeline-e2e`)

The agent's container has no toolchain and no Docker, so it edits blind and can't
validate its own patch. Fix = a **host-side responder** that gives the agent an
in-turn `check-patch` tool:

- `check_server.py` — background thread per run. Waits for the agent's SHARED_DIR to
  appear, drops a `check-patch` client into it, warms one long-lived `-vul` container,
  and serves check requests by running the **same** deployment-faithful
  build+PoC+`rake test` the outer loop uses (`verify_fix.run_check`) — never the `-fix`
  image. Writes a PASS marker on `verified_correct`.
- `verify_fix.run_check` — container-agnostic check engine extracted from `verify()`,
  so the warm container is reused across checks (incremental rebuild = seconds, not a
  ~40 min cold build).
- **Enforcement** (`OSS_CRS_CHECK_PATCH=1`): `repair_loop.py` rejects a submission the
  agent never self-checked as `unchecked` — *without* paying for a verify build — and
  feeds back "run check-patch until it prints PASS, then submit."
- `OSS_CRS_RUN_TIMEOUT` wall-clock cap + `terminate_crs_run` teardown; race fix so the
  responder latches only a SHARED_DIR created after service start (`newer_than` filter).

208 tests passing. Commits: timeout cap → `run_check` engine → responder core → README
→ check-patch wiring → race fix → enforcement gate.

## What the two live runs proved (infrastructure: all green)

1. **Channel wiring is correct.** Both runs dropped the `check-patch` client into the
   *live* run's SHARED_DIR (`runs/<runid>`), never one of the ~9 stale dirs from prior
   killed campaigns. The `newer_than` race fix holds.
2. **Warm check container comes up** and polls for the duration of the run.
3. **Timeout cap works as designed** — see the suspend note below.

## What blocked validation (the actual finding)

**The gate was never exercised, because the agent never submitted a patch.** Two runs,
same wall: the local model spends its entire budget *reading* and never commits to an
edit.

- **Run 1 (`1784054740qq`)** — invalidated by the **controlling machine suspending**.
  The host idle-slept ~4.15 h of a ~5 h window. `subprocess.run`'s timeout uses `CLOCK_MONOTONIC`,
  which excludes suspend, so the 2 h cap counts *awake* time and correctly had not
  fired — but wall-clock progress was near zero and the agent's in-flight model request
  died on resume. **Diagnosis via `CLOCK_BOOTTIME − CLOCK_MONOTONIC` = 4.15 h.**
  Fix: relaunch under `systemd-inhibit --what=idle:sleep`.
- **Run 2 (`1784085713au`, under inhibitor)** — no suspend (gap steady at 25883 s).
  Agent ran ~75 min awake and:
  - spent the whole time in read-only recon (`grep overflow`, `Read vm.c / numeric.c /
    opcode.h / ops.h`);
  - called **`EnterPlanMode`** at ~50 min and then got **stuck exploring inside plan
    mode** for 26+ min — plan mode forbids edits, so this is a trap for a weak model;
  - produced **zero edits**, zero check-patch invocations, empty `git diff`;
  - turn latency degraded from ~1.5 min → **~6.5 min/turn** as big-file reads ballooned
    context.
  - Killed manually before the 2 h awake cap.

## Interpretation

- The check-patch gate is **built and correct**, but it sits **downstream** of the real
  bottleneck. We built a turnstile; the runner never reached it.
- The binding constraint is unchanged from Jul 13: **Qwen3-Coder-30B is too slow and too
  indecisive at long context** to close these bugs. It reads exhaustively, won't commit
  to a first edit, and latency compounds as context grows.
- Deeper mismatch: check-patch only pays off if the agent adopts an **edit → check →
  iterate** loop. This model is in "understand everything, submit once" mode — the
  opposite — so neither the fast feedback channel nor the gate can help it.

## Next levers (cheapest first)

1. **Disable plan mode** for the agent. It's actively counterproductive here — the agent
   entered it and stalled, and it blocks edits. One change in the agent invocation.
2. **Front-load "patch early" into the task prompt.** Tell the agent up front: you have a
   `check-patch` tool, so make your best edit quickly and validate it rather than reading
   exhaustively first. Turn check-patch from a safety net into the primary loop.
3. **Let a run ride to the 2 h cap** to test whether the `timed_out` feedback ("stop
   exploring, write the patch now") on attempt 2 breaks the paralysis. If attempt 2 also
   just explores, that's decisive: the model can't do this and no gate/prompt work saves
   it.
4. **Strategic:** two runs point at model capability as the wall. The planned serving
   upgrade to a stronger model (e.g. a GLM-class model on a larger multi-GPU server)
   likely gates real progress more than any further pipeline work.

## Operational notes

- **Always run campaigns under `systemd-inhibit --what=idle:sleep`**; if the controlling
  machine is a laptop, keep the lid open (lid-close can suspend regardless of
  inhibitors). Idle-suspend silently freezes the whole run and breaks the model
  connection on resume.
- To detect a past suspend: `CLOCK_BOOTTIME − CLOCK_MONOTONIC` in Python, or
  `journalctl -k | grep 'PM: suspend'`.
- Cleanup on kill: `pkill -f learn_loop.py`, then `docker rm -f` the `crs_compose_*`
  containers and `arvo-<id>-check`. Killing the agent mid-run can make learn_loop spawn
  the next attempt before it dies — re-check for an orphaned `oss-crs run` tree.
