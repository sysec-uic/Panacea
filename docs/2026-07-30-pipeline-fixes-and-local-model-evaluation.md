# Pipeline fixes + local-model evaluation (Jul 29–30, 2026)

A working session on the `arvo-eval` local-model repair pipeline. It produced two
durable pipeline fixes, a full evaluation of a candidate replacement model that
concluded in a revert, and — most importantly — the first proper *measurement* of the
incumbent local model's solve rate across a spread of bugs. Setup throughout: the
OSS-CRS `crs-claude-code` repair agent driving a local model served through an
OpenAI-compatible bridge, with inline crash orientation, the in-turn `check-patch`
self-check, and the recon→forced-edit two-pass all enabled.

## Two pipeline fixes (committed)

### 1. Forced-edit gate scoped to the authoritative source tree

The recon→forced-edit two-pass decides whether to force a second "make the edit now"
pass by counting the agent's edits at the recon cap. That count included **every**
edit, so stray edits to the throwaway worktree, scratch files, or a downloaded copy
of the source falsely satisfied the gate — the gate stayed hands-off even when nothing
submittable had been written.

Fix: count only edits under the tree a patch is actually submitted from (the download
copy the check-patch flow validates when check-patch is enabled, otherwise the in-place
source). Verified on a real transcript where the old, unscoped count (7) split into 2
authoritative edits + 5 stray — the gate now measures the submittable ones.

### 2. check-patch no longer self-poisons across re-checks

The check-patch service reuses **one** warm vulnerable-build container across every
in-turn self-check, and nothing reset the source tree between checks. `git apply` is
atomic on failure but leaves the tree modified on success — so the first diff that
applied poisoned the tree, and every later diff then failed "did not apply to a fresh
checkout." That was the dominant check-patch failure (12–14× per run).

Fix: reset the tree to pristine before each apply attempt (a no-op on a clean tree, so
the incremental-build cache is preserved), and escalate strict `git apply` to a 3-way
apply that survives context drift between the agent's working copy and the fresh
checkout. Confirmed end-to-end on a bug that previously drowned in plumbing failures:
"did not apply" went **14 → 0**, and every check now reaches real crash/test
evaluation. This moved the wall from *diff plumbing* to *fix correctness*.

## Candidate model evaluation: concluded in a revert

Motivated by the correctness ceiling above, we researched a stronger model that fits
the current VRAM budget, confirmed it was servable, and evaluated it — then reverted.

- **Selection & serving.** Picked a larger mixture-of-experts coder that fits the budget
  at a healthy 4-bit quant, confirmed it runs on the existing serving stack (GGUF +
  the bridge), and brought it up. Tool-calling through the bridge worked on first try.
- **Four runs, four non-acting failures.** The model never completed the
  edit→check-patch loop. Each fix surfaced a new avoidance behavior: it looped on an
  "ask the user" tool (no human exists in a headless run); with that disabled it fell
  into a verbatim generation loop (same reasoning + same command repeated); a sampling
  change (recommended sampling + a repetition-suppressing sampler, forced in the bridge)
  broke that loop, but it then looped on task-management/worktree meta-tools; with the
  whole meta-tool surface disabled it used the shell to keep *reading* and still never
  edited — plus a raw generation defect (substituting a non-ASCII token into file
  paths). Its *reasoning* was on-target (it correctly diagnosed a hard use-after-free
  root cause), but it could not *execute* the agentic loop.
- **Conclusion & revert.** A definitive model–harness fit failure, not a series of
  independent quirks. Reverted to the incumbent model. Kept one model-agnostic
  improvement — an expanded agent disallow-list stripping the ask/plan/task/worktree
  meta-tools that no single-target repair needs — and removed the model-specific
  sampling override from the bridge.

## The incumbent model: solve rate measured

Coming into the session the incumbent local model had only ever been run on **two**
bugs — both among the hardest in the corpus (a subtle garbage-collector use-after-free
and an uninitialized-value bug) — and both timed out. That was nearly generalized into
"this calibre of model is insufficient." It isn't supported by two hardest-case
timeouts, so we measured across the difficulty distribution instead.

- **Spread 1 (five tractable-class bugs):** **5/5 verified, all oracle-confirmed** — the
  recurring stack/heap families the learning loop targets.
- **Spread 2 (remaining unrun bugs, weighted toward the harder uninitialized-value and
  segfault classes):** in progress at time of writing; early results continue the
  pattern (tractable classes solve and oracle-confirm; the hardest memory-safety
  root-causes time out).

**Corrected finding.** A small model that fits the current VRAM budget solves routine,
recurring bug classes at a high, oracle-confirmed rate, and hits a **bounded correctness
ceiling on the subtlest memory-safety root causes** — not a blanket incapacity.

## Lessons worth carrying forward

- **Measure across the difficulty distribution before concluding capability.** Two
  hardest-case failures nearly produced a wrong, sweeping negative claim; a spread
  flipped it to a clean 5/5.
- **Raw benchmark score does not predict agentic harness fit.** The higher-scoring
  candidate could not drive the loop at all; the lower-scoring incumbent runs the table
  on routine bugs. Fit dominated.
- **Fix the substrate before blaming the model.** The check-patch self-poisoning and
  the forced-edit gate scoping were harness bugs masquerading as model failures; fixing
  them turned noisy plumbing failures into clean correctness signal.
