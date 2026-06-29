# mruby Heuristic Learning Loop — Design

**Date:** 2026-06-29
**Status:** Approved design, pending implementation plan
**Scope:** A single project (mruby) within the ARVO dataset.

## 1. Problem & Goal

We run an automated vulnerability-repair agent (OSS-CRS / crs-claude-code, driven by
`arvo-eval/arvo_oss_crs.py`) against ARVO bugs. Today each bug is fixed cold: lessons
learned on one bug do not carry to the next. mruby is the largest project in the dataset
(30 bugs) and has a strong recurring failure family — **~11 of 30 bugs are
`Stack-use-after-return READ 4`** (the pool/stack-escape pattern documented in the
`439494108` analysis). This is exactly the kind of repetition a memory of heuristics
should exploit.

**Goal:** a *self-improving agent memory* for mruby. After each bug is fixed and verified,
an LLM extracts a reusable heuristic; the heuristic is curated into a single compact
"playbook" that is injected into the agent's context on subsequent mruby bugs. Over a
chronological run, the agent should fix later bugs more often / more correctly because it
carries forward what worked on earlier ones.

**Non-goals (YAGNI):**
- Not a predictive triage model, patch-ranking verifier, or general benchmark harness.
- Not multi-project. Heuristics may be mruby-specific (mruby-set khash, GC write barriers,
  bigint pool escapes). Portability is explicitly out of scope for v1.
- Not learning from failures ("negative heuristics") in v1 — see §7.

## 2. Core Decisions (settled during brainstorming)

| Axis | Decision |
|------|----------|
| Core behavior | Self-improving agent memory (accumulate → inject) |
| Target | mruby only, deep & specific |
| Heuristic source | Auto-extract via LLM after each run |
| Retrieval/injection | Inject **all** — one curated, self-compressing playbook every run |
| Eval protocol | Chronological holdout (order by `localId`) |
| Architecture | Thin orchestrator wrapping `arvo_oss_crs.py` + file-based injection |
| Correctness gate | **v1: no-crash (sanitizer-aware) + `make test`.** Differential-vs-`-fix` deferred. |

## 3. Architecture Overview

A new orchestrator `learn_loop.py` (in `arvo-eval/`) walks mruby's 30 bugs in `localId`
order (monotonic with OSS-Fuzz issue date, so chronological for free). For each bug:

```
load_bug
  → snapshot current playbook (version vN)
  → inject playbook into agent context
  → run OSS-CRS pipeline (unchanged arvo_oss_crs.run_oss_crs)
  → collect patch
  → verify: sanitizer-aware no-crash  AND  `make test` passes
  → append run record to ledger
  → if verified correct:
        extract heuristic (LLM)
        curate into playbook  → version vN+1
  → next bug consumes vN+1
```

**Holdout guarantee (no leakage):** a bug's own heuristic is only added to the playbook
*after* that bug has been evaluated. Processing strictly in `localId` order means no bug is
ever tested against a playbook that contains its own lesson. This invariant is asserted in
tests (§9).

## 4. Components

### 4.1 Orchestrator — `learn_loop.py`
Drives the loop above. Reuses `build_instance.load_bug` and `arvo_oss_crs.run_oss_crs`.
Reads the mruby bug list from `arvo_new.db` (`SELECT localId FROM arvo WHERE project='mruby'
ORDER BY localId`). Flags: `--inject/--no-inject` (control vs treatment, §6),
`--bugs <subset>`, `--skip-build` (passthrough), `--dry-run` (stub agent for integration
tests).

### 4.2 Playbook store
- `arvo-eval/playbook/mruby_playbook.md` — the human/agent-readable curated digest that is
  injected. Compact (target < ~2–3 KB) so it never dominates context.
- `arvo-eval/playbook/playbook_state.json` — sidecar with provenance and versioning:
  ```json
  {
    "version": 7,
    "heuristics": [
      {"id": "h-439494108", "source_bug": 439494108, "tags": ["suar", "bigint-pool"],
       "added_after_bug": 439494108, "confidence": "high", "text": "..."}
    ]
  }
  ```
  `added_after_bug` is what the holdout-invariant test checks against the bug being run.

### 4.3 Injector
Writes the current playbook into the agent's reachable working context. **Mechanism is the
one genuine unknown** and is resolved by a spike (§8, Phase 0): the likely path is dropping
a `CLAUDE.md` / `HEURISTICS.md` into the wrapped project working tree that crs-claude-code
reads on startup. If file-injection proves unreliable, fall back to prompt-level injection
(prepend the playbook to the task prompt OSS-CRS hands the agent). The injector is a small
module with one function `inject(playbook_text, project_dir) -> None` so the mechanism can
change without touching the loop.

### 4.4 Verifier (correctness-gated) — extend `verify_fix.py`
Two changes to today's `verify_fix.py`:
1. **Sanitizer-aware crash detection.** Current code (`verify_fix.py:88`) greps only
   `AddressSanitizer`; mruby has 6 MSan bugs and 1 `Check failed`. Generalize the crash
   signature to the bug's actual sanitizer (`MemorySanitizer`, `AddressSanitizer`,
   `UndefinedBehaviorSanitizer`, runtime aborts).
2. **Correctness oracle v1 = no-crash + `make test`.** After confirming the rebuilt target
   no longer crashes on `/tmp/poc`, run mruby's own test suite inside the container
   (`cd /src/mruby && make test` or `rake test`, exact target confirmed during Phase 0).
   A run is **verified correct** only if: patch applies, builds, poc no longer crashes,
   AND `make test` passes. Anything else → not correct → does **not** feed the extractor.

   *Deferred (v2):* differential testing against the `n132/arvo:<id>-fix` image to catch
   patches that silence the crash but change behavior — the stronger oracle our prior
   analyses (`mruby-444773339-real-fix-is-buggy`, `arvo-fix-root-cause-not-crash-site`)
   show is sometimes necessary. v1 ships without it to get the loop running; §7 notes the
   risk this leaves open.

### 4.5 Extractor — `extract_heuristic.py`
One Claude API call. **Input:** bug metadata (crash_type, sanitizer, fuzz_target, faulting
frames), crash trace, the agent's accepted diff, a trajectory summary, and the verification
verdict. **Output:** a structured heuristic
`{trigger, root_cause_lesson, how_to_apply, tags, source_bug, confidence}`. Only invoked
for verified-correct runs.

### 4.6 Curator/compressor
LLM step that merges a newly extracted heuristic into `mruby_playbook.md`: dedup against
existing entries, resolve contradictions, compress, keep the digest under the size target.
Runs after each successful extraction. Updates `playbook_state.json` version.

### 4.7 Ledger — `arvo-eval/results/learn/ledger.jsonl`
One record per (bug, pass): `{bug_id, pass: "control"|"treatment", classification,
verified_correct, make_test_passed, tokens, elapsed_s, playbook_version, heuristic_ids_present}`.
This is the experiment's raw data and the input to the analysis in §6.

## 5. Data Flow Diagram

```
arvo_new.db ──(mruby ids, localId order)──> learn_loop.py
                                               │
              playbook vN ──inject──> [ OSS-CRS agent run ] ──patch──> verify_fix.py
                   ▲                                                       │
                   │                                              (no-crash + make test)
              curator <── extract_heuristic.py <──(only if correct)──── verdict
                   │                                                       │
              playbook vN+1                                          ledger.jsonl
```

## 6. Measuring Self-Improvement

The chronological holdout fixes the *ordering*; to claim the playbook *helps* we need a
baseline. The experiment is **two passes over the same `localId` ordering**:

- **Control pass:** `--no-inject` (agent runs cold, current behavior). Heuristics are still
  extracted and the playbook is built, but never injected.
- **Treatment pass:** `--inject` (playbook injected each bug).

Compare **verified-correct rate on the later ~two-thirds of bugs** (where an accumulated
playbook exists) between control and treatment. The SUAR-escape cluster (11/30) is the
primary place to look for lift.

**Explicit caveat (must stay in scope expectations):** N=30, with ≈20 bugs in the holdout
tail. This is a **pilot** producing *directional* signal, not statistical proof. Results
are reported as a learning curve + control/treatment delta, not a significance claim.

## 7. Error Handling & Scope Guards

- No patch / apply-failed / build-failed / still-crashes / `make test` fails → recorded as
  a failure in the ledger; **no heuristic extracted.** v1 learns only from verified-correct
  runs.
- **Negative heuristics deferred:** learning from failed attempts is tempting but teaches
  from noise (a failure may be agent flakiness, not a real lesson). Out of scope for v1.
- Extractor or curator LLM failure → playbook left unchanged at vN, error logged, loop
  continues to the next bug.
- Injection-mechanism failure (Phase 0 finds no reliable file hook) → fall back to
  prompt-level injection behind the same `inject()` interface.
- **Known residual risk:** the v1 no-crash + `make test` oracle can still admit a patch that
  passes the suite but is subtly wrong on inputs the suite doesn't cover. Accepted for v1;
  v2 differential oracle (§4.4) is the mitigation.

## 8. Implementation Phases (for the plan)

- **Phase 0 — Spikes (no learning yet):**
  (a) Confirm the injection point: get a known string from `mruby_playbook.md` to
  demonstrably reach the agent's context on one mruby bug.
  (b) Confirm the `make test` invocation that runs mruby's suite inside an ARVO container.
- **Phase 1 — Verifier upgrade:** sanitizer-aware crash detection + `make test` gate in
  `verify_fix.py`, with unit coverage on the classification logic.
- **Phase 2 — Store + injector + ledger:** playbook read/write/version, holdout-invariant
  enforcement, `inject()`, ledger writes.
- **Phase 3 — Extractor + curator:** LLM extract and merge/compress.
- **Phase 4 — Orchestrator:** `learn_loop.py` wiring it together; `--dry-run` with a stub
  agent for integration testing on cached bugs.
- **Phase 5 — Experiment:** control pass, then treatment pass over all 30 mruby bugs;
  produce the §6 analysis.

## 9. Testing

- **Unit:** playbook store add/dedup/version; ledger record shape; verifier classification
  for each sanitizer; **holdout-invariant assertion** — for every bug run, no injected
  heuristic has `added_after_bug >= current_bug_localId`.
- **Integration:** `learn_loop.py --dry-run` over 2–3 cached mruby bugs (e.g. 439494108,
  440058794) with a stubbed agent that returns a known patch, exercising inject → verify →
  extract → curate without spending real agent budget.
- **The experiment itself** (Phase 5) is the end-to-end validation.

## 10. Tech & Reuse

- Python, matching the repo. Reuse `build_instance.load_bug`, `arvo_oss_crs.run_oss_crs`,
  and extend `verify_fix.py`.
- Bug metadata from `arvo_new.db` (sqlite).
- Playbook = markdown + JSON sidecar. No new datastore.
- Extractor/curator via the Claude API (Opus 4.8), consistent with the existing OSS-CRS
  agent model.

## 11. Open Items Resolved by Phase 0

1. Exact injection hook for crs-claude-code (file vs prompt).
2. Exact `make test`/`rake test` target and runtime inside the mruby ARVO containers.

Both are de-risking spikes, not design unknowns — the architecture holds either way.
