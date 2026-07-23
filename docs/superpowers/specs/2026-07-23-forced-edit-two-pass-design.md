# Autonomous forced-edit two-pass — design

**Date:** 2026-07-23
**Status:** approved design, pre-implementation
**Component:** `arvo-eval` local-model repair loop (`arvo_oss_crs.py`, `learn_loop.py`)
**Related:** `docs/2026-07-23-control-baseline-and-injection-delivery-bug.md`,
`docs/2026-07-21-latency-wall-cleared-orientation-and-cache-reuse.md`

## Motivation

On the local-model backend, delivery of the playbook and crash orientation is now
confirmed working (identity-pinned injection, commit `2727f90`). Yet the repair agent
still loses hard bugs to a single, specific failure: **it understands the fix but never
writes it.**

Observed live on bug `439645304` (mruby set/khash rebuild use-after-free): delivery
verified (agent read `HEURISTICS.md`, 5 positive references, 0 "no HEURISTICS"), the
agent ran ~45 read-only investigation commands, reached the **correct** root cause, and
stated the exact fix in prose twice ("add `mrb_field_write_barrier_value` after the set
insertion paths") — then made **zero edits, zero `check-patch` calls**, and hit the 2h
wall-clock cap. Verdict: `timed_out`.

The existing countermeasure is prose: a paragraph in the injected `check-patch`
instruction telling the agent to "make your best edit early … an early wrong edit that
check-patch refutes teaches you more than an hour of reading." The agent reads it and
ignores it. **More or louder prose is not the fix.** The failure is commit-to-edit, and
it needs a mechanical forcing function.

## Goals

- Convert the "understands the fix, writes nothing" failure into a real edit + validated
  patch, **without an operator in the loop** — the shipped pipeline is the repair agent
  (Claude) + the local model + this automated forcing function, running unattended.
- **The agent always writes every edit.** The harness only controls timing and prompting.
  We are still measuring "can this model fix the bug," so the harness must never author a
  patch itself.
- Deterministic and robust enough to run overnight without supervision: no fragile
  mid-flight surgery, no dependence on transcript-format stability in a hot path.

## Non-goals

- No harness-authored patches / prose-to-diff extraction-and-apply (explicitly rejected:
  off-thesis — it would count the harness's edit as the agent's solve).
- No change to the differential oracle, the check-patch self-check service, or auto-submit.
- No change to the injection-delivery mechanism (that is already fixed).

## Design overview

Add a **two-pass** structure inside `run_oss_crs`, gated by a new env flag
`OSS_CRS_FORCE_EDIT=1` (same pattern as `OSS_CRS_ORIENT` / `OSS_CRS_CHECK_PATCH`). When
disabled, behavior is byte-for-byte identical to today.

```
Phase 1 (recon):
    inject normal HEURISTICS (+ orientation, + check-patch instruction) as today
    run the agent under the full OSS_CRS_RUN_TIMEOUT hard cap
    at RECON_TIMEOUT (~30 min) elapsed, check the agent's edit count ONCE:
        edits > 0  -> agent is productively working; do nothing, let Phase 1 run to
                      its natural end / the full hard cap (no interruption, no loss)
        edits == 0 -> terminate Phase 1 and proceed to Phase 2

Phase 2 (forced edit):   [only reached when Phase 1 ended with 0 edits]
    rewrite the injected file to a FORCED-EDIT DIRECTIVE (see below)
    run the agent again under the REMAINING budget (hard cap minus Phase-1 elapsed)
    collect patches from Phase 2

Patch collection / grading: unchanged, operates on whichever phase produced a patch.
```

The one-shot check at `RECON_TIMEOUT` is what reconciles "push the staller" with "don't
interrupt a worker": a single timer fires, reads the edit count once (not a hot-path
poll), and either cuts over or steps aside.

## Why this shape

- **Reuses machinery we already own.** Phase 2 is just another `oss-crs run` against the
  same already-built `target-source`, and it changes what the agent reads through the
  **existing injection channel** (`inject_heuristics` writes into `target-source`). No new
  prompt-assembly hooks in OSS-CRS, no new services.
  - **Correction (found in final review, fixed in implementation):** the `check_server`
    self-check thread is **not** reusable as-is across the two passes. It latches a single
    SHARED_DIR at construction and serves it forever, but Phase 2 is a fresh `oss-crs run`
    with a **new** SHARED_DIR — so a thread started once for Phase 1 would leave Phase 2's
    `check-patch` client unwritten and its responder bound to Phase-1's torn-down dir,
    silently crippling the forced pass in the `OSS_CRS_CHECK_PATCH=1` config. The service
    must be **stopped and restarted between phases** (fresh `svc_start` so `find_shared_dir`
    fences out the dead Phase-1 dir, fresh PASS-marker/autosubmit-diff). This is done via an
    `on_forced_handoff` hook (`_recycle_for_phase2`) that stops the old thread, prunes
    docker networks — the second per-bug `oss-crs run` doubles network-pool pressure — then
    starts a fresh thread; the warm `-vul` instance metadata is captured once and reused.
    Auto-submit needs no change (it reads the current run's marker/diff).
- **No lost work, no fragile surgery.** Phase 2 is only ever entered when Phase 1 made
  **0 edits**, so a fresh Phase-2 worktree discards nothing. This sidesteps all
  container-persistence / session-resume fragility — we never have to keep a container
  alive across passes or splice into a running Claude session.
- **Fresh narrow session, deliberately.** Phase 2 is a *new* agent session, not a resume.
  The bet is that the model anchored on "keep reading" during a long recon session behaves
  very differently when handed a single unambiguous task from a clean slate. Feeding its
  own prior diagnosis back removes the excuse to re-investigate.

## Detailed design

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `OSS_CRS_FORCE_EDIT` | unset (off) | Master switch for the two-pass behavior. |
| `OSS_CRS_RECON_TIMEOUT` | `1800` (30 min) | Phase-1 elapsed time at which the 0-edit check fires. |
| `OSS_CRS_RUN_TIMEOUT` | `7200` (2 h) | Unchanged hard per-run wall-clock cap; bounds Phase 1 + Phase 2 together. |

Phase 2's cap is `max(RUN_TIMEOUT − phase1_elapsed, floor)` where `floor` is a small
minimum (e.g. 900 s) so a forced pass always gets a usable budget. With the defaults a
staller yields ~30 min recon + ~90 min forced edit, both inside the 2 h hard cap.

### Edit detection (`agent_edit_count`)

A pure function that counts `Edit` / `Write` / `MultiEdit` `tool_use` events in the
agent's persisted session transcript (the JSONL OSS-CRS writes under the run's
`LOG_DIR`). Primary signal. This is the same parse we already do ad hoc when inspecting
runs; formalize it as a tested helper. `0` ⇒ trigger Phase 2.

- Robustness: the check runs at exactly one moment (the `RECON_TIMEOUT` mark) and once
  more at Phase-1 end, never per-turn, so transcript-format drift can't destabilize the
  hot path.
- Fallback signal (defense in depth, OR-combined): a non-empty worktree `git diff` or a
  collected patch also counts as "edited," so a mis-parsed transcript can only ever make
  us *skip* a needless Phase 2, never clobber real work.

### Root-cause extraction (`extract_root_cause`)

A pure function that returns the agent's own final substantial assistant text block from
the Phase-1 transcript (the last `text` content of meaningful length). This is the
diagnosis the agent already produced. If none is found, Phase 2 proceeds with an empty
prior-analysis section — the directive still stands on the crash trace alone.

The harness does **not** interpret, correct, or convert this text into a patch. It is
passed back verbatim as the agent's own words.

### Forced-edit directive (`build_forced_edit_directive`)

Phase 2 replaces the injected `HEURISTICS.md` content with a generated directive
containing, in order:

1. **The single mandate**, first and unmissable: *"Do not investigate further. Make the
   one edit your analysis already points to and run `check-patch`. That is your only
   task."*
2. **The crash orientation** (already available — same content `inject_orientation`
   produces).
3. **Your prior analysis** — the `extract_root_cause` text, framed as "You already
   concluded:".
4. **The exact recipe** — repo path inside `clean-src`, the `download-source … && cd … &&
   bash "$OSS_CRS_SHARED_DIR/check-patch"` one-liner (reuse `check_patch_instruction`),
   and "when check-patch prints PASS you are done."

Explicitly omitted: the full playbook and the "read the codebase" affordances. Phase 2 is
about narrowing, not informing.

### `run_oss_crs` control flow

Wrap the current single "inject → run agent → collect patches" body in a phase loop
gated by `OSS_CRS_FORCE_EDIT`:

- Phase 1 runs with the existing `_run_agent_with_timeout` under the hard cap, plus a
  one-shot timer (thread or scheduled check) at `RECON_TIMEOUT` that reads
  `agent_edit_count`; if `0`, it signals Phase-1 termination.
- If Phase 1 ended via that 0-edit signal, build the directive, re-inject, and run Phase 2
  under the remaining budget.
- Patch collection / grading run once, afterward, on the run dir that has the patch —
  unchanged logic.

When `OSS_CRS_FORCE_EDIT` is unset, none of the above executes and the function behaves
exactly as it does today.

### Bounded failure

Exactly **one** forced pass. If Phase 2 also ends with 0 edits, record the current
verdict (`timed_out` / `no_changes`) as today. No escalation loop, no third pass.

## Observability / ledger

Add fields to the per-bug ledger record so the two-pass behavior is auditable without
reading transcripts:

- `forced_edit_triggered`: bool — did Phase 1 end with 0 edits and hand off to Phase 2.
- `edit_phase`: `"recon"` | `"forced"` | `null` — which phase produced the accepted patch
  (or none).
- `phase1_edits`, `phase2_edits`: ints.

These make it a one-line query to answer "how often does the forcing function fire, and
how often does it convert a would-be `timed_out` into a solve."

## Experimental scope (launch-time decision, not baked in)

The mechanism is **arm-agnostic**: the directive is built from the crash trace and the
agent's own words, independent of any playbook content, so it is a fair general pipeline
improvement rather than part of the treatment. To avoid confounding the playbook effect,
it should run on **both** control and treatment (as `OSS_CRS_ORIENT` already does).

**Implication:** turning it on obsoletes the current control 6/10 baseline — a clean
comparison needs a control re-run with the flag on. Because the behavior is flag-gated
(`OSS_CRS_FORCE_EDIT` unset ⇒ no change), we can land the code dark and choose when to
enable it and re-baseline.

## Testing

Pure-logic unit tests (no Docker / network), mirroring `tests/test_arvo_oss_crs.py`:

- `agent_edit_count`: 0 on a recon-only transcript; correct count with edits; robust to
  malformed lines; fallback (`git diff` / collected patch) OR-combines correctly.
- `extract_root_cause`: returns the last substantial assistant block; empty-safe.
- `build_forced_edit_directive`: mandate appears first; includes crash trace, prior
  analysis, and the repo-scoped check-patch recipe; excludes the full playbook.
- Phase-decision: `edits == 0 ⇒ force`, `edits > 0 ⇒ no force`; Phase-2 budget =
  `max(hard_cap − phase1_elapsed, floor)`.
- Flag-off: `OSS_CRS_FORCE_EDIT` unset ⇒ single-pass path, identical call sequence to
  today (guard against accidental behavior change).

## Risks / open questions

- **Does the model actually edit when narrowed?** The core hypothesis. Unproven until run.
  Ideal first test: head-to-head on the two set-family bugs that bracket this window
  (`439645304`, `440058794`) — same bugs, forced-edit on — and see if 0 edits becomes a
  validated patch.
- **Budget split (30/90).** Recommended, tunable via env. If real recon on hard bugs
  needs more than 30 min before a diagnosis exists, `extract_root_cause` will feed Phase 2
  a weak analysis; the cap may need lifting after observation.
- **Transcript location/format** for `agent_edit_count` / `extract_root_cause` must be
  pinned to how OSS-CRS persists the session under `LOG_DIR`; verify during implementation
  and cover with a fixture.
