# OSS-CRS vs. Claude investigation — comparison (ARVO 444773339)

Comparison of two independent analyses of the same bug (mruby bigint heap-buffer-overflow,
`Heap-buffer-overflow READ 4` in `uadd` @ `mrbgems/mruby-bigint/core/bigint.c:356`):

- **Claude (from-scratch):** `claude-tap-testing/results/444773339/ANALYSIS.md` + `fix.patch`
- **OSS-CRS (`crs-claude-code`):** `claude-tap-testing/results/444773339/oss_crs_investigation.md` + `oss_crs_patch.diff`

## Bottom line

Both reached the **identical fix** — swap the operands inside `uadd` so the shorter one is `x`
(`x->sz <= y->sz`) — with the same justification (the read loops require it; addition is
commutative so swapping is free). The patches are byte-for-byte equivalent (`x->sz > y->sz`
vs. `y->sz < x->sz`). The difference is entirely in the *thought process* and *verification
rigor*, not the answer.

## Where they agree

- Same root cause: `uadd`'s implicit precondition `x->sz <= y->sz`, violated by `mpz_add`
  calling it in raw argument order.
- Same fix location: the contract belongs in `uadd`, so every caller is protected — not at
  the call site.
- Same correctness argument: addition is commutative → the swap is semantically free.

## Where the thought processes diverge

| Dimension | Claude (ANALYSIS.md) | OSS-CRS (oss_crs_investigation.md) |
|---|---|---|
| **Structure** | Explicit phases: root-cause → *who violates it* → hypothesis → fix + verify | Linear template: Reproduce → Root-cause → Fix → Verify → Stats |
| **Caller analysis** | Walks **every branch** of `mpz_add` (zero checks, both `sz==1` fast paths, `estimated_size`) to *prove* exactly which path reaches `uadd` with the precondition broken | Asserts "calls `uadd` without ordering operands" — correct, but does not enumerate why the fast paths don't save it |
| **READ vs WRITE** | Reconciles with the ASan evidence: `z` is sized `max+1`, so the **write is in-bounds** — only the read overflows, consistent with "READ of size 4" | Claims the carry write to `z->p[y->sz]` "is also out-of-bounds" — **incorrect**, and contradicts its own quoted "READ of size 4" |
| **Alternative fixes** | Explicitly weighs `uadd` vs. `mpz_add` and rejects the call-site fix with reasoning | No alternatives considered |
| **History claims** | Avoids claims about upstream (constraint: no peeking at `-fix`) | Asserts "upstream guarded this; the 4×-unrolled refactor dropped the swap" — an **unverified narrative**, since it states the fix was never consulted |
| **Verification** | **Oracle-based differential testing**: random cases vs. Python, `a+b==b+a`, exact-value equality, the division-then-add path | "test suite passes" + "both orderings sane"; the POV sidecar **timed out**, forcing a fallback to the fuzzer binary. No oracle correctness check |
| **Run cost** | n/a (manual) | 670 s, 93 agent turns, 4 POV runs, 2 patch builds |

## The consequential gap: verification rigor

The whole lesson of this bug (see `memory/mruby-444773339-real-fix-is-buggy.md`) is that the
upstream fix **stopped the crash but returned wrong sums**. OSS-CRS's "no crash + test suite
green" is exactly the check that would *not* have caught that. It got the right answer here
without the rigor that protects against the wrong one. Claude's oracle differential test is
the only step that distinguishes a correct fix from a plausible-but-wrong one.

## How OSS-CRS can cut tokens *without* losing depth

Reasoning depth and token cost are largely orthogonal — the depth that mattered (caller
enumeration, READ/WRITE reconciliation, alternative-fix weighing) is a few hundred output
tokens plus two targeted reads. The 93 turns / 670 s went to environment friction and
exploration, not thinking.

**Cut the waste (where the tokens actually are):**
1. Use the ASan trace as the search index — the faulting frame + allocation stack name the
   exact functions to read. Two function reads, no file sweeps.
2. Read once, hypothesize once, confirm once. Avoid iterative re-reads and trial-patching.
3. Build the verification harness one time; don't retry-loop on build/POV/timeouts.

**Add back the depth (nearly free in tokens):**
4. Caller-branch enumeration — read one already-loaded function + a sentence of reasoning.
5. Reconcile the fix against the crash classifier (a READ overflow isn't fixed by changing
   only a write) — a built-in sanity check that would have caught OSS-CRS's wrong write claim.
6. Always run an oracle/differential correctness test — the single highest-value addition.
7. Don't narrate unverified history unless a provided diff shows it.

Net: tighten the loop to reclaim turns, then reinvest a small fraction into enumeration +
the oracle test — fewer tokens **and** higher rigor than the original run.

> These improvements were encoded as prompt edits in the `crs-claude-code` agent
> (`~/crs-claude-code`, branch `improve-investigation-efficiency`): a new "Investigation
> Discipline" section in `agents/claude_code.md`, plus changes to `workflow_pov.md`,
> `pov_present.md`, and `pre_submit.md`.
