# ARVO 444773339 — mruby bigint heap-buffer-overflow

> Wrapped up with Claude usage sitting comfortably at **9%** after the run — plenty of
> headroom left in the tank for the next one.

- **Project:** mruby
- **Crash type:** Heap-buffer-overflow READ 4 (ASan)
- **Severity:** High
- **Fuzz target:** `mruby_fuzzer` (afl, ASan)
- **Crash site:** `mrbgems/mruby-bigint/core/bigint.c:356` in `uadd`
- **Constraint for this run:** diagnose and fix using *only* what is inside the
  `n132/arvo:444773339-vul` container — no internet, no peeking at the `-fix` image.

---

## How the bug presents

Running the stored PoC (`arvo`, a 97-byte fuzzer input doing big-integer `**`, `/`
and `+`) aborts with:

```
==7==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x...294 ... READ of size 4
    #0 uadd        bigint.c:356:23
    #1 mpz_add     bigint.c:614:5
    #2 mrb_bint_add_n bigint.c:3068:3
    #3 mrb_bint_add  bigint.c:3083:7
    ...
0x...294 is located 0 bytes after a 52-byte region [0x...260,0x...294)
allocated by thread T0 here:
    #1 mrb_realloc ...
    #4 mpz_init_heap bigint.c:114:12
    #5 div_limb      bigint.c:1363:3
    #6 udiv          bigint.c:1413:5
    #7 mpz_mdiv      bigint.c:1575:3
    #8 mrb_bint_div  bigint.c:3195:3
```

Two facts jump out:

1. The faulting **read** happens inside an *addition* (`uadd` ← `mpz_add`).
2. The over-read buffer (52 bytes = 13 limbs of 4 bytes) was **allocated during a
   previous division** (`div_limb`). So a value produced by `/` is later read out of
   bounds by `+`.

---

## Thought process (systematic debugging)

### Phase 1 — Root cause investigation

Read `uadd` (the function that actually faults):

```c
static void
uadd(mpz_t *z, mpz_t *x, mpz_t *y)
{
  mp_dbl_limb c = 0;
  size_t i;

  /* (A) overlap: read BOTH x and y, bounded by x->sz */
  for (i = 0; i + 4 <= x->sz; i += 4) {
    c += (mp_dbl_limb)y->p[i]   + (mp_dbl_limb)x->p[i];   z->p[i]   = LOW(c); c >>= DIG_SIZE;
    c += (mp_dbl_limb)y->p[i+1] + (mp_dbl_limb)x->p[i+1]; z->p[i+1] = LOW(c); c >>= DIG_SIZE; /* line 356 */
    ... i+2, i+3 ...
  }
  for (; i < x->sz; i++) { c += y->p[i] + x->p[i]; ... }   /* still reads BOTH, up to x->sz */

  /* (B) tail: read ONLY y, from x->sz up to y->sz */
  for (; i + 4 <= y->sz; i += 4) { c += y->p[i]; ... }
  for (; i < y->sz; i++)         { c += y->p[i]; ... }

  /* (C) final carry written at index y->sz */
  z->p[y->sz] = (mp_limb)c;
}
```

The structure encodes an **implicit precondition**: loop (A) reads `y->p[i]` for every
`i < x->sz`, loop (B) only extends `y`, and (C) writes `z->p[y->sz]`. All of this is
correct **iff `x->sz <= y->sz`** — i.e. `x` is the shorter operand and `y` the longer.
If `x` is *longer* than `y`, loop (A) reads `y->p[i]` for indices beyond `y`'s buffer.

That is exactly a READ past the end of `y`, matching the ASan report (the OOB address
is "0 bytes after" `y`'s 13-limb allocation).

### Phase 2 — Who violates the precondition?

Read the caller `mpz_add`:

```c
static void
mpz_add(mpz_ctx_t *ctx, mpz_t *zz, mpz_t *x, mpz_t *y)
{
  if (zero_p(x)) { ...; return; }
  if (zero_p(y)) { ...; return; }

  if (y->sz == 1 && x->sz > 1) { /* fast path, no uadd */ ...; return; }
  if (x->sz == 1 && y->sz > 1) { mpz_add(ctx, zz, y, x); return; } /* swap for sz==1 only */

  mpz_t z;
  size_t estimated_size = ((x->sz > y->sz) ? x->sz : y->sz) + 1;  /* result size IS max-aware */
  mpz_init_heap(ctx, &z, estimated_size);

  if (x->sn > 0 && y->sn > 0)      { uadd(&z, x, y); z.sn = 1; }   /* <-- raw arg order */
  else if (x->sn < 0 && y->sn < 0) { uadd(&z, x, y); z.sn = -1; }  /* <-- raw arg order */
  else { /* usub path for differing signs */ }
  ...
}
```

`mpz_add` calls `uadd(&z, x, y)` in **raw argument order**. The only size-ordering
logic in `mpz_add` handles the `sz == 1` fast paths. So when **both** operands have
`sz > 1` but `x->sz > y->sz` (e.g. `x` has 14 limbs, `y` the 13-limb division result),
neither fast path fires and `uadd` is entered with its precondition violated → OOB read.

Note the output buffer `z` is already sized with `max(x->sz, y->sz) + 1`, so the *write*
side is fine; only the *read* of the shorter operand overflows. Consistent with this
being a READ, not a WRITE.

### Phase 3 — Hypothesis

> `uadd` requires `x->sz <= y->sz`, and `mpz_add` does not guarantee it for the
> general (both multi-limb) case. Make the operand sizes ordered and the crash
> disappears while results stay correct.

Addition is commutative, so reordering operands is semantically free.

### Phase 4 — Fix + verification

Chosen fix: enforce the precondition **inside `uadd`** by swapping the operand
pointers when `x` is longer. This is minimal (3 effective lines), fixes the root
cause at the function level, and protects *every* caller of `uadd` (both same-sign
branches in `mpz_add`), not just the path the PoC happened to hit. The existing body
is already correct once `x->sz <= y->sz` holds.

Why not patch `mpz_add` instead? That would only fix the two call sites we can see and
leaves the latent footgun in `uadd` for any future caller. Fixing the contract where
it lives is the more robust choice.

---

## Verification (all inside the vulnerable container)

1. `arvo compile` after copying the patched `bigint.c` into `/src` → **exit 0**.
2. `arvo run` on the original PoC → **`Execution successful.`, exit 0, no ASan error**
   (previously: heap-buffer-overflow abort).
3. Correctness regression with the in-container `mruby` interpreter:
   - `a + b == b + a` for operands with different limb counts (exercises the swap),
   - exact-value equality against expected `Integer` results,
   - the division-then-add path (`(10**100)/7 + 10**80`) returns the correct value.

No crash **and** arithmetic still correct.

---

## The patch

See [`fix.patch`](./fix.patch). Summary:

```diff
--- a/mrbgems/mruby-bigint/core/bigint.c
+++ b/mrbgems/mruby-bigint/core/bigint.c
@@ -342,6 +342,13 @@
 static void
 uadd(mpz_t *z, mpz_t *x, mpz_t *y)
 {
+  /* The loops below read both operands up to x->sz, then read the tail of y
+     up to y->sz.  This is only correct when x is the shorter operand, so
+     ensure x->sz <= y->sz.  Addition is commutative, so swapping is safe. */
+  if (x->sz > y->sz) {
+    mpz_t *t = x; x = y; y = t;
+  }
+
   /* Core multi-limb addition with carry propagation */
   mp_dbl_limb c = 0;
   size_t i;
```
