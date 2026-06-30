# Cross-Project Heuristic Transfer — Experiment Design

**Date:** 2026-06-30
**Status:** Proposed design, pending implementation plan
**Scope:** A de-risking pilot that runs on the existing 200-bug / 59-project ARVO slice
(`arvo_new.db`) to decide whether the heuristic learning loop should be scaled from
one project (mruby) to all of OSS-Fuzz.

## 1. Problem & Goal

The mruby heuristic learning loop
(`2026-06-29-mruby-heuristic-learning-loop-design.md`) works because mruby supplies 30
bugs with a dominant recurring family (11/30 SUAR), so **in-project** transfer is
guaranteed and the whole playbook fits in context. The OSS-Fuzz distribution is the
opposite: in our own slice the median project has **2 bugs and 25/59 projects have
exactly one** — too few to ever accumulate an in-project memory. Scaling to OSS-Fuzz is
therefore worthwhile **only if heuristics transfer across projects within a bug class.**

The encouraging prior: the dominant crash families are allocator/language-level, not
project-specific — `Use-of-uninitialized-value` (49), heap-buffer-overflow (~31),
use-after-free (~14), stack-use-after-return (9). A lesson keyed on the *bug class and
code pattern* rather than project internals plausibly generalizes.

**Goal:** measure cross-project transfer directly and cheaply on data we already have,
and emit a **pre-registered go/no-go** for the global retrieval layer — before spending a
multi-thousand-bug compute program on an unproven hypothesis.

**Non-goals (YAGNI):**
- Not building the global vector/retrieval store. This experiment decides *whether* to.
- Not running the full ARVO corpus. The 200-row slice is the de-risking instrument.
- Not changing the agent, the faithfulness wall, or the oracle's mechanics.

## 2. Hypothesis & Pre-Registered Decision Rule

**H1 (transfer):** Injecting heuristics extracted from *other projects'* bugs in the
*same crash class* raises the verified-correct rate on a target bug, **beyond** the effect
of injecting an equal volume of *unrelated-class* foreign heuristics.

**Primary contrast (pre-registered): arm B vs arm B′** (matched-class foreign vs
mismatched-class foreign placebo) on verified-correct rate over the eval set. This
isolates transfer of *relevant content* from the artifact of "more text in context."

**Decision rule, fixed before looking at results:**

| Outcome | Conclusion |
|---|---|
| B significantly > B′ (and B ≥ A) | **Go** — build the global retrieval layer; transfer is real. |
| B ≈ B′ ≈ A | **No-go** — federate only (keep per-project memories); transfer is an artifact. |
| B > B′ in some classes only | **Partial** — build the global layer **scoped to the transferring classes**; report per-class. |

Secondary (exploratory, not decision-gating): B vs A (total benefit of foreign memory),
C vs A (in-project reference), oracle-confirmed rate as a stronger correctness metric.

## 3. Core Decisions

| Axis | Decision |
|------|----------|
| Instrument | Existing 200-bug / 59-project slice; no new data collection |
| Unit of transfer | Crash **class** (coarse family), keyed to a fixed taxonomy (§5) |
| Memory build | **One cold pass** builds a frozen heuristic store H; arms differ only in the *read filter* over H (frozen-playbook protocol) |
| Arms | A cold, B matched-foreign, B′ mismatched-foreign placebo, (C in-project, reference) |
| Eval set | Bugs in multi-project classes that have ≥1 chronologically-prior foreign donor |
| Design | **Paired within-bug** across arms; m trials per (bug, arm) for stochasticity |
| Primary metric | verified-correct rate; secondary: oracle-confirmed rate |
| Leakage control | Global `localId` chronology + same-project exclusion in B/B′ |
| Faithfulness | Oracle/`-fix` wall unchanged; grader output never reaches the agent |

## 4. Experimental Design

### 4.1 Two phases

**Phase A — build the frozen memory (one pass, cold).**
Walk all slice bugs in **global `localId`** order. Run the agent **cold** (no injection),
verify, and on verified-correct + oracle-non-divergent, extract a heuristic tagged with
`source_project`, `crash_class`, and `added_after_bug = localId`. Cold so donors are not
themselves contaminated by injection effects. Result: frozen store **H**. (Cached cold
results from prior runs may be reused to avoid re-spending this pass.)

**Phase B — measure transfer (one pass per arm).**
Walk the eval set; for each (bug, arm, trial) retrieve the arm-specific eligible slice of
H, inject, run the agent, verify, and grade. All arms read the *same frozen H* — only the
filter differs — which removes the divergent-history confound and lets the arms share
Phase A.

### 4.2 Arms (the read filter over H, for target bug t in project P, class K)

A heuristic h is eligible for t only if `h.added_after_bug < t.localId` (chronological
holdout — never see the present or future), **and** the arm predicate holds:

| Arm | Predicate | Purpose |
|---|---|---|
| **A — cold** | inject nothing | baseline |
| **B — matched foreign** | `h.source_project ≠ P` **and** `h.crash_class == K` | **the treatment** — isolates cross-project transfer |
| **B′ — placebo foreign** | `h.source_project ≠ P` **and** `h.crash_class ≠ K`, sampled to **match B's count and total size** for t | controls for injection/context-length artifact |
| **C — in-project (reference)** | `h.source_project == P` **and** `h.source_bug ≠ t` | upper-bound sanity that the memory mechanism works at all |

Same-project lessons are excluded in B/B′ by construction, so any B lift is *purely*
foreign. B′ draws the **same number and roughly the same byte size** of heuristics as B
for that bug (deterministic seed per bug) so the only variable between B and B′ is class
relevance.

### 4.3 Eval-set selection

Include t only if transfer is *possible* for it:
1. `crash_class(t)` appears in ≥2 distinct projects in the slice, **and**
2. at least one qualifying foreign donor exists with `added_after_bug < t.localId`
   (i.e. B's slice is non-empty), **and**
3. B′ can be filled to match B's count from mismatched-class foreign donors.

Single-project-class bugs are excluded as *targets* but their heuristics may still serve
as B′ placebo donors. This focuses compute where transfer can even register. (Donors
accumulate over chronological time, so early bugs naturally qualify less — see §4.5.)

### 4.4 Metric & statistics

Per (bug, arm, trial) outcome, ordinal:
- `0` = not solved (no patch / build fail / crash remains / verifier fail)
- `1` = solved (crash gone + verifier) but oracle `divergent`/`no_fix`/`error`
- `2` = `oracle_confirmed`

**Primary:** verified-correct rate = fraction with score ≥ 1, per arm over the eval set.
**Secondary:** oracle-confirmed rate = fraction with score = 2 (treated as PoC-arm-only
outside mruby — see §6).

- **m trials per (bug, arm)** (default m = 3); per-bug per-arm value = mean score.
- **Paired within-bug** analysis (same bugs traverse every arm): bug difficulty dominates
  raw rates, so contrasts are computed per-bug then aggregated.
- Report **effect size with a bootstrap CI** over bugs for B−A and **B−B′**, plus a
  paired permutation (or Wilcoxon) test on the per-bug means for the **single
  pre-registered B vs B′** contrast. Everything else is exploratory.
- N of eval bugs is stated up front. This is a **pilot → directional**, not a
  significance claim; the deliverable is an effect estimate + CI + the §2 decision.

### 4.5 Transfer-vs-donors curve

Because donors accumulate in chronological order, report B−B′ as a function of
**donors-available at t** (and of `crash_class`), not only as a single pooled number. A
transfer signal that grows with donor count is far stronger evidence than a flat pooled
delta, and tells the full-corpus build how much memory is needed before lift appears.

## 5. Crash-Class Taxonomy

Raw `crash_type` is noisy (`UNKNOWN READ`, `Heap-buffer-overflow READ 1` vs `READ 8`).
Normalize to a coarse, explicit family — the matched/mismatched key:

| Family | Maps from (crash_type prefix) |
|---|---|
| `uninit` | Use-of-uninitialized-value |
| `heap-oob` | Heap-buffer-overflow (READ/WRITE, any size) |
| `stack-oob` | Stack-buffer-overflow, Stack-use-after-return |
| `uaf` | Heap-use-after-free, Use-after-free |
| `null-deref` | Null-dereference, Segv on unknown address (null page) |
| `other` | everything else (incl. `UNKNOWN READ`) |

The READ/WRITE size suffix is collapsed. The mapping is a small committed dict, unit-
tested, and frozen for the run so eligibility is deterministic and reviewable.

## 6. Threats to Validity (and controls)

1. **Injection/context-length placebo** → arm B′ (matched count+size, mismatched class).
   The decision rides on B vs B′ precisely to neutralize this.
2. **In-project leakage masquerading as transfer** → B excludes same-project donors.
3. **Chronological leakage** → global `localId` holdout; extend the existing strict-
   increasing/unique assertion (`tests/test_mruby_bugs.py`) to the whole slice.
4. **Cross-project duplicate bugs** (ARVO carries near-duplicates) → dedup donors by
   `fix_commit` / patch similarity; flag eval bugs whose qualifying foreign donor shares a
   near-identical patch and analyze them as a separate stratum (that's copy, not transfer).
5. **Donor-count imbalance** → B and B′ matched per-bug; results stratified by donor count
   (§4.5).
6. **Stochastic agent variance** → m trials + paired within-bug analysis.
7. **Oracle weakness at scale** → outside mruby the differential oracle is effectively
   PoC-only (no per-project script battery), so `oracle_confirmed` is the weaker secondary
   metric; verified-correct is primary. Stated honestly in the report.
8. **Multiple comparisons** → exactly one pre-registered contrast (B vs B′); all else
   exploratory and labeled as such.

## 7. Data / Ledger

Each `results/transfer/ledger.jsonl` record:
`{bug_id, project, crash_class, arm, trial, score, classification, oracle_label,
n_donors, donor_ids, donor_bytes, playbook_version}`. `donor_ids` makes every injection
auditable and lets us re-derive the B/B′ matching post hoc. The analysis script in §8
consumes only this file.

## 8. Implementation Phases (for the plan)

- **Phase 0 — Tagging:** thread `source_project` + `crash_class` onto extracted heuristics
  (`extract_heuristic` output + `playbook_store.add_heuristic`); commit the §5 taxonomy
  with unit tests.
- **Phase 1 — Retrieval filter:** an arm-parameterized eligibility filter over H
  (`active_heuristics` generalized: holdout + project + class predicate + B′ matched
  sampling with a per-bug deterministic seed). Pure function, unit-tested without Docker.
- **Phase 2 — Eval-set selector:** §4.3 selection over the slice; unit-tested against a
  synthetic bug table.
- **Phase 3 — Build pass (Phase A):** cold global pass producing frozen H (or adapter to
  reuse cached cold results).
- **Phase 4 — Arm runner (Phase B):** generalize `learn_loop.run_pass` to take an arm +
  trial and the frozen H; write the §7 ledger. Reuse `verify`, `grade`, `inject`.
- **Phase 5 — Analysis:** ledger → per-bug paired means → B−B′ effect + bootstrap CI +
  permutation test + the donors-curve (§4.5) + the §2 decision.

## 9. Testing

- **Unit (no Docker):** taxonomy mapping; holdout+project+class filter (B excludes same
  project, B′ excludes same class and matches B's count, A empty); eval-set selector;
  global chronology assertion; ledger record shape.
- **Wall test:** the arm filter and donor metadata never reach agent feedback / repair
  loop / contrastive prompt (mirrors the existing `-fix`-wall tests).
- **Integration (opt-in, Docker):** one eval bug through arms A/B/B′ end-to-end on cached
  builds, asserting the injected donor set matches the arm predicate.

## 10. Tech & Reuse

- Python, matching the repo. Reuse `learn_loop.run_pass`, `verify_fix.verify`,
  `differential_oracle.grade`, `extract_heuristic`, `injector.inject`, `ledger`.
- Bug metadata + `crash_type` from `arvo_new.db`; generalize `mruby_bugs.mruby_bug_ids`
  to a project-agnostic global-`localId` loader.
- No new datastore: H is the existing JSON store with two extra tags; results are JSONL.
- A vector/retrieval store is **explicitly out of scope** — this experiment decides
  whether to build it. The filter here is a linear scan over H (trivial at slice scale).

## 11. Known Limitations

- The slice is C/C++ only and tops out at 30 bugs/project; a positive result generalizes
  to the full corpus only as a *directional* prior, and says nothing about other-language
  transfer.
- `oracle_confirmed` inherits the §8 limitation of the differential-oracle design (canonical
  `-fix` may itself be wrong); outside mruby it is also PoC-arm-only. Hence verified-correct,
  not oracle-confirmed, is the decision metric.
- A no-go result rejects *global memory*, not *per-project memory*: the mruby-style loop
  stands either way.
