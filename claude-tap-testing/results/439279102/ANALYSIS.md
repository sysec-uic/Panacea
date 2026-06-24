# ARVO Bug 439279102 — From-Scratch Analysis & Fix

**Project:** mruby · **Fuzz target:** `mruby_fuzzer` · **Engine/Sanitizer:** libFuzzer / ASan
**Crash type:** stack-use-after-return, READ of size 4 · **SCARINESS:** 55
**Faulting frame:** `mrb_bint_as_float` @ `mrbgems/mruby-bigint/core/bigint.c:2949`
**Images:** `n132/arvo:439279102-vul` / `-fix`
**Real upstream fix commit:** `88bfa0be` — *"mruby-bigint: move pool handling from bint_set to mpz_move"*

> Analyzed from the `-vul` container only (no peeking at the fix until the comparison
> step). This is my own workflow run; the folder already contained an independent
> `oss_crs_*` analysis from the OSS-CRS agent, which I did not rely on.

---

## 1. Reproduce

```
==ERROR: AddressSanitizer: stack-use-after-return  READ of size 4
SCARINESS: 55 (4-byte-read-stack-use-after-return)
    #0 mrb_bint_as_float   mrbgems/mruby-bigint/core/bigint.c:2949
    #1 rat_float           mrbgems/mruby-rational/src/rational.c:388
    #2 mrb_rational_to_f   mrbgems/mruby-rational/src/rational.c:397
    #3 flo_add             src/numeric.c:501
    #4 mrb_vm_exec         src/vm.c
Address ... is located in stack of thread T0 in frame
    #0 mrb_bint_reduce     mrbgems/mruby-bigint/core/bigint.c:3673
  This frame has object 'pool_storage' <== access at offset 208 is inside this variable
```

The PoC is a small Ruby program that mixes big rational literals (`1r`, `2r`, `5r`),
bignums, and a float, ultimately doing `float + Rational` where the rational has a
multi-limb numerator/denominator.

## 2. Root cause

mruby-bigint uses a **scoped scratch pool** for temporaries. `MPZ_CTX_INIT` declares
`mpz_pool_t pool_storage` **on the caller's stack frame**; `pool_alloc` hands out limb
arrays from it, and `is_pool_memory()` tests whether an `mpz_t`'s limbs live inside it.

`mrb_bint_reduce` (bigint.c:3672) reduces a rational's `num`/`den`. Its quotients `a`, `b`
can be backed by this stack pool, then it wraps them into persistent GC objects:

```c
struct RBigint *b1 = bint_new(ctx, &a);   /* numerator  */
struct RBigint *b2 = bint_new(ctx, &b);   /* denominator */
```

In `bint_new` (bigint.c:2789) there are two branches:

```c
if (x->sz <= RBIGINT_EMBED_SIZE_MAX) {       /* <= 6 limbs: SAFE */
  memcpy(RBIGINT_EMBED_ARY(b), x->p, ...);   /* copies limbs into the object */
}
else {                                       /* > 6 limbs (> 192-bit): BUG */
  RBIGINT_SET_HEAP(b);
  mpz_move(ctx, &b->as.heap, x);             /* only copies the *pointer* */
}
```

`mpz_move` does `y->p = x->p` — a pure pointer transfer. When `x->p` points into the
stack pool, the heap `RBigint` ends up **aliasing `mrb_bint_reduce`'s stack frame**.
Once `mrb_bint_reduce` returns, that frame is reclaimed. The later
`flo_add → mrb_rational_to_f → rat_float → mrb_bint_as_float` reads the limbs through the
dangling pointer → **stack-use-after-return** (offset 208 inside `pool_storage`).

The crash *site* (`mrb_bint_as_float`) is just the reader. The **root cause is the escape
boundary**: `bint_new`'s heap branch baking a transient pool pointer into a persistent
GC object. (Embedded bignums ≤ 6 limbs are safe because that branch memcpys.)

## 3. The fix

Heap-promote at the escape boundary: in `bint_new`'s heap branch, if `x`'s limbs are
pool-backed, copy them into freshly `mrb_malloc`'d heap storage instead of aliasing the
pool. Guarded by `#if MRB_BIGINT_POOL_SIZE > 0` so it compiles out when the pool is
disabled. See `fix.patch`.

This is the surgical, root-cause fix at the exact point where pool memory would otherwise
outlive its scope. The **real upstream fix** (`88bfa0be`) instead puts the same
pool→heap deep copy inside `mpz_move` itself:

```c
mpz_move(...) {
#if MRB_BIGINT_POOL_SIZE > 0
  if (MPZ_HAS_POOL(ctx) && is_pool_memory(x, MPZ_POOL(ctx))) {
    mpz_set(ctx, y, x);   /* deep copy */
    mpz_clear(ctx, x);
    return;
  }
#endif
  ...
}
```

Both target the identical root cause. Upstream's is broader (every `mpz_move` of pool
memory deep-copies, even internal same-scope transfers that didn't strictly need it);
mine is narrower (only the `bint_new` persistence boundary, which is the only place a
pool pointer can actually escape the `MPZ_CTX` scope into a GC object). The independent
`oss_crs` agent also chose `mpz_move`, matching upstream.

## 4. Verification

| Step | Result |
|------|--------|
| Unpatched baseline (`arvo`) | ❌ stack-use-after-return in `mrb_bint_as_float` (SCARINESS 55) |
| `arvo compile` (patched) | ✅ rc 0 |
| `arvo run` (patched) | ✅ "Executed /tmp/poc", rc 0, 0 sanitizer errors |
| PoC via host `mruby` (patched-vul) | ✅ exit 0 — identical to `-fix` image |

**Correctness, not just "no crash".** Built host `mruby` (`rake`) from the patched `-vul`
source and the `-fix` source, and ran a differential test against a Python oracle on
`Rational(p**a, q**b).to_f` (multi-limb num/den — the exact crash path):

| Input | patched-vul `to_f` | `-fix` `to_f` | Python oracle |
|-------|--------------------|---------------|---------------|
| `Rational(2**300, 3**200)`  | `7.66915923726601e-06` | `7.66915923726601e-06` | `7.66915923726601e-06` |
| `Rational(3**250, 2**260)`  | `1.02923561840864e+41` | `1.02923561840864e+41` | `1.02923561840864e+41` |

My patched build produces values **bit-identical to the upstream `-fix` image and the
oracle** on the SUAR path. The fix only relocates the limb buffer (copy vs. alias), so it
cannot perturb arithmetic — confirmed.

## 5. Out-of-scope pre-existing bugs found in this snapshot

This `-vul` bigint.c is a heavily-refactored snapshot (custom stack pool + Knuth `udiv`
rewrite) and carries **other, unrelated bugs** that are NOT ARVO 439279102 and are NOT
introduced by my patch:

- **`udiv` integer FPE** (`bigint.c:1446`, divisor top-limb `z == 0`): SIGFPE during
  `mpz_gcd`/`mpz_mdiv` for many big-rational reductions, e.g. `Rational(5**130, 7**110)`.
  **Reproduces identically in the upstream `-fix` image** (rc 136) — so it is a separate,
  still-unfixed defect, out of scope here.
- **Wrong reduction** in the `-vul` snapshot:
  `Rational(123456789012345678901234567890, 987654321098765432109876543210)` →
  `109739369/109739369` (wrong numerator). The `-fix` image returns the correct
  `13717421/109739369`; upstream fixed this separately via the `mpz_mul` carry-propagation
  change (a different commit from the SUAR fix), so my SUAR-only patch does not address it.

These confirm the SUAR fix is correctly scoped: it matches exactly what upstream commit
`88bfa0be` touches, and the residual differences vs. `-fix` are entirely from other,
unrelated upstream commits.

## 6. Reproduction commands

```bash
docker run --rm n132/arvo:439279102-vul arvo                 # reproduce SUAR
# patch mrbgems/mruby-bigint/core/bigint.c per fix.patch
docker exec <c> bash -lc 'arvo compile && arvo run'          # verify no crash
docker exec <c> bash -lc 'cd /src/mruby && CC=clang CXX=clang++ LD=clang rake -j4'  # host build
```
