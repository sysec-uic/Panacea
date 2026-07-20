# Crash Orientation & Learned Recon Scripts — Design

**Date:** 2026-07-20
**Status:** Approved design, pending implementation plan
**Scope:** The repair-agent side of `arvo-eval` (the bug-fixing agent, not the lesson
extractor). Two staged phases; Phase 1 is the certain win, Phase 2 is the research
extension.

## 1. Problem & Goal

The repair agent (OSS-CRS / crs-claude-code driving the local GLM-5.2 model) burns its
entire per-attempt wall-clock cap (`OSS_CRS_RUN_TIMEOUT`, currently 7200s) on
**deterministic recon** — reproducing the crash and localizing the faulting subsystem —
and never reaches the `edit → check-patch → iterate` loop.

Observed live on bug `439494108` (2026-07-20, GLM-5.2, single-slot serving):

- **Attempt 1:** ~2h, 66 turns, **1 edit** to the correct file (`bigint.c`), **0
  check-patch runs** → timed out with no validated patch.
- **Attempt 2:** ~1h44m, ~54 turns, **0 edits, 0 check-patch** — pure recon, timed out.

The agent localizes correctly (right file, right functions) — capability is not the
wall. The failure is **throughput/pacing**: it re-derives, via slow ~4 min/turn LLM
grepping, information the sanitizer report already states outright. The injected guidance
*already* says "do not read the whole codebase first… make your best edit… budget for
several edit → check cycles" (`arvo_oss_crs.py:check_patch_instruction`) and is ignored.
Prose is spent; orientation must become **structural**, not advisory.

**Goal:** hand the agent its orientation deterministically so recon collapses from ~40+
minutes of LLM turns to seconds, leaving the budget for the iterate loop.

- **Phase 1 (fixed):** parse the sanitizer trace already stored in the DB into an
  `ORIENTATION.md` and inject it into the agent's workspace.
- **Phase 2 (learned):** after a solved bug, the extraction agent emits a reusable
  crash-class recon *script* into the playbook; later same-class bugs get it injected.

**Non-goals (YAGNI):**
- **Auto-check-on-edit forcing function** — attacks the other end (check-often) via a
  host-side repo watch. Complementary but a separate mechanism; its own follow-up spec.
- Serving-latency fixes (LiteLLM aux-split / llama-server `--parallel`). Orientation cuts
  the *number* of recon turns; serving cuts *cost per* turn. Independent, shipped
  separately. See memory `glm52-serving-single-slot-cache-eviction`.
- No change to the correctness authorities (check-patch, differential oracle).

## 2. Core Decisions (settled during brainstorming)

| Axis | Decision |
|------|----------|
| What we offload | Deterministic recon: crash class, fault site, call chain, root-cause frame |
| Data source | `arvo.crash_output` column (already populated by `arvo-expanded/populate_crash_output.py`) |
| Phase 1 producer | Pure host-side parser → `ORIENTATION.md`; **no** LLM, **no** agent turns |
| Phase 2 producer | Extraction agent emits a `recon_script` alongside the prose heuristic |
| Phase 2 trust gate | Script must run without error on the solved bug's own source and emit non-empty output before it is injected |
| Delivery | Injected into the agent's `target-source` dir, like `HEURISTICS.md` |
| Experiment cleanliness | Orientation applied to **both** control and treatment (it is harness, not memory) |
| Deployment faithfulness | The sanitizer trace is a signal a real OSS-Fuzz developer receives; no `-fix` leakage |
| Holdout | Learned scripts flow only to strictly-later bugs, same as prose |

## 3. Deployment-faithfulness & holdout (why this is legal)

The system's two research invariants must hold:

- **No leakage.** Phase 1 injects only the sanitizer crash report — exactly what OSS-Fuzz
  hands a real developer. It never touches the canonical `n132/arvo:{id}-fix` image (that
  stays reachable only by the post-hoc oracle the agent can't see). Phase 2 scripts are
  extracted from *earlier* solved bugs only.
- **Faithful signals.** Parsing the trace into `ORIENTATION.md` is mechanical extraction
  from a signal the agent already legitimately has; it adds no oracle knowledge.

## 4. Phase 1 — Fixed orientation

### 4.1 New module `orientation.py` (pure logic, no I/O)

```
parse_crash_output(crash_output: str, crash_type: str, project: str) -> Orientation
render_orientation(o: Orientation) -> str        # -> ORIENTATION.md body
```

`Orientation` (dataclass): `crash_class`, `fault_site` (func, path, line), `call_chain`
(list of app frames in order), `source_frame` (optional func/path/line — the sanitizer's
root-cause pointer), `summary_line`, `raw_trace` (trimmed).

**Parsing rules:**
- Frames match `#\d+ 0x[0-9a-f]+ in <func> <path>:<line>(:<col>)?`.
- **Application frames** = path under `/src/<project>/`, excluding `/src/llvm-project/`,
  libFuzzer (`FuzzerLoop`, `LLVMFuzzerTestOneInput`, `fuzzer::`), and asan/msan runtime.
- **fault_site** = first (topmost) application frame.
- **call_chain** = application frames in order from the fault site down.
- **source_frame** = the frame in the sanitizer's secondary block, detected by any of the
  markers: `located in stack of`, `freed by`, `allocated by`, `previously allocated by`,
  `Uninitialized value was created by`. Optional — absent for some sanitizers.
- **crash_class** / **summary_line** from the `ERROR:` and `SUMMARY:` lines.
- Format-agnostic across **ASan and MSan** (e.g. `440058794` is MSan); the frame regex is
  shared, only the source-block marker set differs.

### 4.2 Rendered `ORIENTATION.md`

```
# Crash orientation (parsed from the sanitizer report — a real developer signal)
Class:       stack-use-after-return (READ 4)
Fault site:  limb_addmul_1    mrbgems/mruby-bigint/core/bigint.c:726
Call chain:  limb_addmul_1 ← mpz_mul_basic ← mpz_mul ← bint_mul ← rat_sub_b
Source frame (where the bad memory came from):
             mrb_bint_reduce  mrbgems/mruby-bigint/core/bigint.c:3673
→ Read these functions first, form a root-cause hypothesis, make your first edit,
  then run check-patch. Do NOT re-derive the trace by grepping the codebase.

<trimmed verbatim sanitizer trace>
```

### 4.3 Wiring & gating

- New thin `inject_orientation(project_dir, sanitizer, bug, project)` in `arvo_oss_crs.py`,
  called next to `inject_heuristics` (`arvo_oss_crs.py:456`, where the `bug` dict with
  `crash_output` is in scope). It writes `ORIENTATION.md` into the `target-source` dir and
  prepends a one-line pointer to the injected `HEURISTICS.md` ("Read ORIENTATION.md first").
- Gated by `OSS_CRS_ORIENT=1`. **Applied to both passes** so it cannot skew the playbook
  A/B (it is not the memory under test).

### 4.4 Error handling / degradation

- `crash_output` missing/empty → no `ORIENTATION.md`; agent falls back to today's behavior;
  log a warning.
- Trace unparseable (no app frames) → still inject `crash_class` + `summary_line` + trimmed
  raw trace, omitting the parsed pointers. Never abort the run for an orientation failure.

## 5. Phase 2 — Learned recon scripts

After a solved, `oracle_confirmed` treatment bug, the extraction step (`extract_heuristic.py`
/ `contrastive_extract.py`) additionally emits a short **recon script** for the crash class
— e.g. grep the functions/macros that mattered, print the relevant structs, check the
class-specific invariant.

- **Storage:** new playbook heuristic fields `recon_script: str | None`,
  `recon_script_validated: bool` (`playbook_store.py`).
- **Trust gate (at extraction time):** run the candidate script against the *solved bug's
  own* source tree; it must exit 0 and produce non-empty output → `validated=true`.
  Otherwise drop the script (keep the prose lesson). Only validated scripts are injected.
- **Injection:** when a heuristic matching a later bug's crash-class/tags is selected, its
  script is written to `playbook-scripts/<id>.sh` in the workspace and referenced from
  `HEURISTICS.md` ("Run `bash playbook-scripts/<id>.sh` to orient on this crash class").
- **Bounded risk:** the script is *advisory orientation only*. check-patch and the
  differential oracle remain the sole correctness authorities, so a wrong recon script can
  cost turns but **can never produce a wrong verdict**. This is the safety property that
  makes learned, LLM-generated scripts acceptable.
- **Holdout:** scripts flow only to strictly-later bugs, identical to prose lessons.

## 6. Measurement / success criteria

Derived from the agent stream (`agent/claude_stdout.log`) via the tool-result-timestamp
parsing already prototyped this session:

- **turns-to-first-edit** ↓ (primary)
- **turns-to-first-check-patch** ↓
- **edit → check cycles per attempt** ↑
- **solve rate** on the recon-bound bugs (`439494108`, `439645304`, `440058794`) ↑

A/B by toggling `OSS_CRS_ORIENT=0/1` on the same bug(s), independent of the playbook A/B.

## 7. Module boundaries & testing

- `orientation.py` — pure functions, no I/O; unit-tested against the **30 real
  `crash_output` strings already in `arvo_new.db`** (assert parsed class / fault site /
  source frame). Fits the repo's existing pure-logic test suite (no Docker/network).
- `arvo_oss_crs.py` — thin wiring only (`inject_orientation`, env gate).
- Phase 2 — `extract_heuristic.py` / `contrastive_extract.py` (emit script),
  `playbook_store.py` (fields + validation), injection path (drop + reference). Layered on
  the Phase 1 baseline; no change to correctness authorities.

## 8. Rollout

1. Ship Phase 1 (pure host-side, fully testable) and validate the parser on all 30 traces.
2. Run an `OSS_CRS_ORIENT` A/B on a recon-bound bug to confirm turns-to-first-edit drops.
3. Add Phase 2 once Phase 1 shows the effect.

All code changes take effect only on campaign **restart** (the startup-import trap —
memory `glm52-...`), so this is a next-run change; it does not disturb any in-flight run.
