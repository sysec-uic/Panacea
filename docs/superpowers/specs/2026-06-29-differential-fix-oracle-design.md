# Differential `-fix` Oracle — Design

**Date:** 2026-06-29
**Status:** Approved design, pending implementation plan
**Scope:** A signal-quality upgrade to the mruby Heuristic Learning Loop
(`docs/superpowers/specs/2026-06-29-mruby-heuristic-learning-loop-design.md`). This is the
v2 differential oracle that loop design §4.4 deferred.

## 1. Problem & Goal

The learning loop accumulates a playbook of heuristics that is injected into the agent on
later bugs. A heuristic is only as trustworthy as the verdict that produced it. Today a run
is labelled `verified_correct` on **crash-gone + `rake test` passes** (`verify_fix.py`).
That gate admits patches that silence the crash but are subtly wrong on inputs the suite
does not cover — exactly the failure family documented in the `mruby-444773339-real-fix-is-buggy`
and `arvo-fix-root-cause-not-crash-site` analyses. In a *learning* system this is worse than
in a one-shot fixer: a single wrong verdict becomes a confidently-wrong heuristic that is
injected into **every later bug**, so bad signal compounds.

**Goal:** before a lesson is learned, compare the agent's accepted patch against the
canonical upstream fix (`n132/arvo:{localId}-fix`). If the patched build *behaves differently
from the fix build*, suppress the lesson. If it matches, promote it to high confidence. If no
`-fix` image exists, learn exactly as today. **The agent never sees any of this** — the
oracle is a post-hoc grader of heuristic confidence, not a source of repair feedback.

**Non-goals (YAGNI):**
- Not a new agent feedback signal. The deployment-faithful wall (agent/repair/contrastive
  paths never read `-fix`) is preserved unchanged.
- Not absolute correctness. The oracle measures *divergence from canonical*, not ground
  truth (see §8 limitation).
- Not a fuzzing/property-based differential. The probe set is the crash PoC plus a small,
  committed script battery — deterministic and reviewable, not generative.

## 2. Core Decisions (settled during brainstorming)

| Axis | Decision |
|------|----------|
| What to diff on | **PoC probe + committed mruby script battery** (catches wrong-output regressions, not just crash/no-crash) |
| What the label does | **Veto + promote**: divergent → suppress lesson; confirmed → high confidence; no `-fix` → learn as today (`tests_only`) |
| Where it runs | Post-hoc, in `learn_loop.py` only, after `repair_with_retries` returns the accepted diff, before `add_heuristic` |
| Faithfulness | Grader output flows **only** into the ledger and the add/suppress decision — never into agent feedback, the repair loop, or the contrastive prompt |
| Ground truth | `-fix` image is treated as canonical; the oracle inherits any error in the upstream fix (§8) |

## 3. Architecture Overview

A new module `differential_oracle.py` (in `arvo-eval/`), structured like `verify_fix.py`
and reusing its Docker helpers (`docker_exec`, the build/apply steps). It is invoked from
`learn_loop.py` at the single integration point identified below.

```
learn_loop: repair_with_retries -> accepted diff (crash gone + rake test passed)
                                       │
                          extract lesson (success or contrastive)
                                       │
                 differential_oracle.grade(bug, accepted_diff)   <-- NEW
                                       │
        ┌──────────────┬──────────────┴───────────────┬─────────────────┐
   oracle_confirmed   divergent              no_fix_available        oracle_error
        │              │                            │                     │
  add_heuristic    SUPPRESS                   add_heuristic          add_heuristic
  confidence=high  (record only)              oracle=tests_only      oracle=tests_only
  oracle=confirmed                            (extractor confidence) (extractor confidence)
```

**Integration point (`learn_loop.py`, today lines 95–104):** inside `if solved:`, after the
`lesson` is produced by `extract`/`contrastive` and **before** `add_heuristic`. This is the
only call site.

## 4. Components

### 4.1 Grader — `differential_oracle.py`

```python
def grade(bug: dict, agent_diff: str, *, probes: list[Path], patched_container: str | None = None) -> dict:
    """Return {label, fix_image_available, divergences}.

    label ∈ {"oracle_confirmed", "divergent", "no_fix_available", "oracle_error"}
    divergences: list of {probe, kind: "exit"|"stdout", agent_out, fix_out}
    """
```

**Mechanics**
1. **Resolve the fix image.** Check `n132/arvo:{bug['localId']}-fix` is available
   (`docker image inspect`, falling back to a `docker pull`). If unavailable →
   `{"label": "no_fix_available", "fix_image_available": False, "divergences": []}`.
2. **Obtain the agent-patched build.** If `patched_container` is supplied (reuse handle from
   `verify_fix`, see §4.3) use it. Otherwise build fresh: run a `-vul` container, `git apply`
   the diff, `compile` — mirroring `verify_fix.verify`. A build failure here → `oracle_error`.
3. **Start the fix container** from `n132/arvo:{id}-fix` (prebuilt; no compile).
4. **Run the probe battery on both containers:**
   - *PoC probe:* `arvo` (harness on `/tmp/poc`). Primary signal — exercises the exact buggy
     path. The agent build is known not to crash (already `verified_correct`); we compare its
     post-fix behaviour against the fix build's.
   - *Script probes:* for each committed `*.rb`, run `cd /src/mruby && bin/mruby <script>` in
     both containers. Regression guard for collateral behaviour change.
5. **Normalize then compare** (see §4.2). Divergence = exit-code mismatch **or** normalized
   stdout mismatch on **any** probe.
6. **Label:** any divergence → `divergent`; all probes agree → `oracle_confirmed`; any
   Docker/run error → `oracle_error`.

### 4.2 Output normalization

Before comparing agent-vs-fix output, strip non-deterministic and sanitizer noise so that
only *semantic* differences register:
- sanitizer banners/summary lines (`==NNN==`, `AddressSanitizer`, `MemorySanitizer`, `SUMMARY:`),
- hex addresses (`0x[0-9a-f]+`), PIDs/TIDs, absolute build paths,
- trailing whitespace and timing lines.
Comparison is on `(exit_code, normalized_stdout+stderr)` per probe. Normalization is a pure
function — unit-tested without Docker.

### 4.3 Reuse handle in `verify_fix.py` (build-cost optimization)

A second full `compile` is the dominant cost. `verify_fix.verify` already builds the
agent-patched container; today it tears the container down unless `keep=True`. Extend it to
optionally **return the live container name** when the verdict is `verified_correct`, so
`learn_loop` can hand that container to `grade(..., patched_container=...)` instead of
rebuilding. The fix image is prebuilt, so the grader then does no compilation at all.
`learn_loop` is responsible for removing the reused container after grading.

### 4.4 Probe battery — `differential/mruby_probes/*.rb`

A small, committed set of mruby scripts (target: 5–10) chosen to exercise common
interpreter paths (integer/bignum arithmetic, string ops, arrays/hashes, set operations,
GC-triggering allocation loops) — the families the mruby ARVO bugs cluster in. Committed to
the repo (not scraped from the container) so the probe set is deterministic, reviewable, and
versioned. Each must produce deterministic stdout and exit 0 on a correct interpreter.

The PoC remains the highest-value probe because it hits the exact faulting path; the script
battery exists to catch patches that fix the PoC path but break something adjacent.

### 4.5 Label → learning, in `learn_loop.py`

Replace the unconditional `add_heuristic` in the `if solved:` block with:

| Grader label | Action |
|---|---|
| `oracle_confirmed` | `add_heuristic(..., confidence="high", oracle="confirmed")` |
| `divergent` | **Suppress** — do not add the heuristic. Record divergence in the ledger. |
| `no_fix_available` | `add_heuristic(...)` with the extractor's own confidence, `oracle="tests_only"` |
| `oracle_error` | Same as `no_fix_available` (never penalize the agent for grader flakiness) |

`add_heuristic` / `playbook_store` gain an optional `oracle` field threaded onto the stored
heuristic and a confidence override; the render path may surface `oracle=confirmed` so the
agent can weight trusted lessons. The suppress path is intentionally simple: the lesson is
dropped, not stored as a caution. (A low-confidence "caution" variant was considered and
rejected for v1 as added complexity without demonstrated need.)

### 4.6 Ledger additions — `results/learn/ledger.jsonl`

Each record gains `oracle_label`, `fix_image_available`, `n_divergences`. This makes the
upgrade **self-measuring**: over a run we can report how many `verified_correct` runs were
actually `divergent` — a direct count of wrong lessons the oracle kept out — and how many
bugs had no `-fix` available (the unguarded remainder).

## 5. Data Flow

```
verify_fix.verify(bug) ──verified_correct + live container──┐
                                                            │
extract / contrastive ──lesson──┐                           │
                                ▼                            ▼
                 differential_oracle.grade(bug, diff, patched_container) 
                                │
            label ──► add_heuristic (confirmed/tests_only)  OR  suppress
                                │
                          ledger.jsonl  (oracle_label, fix_image_available, n_divergences)
```

## 6. Faithfulness Invariants (the `-fix` wall)

The existing wall — agent, `repair_loop`, feedback, and `contrastive_extract` never read
`-fix` — is preserved. The grader is the *only* `-fix` consumer and it lives strictly
downstream of the agent:
1. Called only from `learn_loop`, after `repair_with_retries` has returned, on the accepted
   diff and bug metadata.
2. Its return value is consumed only by (a) the ledger and (b) the add/suppress/confidence
   decision. It is never threaded back into agent feedback, the repair loop, or the
   contrastive prompt.
3. A test asserts the grader is unreachable from the repair/feedback path; the existing
   `-fix`-absent tests (`tests/test_repair_loop.py`, `tests/test_contrastive_extract.py`)
   stay green.

## 7. Error Handling & Scope Guards

- Fix image missing → `no_fix_available`; learn as today. The system keeps working on
  fix-less bugs.
- Grader build/run failure or timeout → `oracle_error`; treated like `no_fix_available`. A
  flaky grader must never cost a real lesson.
- Probe that is non-deterministic even on the fix build (flags itself by differing fix-vs-fix
  on a repeat) is excluded from the battery during curation; probes are vetted before commit.
- Suppressed (`divergent`) runs are still recorded in the ledger so the suppression is
  auditable.

## 8. Known Limitation (documented, not fixed)

The oracle treats the `-fix` image as canonical. Where the **upstream fix is itself wrong**
— `mruby-444773339-real-fix-is-buggy`, whose accepted fix stops the crash but returns wrong
sums — a matching-buggy agent patch would be *confirmed*, and a *correct* agent patch that
diverges from the buggy upstream would be wrongly *suppressed*. The oracle catches divergence
from canonical, not absolute correctness. This is a net win for the common case (the upstream
fix is usually right) but the `oracle_confirmed` label must not be read as a proof of
correctness. Surfacing this honestly in the ledger/analysis is part of the deliverable.

## 9. Implementation Phases (for the plan)

- **Phase 0 — Spike:** confirm `n132/arvo:{id}-fix` exists and runs `arvo`/`bin/mruby` for a
  couple of mruby bugs; confirm output is comparable after normalization (fix-vs-fix on a
  repeat is identical).
- **Phase 1 — Normalization + compare:** pure functions, fully unit-tested (no Docker).
- **Phase 2 — Probe battery:** author and vet `differential/mruby_probes/*.rb`; assert each
  is deterministic on a fix build.
- **Phase 3 — Grader:** `differential_oracle.grade`, reusing `verify_fix` helpers; label
  logic for all four cases.
- **Phase 4 — verify_fix reuse handle:** optional kept-container return; `learn_loop` cleanup.
- **Phase 5 — learn_loop wiring:** veto/promote at the integration point; `oracle` field
  through `playbook_store`; ledger fields.
- **Phase 6 — Measurement:** report from the ledger how many `verified_correct` runs were
  `divergent` and the `no_fix_available` remainder.

## 10. Testing

- **Unit (no Docker):**
  - normalization strips banners/addresses/PIDs; identical-modulo-noise outputs compare equal.
  - divergence logic: exit mismatch and stdout mismatch each flagged; agreement → confirmed.
  - label mapping for `oracle_confirmed` / `divergent` / `no_fix_available` / `oracle_error`.
  - `learn_loop` add-vs-suppress + confidence override against a **stubbed grader** for each
    label (mirrors existing `--dry-run` stub style).
  - `playbook_store` carries the `oracle` field and honors the confidence override.
- **Wall test:** grader symbol is unreachable from `repair_loop`/feedback; `-fix` absent
  tests remain green.
- **Integration (opt-in, Docker):** grade one mruby bug expecting `oracle_confirmed`; craft
  or identify a `divergent` case to prove the veto fires end-to-end.

## 11. Tech & Reuse

- Python, matching the repo. Reuse `verify_fix.docker_exec` and its build/apply steps;
  reuse `build_instance.load_bug` for metadata and the `n132/arvo:{id}-{vul,fix}` naming.
- No new datastore: ledger gains fields; playbook heuristics gain an `oracle` field.
- Probe scripts are plain `.rb` committed under `arvo-eval/differential/mruby_probes/`.
