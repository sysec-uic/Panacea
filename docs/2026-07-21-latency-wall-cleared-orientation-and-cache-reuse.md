# Latency wall cleared — orientation + cache-reuse solve a previously-unsolvable bug (Jul 21, 2026)

Follow-on to the [Jul 20 submission-wall writeup](2026-07-20-submission-wall-cleared-first-local-model-solve.md).
Same setup: `learn_loop.py` treatment pass over mruby ARVO bugs, repair agent =
OSS-CRS crs-claude-code driving a GLM-5.2 local model over an SSH tunnel, run under
`systemd-inhibit`, `OSS_CRS_CHECK_PATCH=1`, `OSS_CRS_RUN_TIMEOUT=7200`,
`LEARN_MAX_ATTEMPTS=1`. This session moved the bottleneck one more layer down —
from *the model can't act* to *the serving latency won't let it act in time* — and
cleared it. Bug **439494108** (mruby bigint pool-escape stack-use-after-return),
which had `no_changes`-timed-out under the old serving, came back **`verified_correct`,
`oracle_confirmed`, 0 divergences, first attempt.**

## The starting problem: the agent localizes but never acts in time

Two runs on 439494108 established the wall. The repair agent (with the crash trace
available) localized correctly and fast — but spent its whole 2h cap reading, never
committing to an edit:

- Both runs reached the *right* subsystem (`mrb_bint_reduce`/`pool_storage` in
  `bigint.c`) by ~turn 10, then over-read. Turn latency decayed **50s → 354s/turn**
  as context grew, so ~60 turns consumed the entire 2h cap.
- Result: 0 edits, 0 check-patch, `no_changes`. The behavioral wall ("understand
  everything, submit once") was made *fatal* by serving latency eating the clock.

## Three findings, each correcting the last

### 1. Orientation must be delivered *inline*, not behind a pointer

We built a "crash orientation" pre-flight (`orientation.py`): parse the sanitizer
report already stored in `arvo.crash_output` into a compact briefing (crash class,
fault site, call chain, root-cause frame) and inject it. First delivery wrote a
separate `ORIENTATION.md` and prepended a one-line *pointer* to `HEURISTICS.md`.

A live A/B showed the agent **read HEURISTICS.md but never followed the pointer** to
open the second file — the same "won't act on an instruction to go do something"
failure that dooms check-patch. Fix: **inline the full briefing at the top of
HEURISTICS.md** (the file the agent provably reads first). Re-run confirmed the agent
then localized straight from the trace. But orientation *alone* still timed out — it
fixed *where to look*, not *acting in time*.

### 2. The latency decay was NOT background-call eviction — it was context re-prefill

The prior working theory (and a memory) blamed Claude Code's interleaved background
"haiku" calls evicting the single-slot KV cache. **Measured and falsified:** the
LiteLLM access log showed **34 `POST /v1/messages` vs ~60 agent turns** — *fewer*
model calls than turns. Headless `claude -p` makes essentially no background calls;
there was nothing to evict. The real cause: `llama-server` ran with **zero flags** (no
`--cache-reuse`, no `-fa`, no `--parallel`), so every turn re-prefilled the entire
growing prompt from scratch. As context passed ~100k tokens, prefill time climbed —
that is the 50s→354s decay.

### 3. The fix is box-side cache-reuse (owner's action; the box is read-only to the agent)

The serving box was restarted as:

```
llama-server -m <GLM-5.2 UD-IQ2_M> --cache-reuse 256 -fa on -t -1 -c 131072
```

`--cache-reuse 256` is the lever: it keeps the common prompt prefix cached across the
agent's turns instead of reprocessing it every time. (Note: the box frees VRAM when
idle — the model unloads to ~1 MiB between runs and reloads to ~60 GB on the first
request; a 503 on port 8080 means it is still loading, wait for 200.)

## The result

A fresh run on 439494108, identical except for the fast server:

| | Slow server (v3) | Fast server (v4) |
|---|---|---|
| Turn latency | 50s → 354s/turn | **43s → 140s/turn** (held) |
| First edit | never (0 edits) | **turn 66, inside the cap** |
| check-patch | never | **ran, PASS** (crash gone + `rake test`) |
| Verdict | `no_changes` (timeout) | **`verified_correct`, oracle_confirmed, 0 div** |

The full chain finally executed end-to-end on a local model: inline orientation →
flat latency → targeted edits → check-patch PASS → submit → fresh-container verify →
differential oracle. The fix is the correct root cause, not a symptom silence: in
`bint_new`'s heap branch it detects pool-backed (stack-scoped) memory
(`MPZ_HAS_POOL && is_pool_memory`) and **copies the limbs into fresh heap storage**
(`mrb_malloc` + `memcpy` + `mpz_clear`) instead of moving a pointer that would dangle
after the caller returns.

## The learning loop demonstrably contributed

This was the *treatment* pass, so the playbook was injected as `HEURISTICS.md`. The
submitted fix mirrors playbook heuristic **`h-439291659`** — "detect pool memory
(`MPZ_HAS_POOL && is_pool_memory`); if so `mrb_malloc` + `memcpy` limbs into a fresh
heap block, then `mpz_clear`" — which was extracted from the **Jul 20 solve of a
different bug in the same family** (439291659). A lesson learned on one bug guided the
correct fix of a later same-class bug: the core research thesis showing signal, on a
local model, end-to-end. The playbook advanced to version 2, now also carrying a lesson
from 439494108.

## Where the walls stand now

- **Jul 15:** the model can't drive the loop. → cleared by a stronger model (GLM-5.2).
- **Jul 17–20:** the model can, but the harness drops the validated fix on submission.
  → cleared by auto-submit + cooperative check-patch guidance.
- **Jul 21 (this run):** the model acts correctly, but serving latency eats the cap
  before it can. → cleared by inline orientation + box-side `--cache-reuse`.

Infrastructure is now demonstrably ready end-to-end on the local model for this bug
class, *and* the treatment/learning signal is visible. Open threads:

- **Scale.** Only a handful of bugs have run under the fully-fixed, fast pipeline. The
  value now is running the fuller mruby set to see whether the treatment/learning
  signal holds across many bugs — the next step.
- **Two stale failures** (439237851 `unchecked`, 439279102 `timed_out`) were recorded
  under the *old* broken serving with no orientation; they deserve a fair re-run on the
  fixed pipeline.
- **Phase 2** (learned recon *scripts*, the deferred half of the orientation spec)
  remains available if per-class recon still costs turns at scale.

## Operational notes

- **Launch runs truly detached** (`setsid nohup env … systemd-inhibit … python3
  learn_loop.py >log 2>&1 </dev/null &`), not via a session-bound background shell — a
  session teardown SIGKILLs a session-child run mid-flight (learned the hard way this
  session; a v3 run died at turn 23 on disconnect). Durable record = the ledger + the
  logfile.
- The serving box is **read-only to the agent** (shared machine); `--cache-reuse` and
  restarts are the owner's action. Diagnostic SSH reads only.
- Results tree (ledger + per-bug outputs) is git-ignored; the playbook is committed only
  as a finished snapshot. Copy anything worth keeping before the next cleanup.
