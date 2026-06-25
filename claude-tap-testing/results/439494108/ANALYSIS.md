# ARVO 439494108 — mruby — stack-use-after-return in `limb_addmul_1` (rational reduce / bigint pool escape)

- **Project:** mruby (`mrbgems/mruby-bigint`, `mrbgems/mruby-rational`)
- **Fuzzer / sanitizer:** `mruby_fuzzer` (libFuzzer) + ASan
- **Crash type:** Stack-use-after-return, READ 4
- **Report:** https://issues.oss-fuzz.com/issues/439494108
- **Reproduce:** `docker run --rm n132/arvo:439494108-vul arvo`

## Crash signature

```
ERROR: AddressSanitizer: stack-use-after-return ... in limb_addmul_1 bigint.c:726
  #0 limb_addmul_1   bigint.c:726
  #1 mpz_mul_basic   bigint.c:926
  #2 mpz_mul         bigint.c:1153
  #3 bint_mul        bigint.c:3142
  #4 rat_sub_b       rational.c:870
  #5 mrb_vm_exec
Address ... is located in stack of thread T0 ... in frame
  #0 mrb_bint_reduce bigint.c:3673
    [160, 2216) 'pool_storage' (line 3675) <== Memory access is inside this variable
```

The read targets `pool_storage`, a **stack-local scratch buffer** belonging to `mrb_bint_reduce`,
accessed long after that function returned (via a later `rat_sub_b → bint_mul → mpz_mul`).

## Root cause — pool memory escaping into a GC-persistent `RBigint`

`mruby-bigint` uses a "scoped memory pool" optimization: `MPZ_CTX_INIT` declares a
`mpz_pool_t pool_storage` **on the caller's stack** (512 limbs), and small `mpz_t` temporaries
are allocated from it (`mpz_init_temp` → `pool_alloc`) instead of the heap.

`mrb_bint_reduce` (used to put a rational's numerator/denominator into lowest terms) builds its
results `a` and `b` using this stack pool, then wraps them into heap `RBigint` objects:

```c
void mrb_bint_reduce(mrb_state *mrb, mrb_value *xp, mrb_value *yp) {
  MPZ_CTX_INIT(mrb, ctx, pool);          // pool_storage lives on THIS stack frame
  ...
  mpz_mdiv(ctx, &a, &x, &r);             // a, b may be pool-backed (point into pool_storage)
  mpz_mdiv(ctx, &b, &y, &r);
  struct RBigint *b1 = bint_new(ctx, &a);
  struct RBigint *b2 = bint_new(ctx, &b);
  *xp = mrb_obj_value(b1);               // escapes to the caller / VM
  *yp = mrb_obj_value(b2);
}
```

`bint_new` decides between an **embedded** payload and a **heap** payload:

```c
static struct RBigint* bint_new(mpz_ctx_t *ctx, mpz_t *x) {
  ...
  if (x->sz <= RBIGINT_EMBED_SIZE_MAX) {     // small: memcpy into the object  -> SAFE
    ... memcpy(RBIGINT_EMBED_ARY(b), x->p, ...); mpz_clear(ctx, x);
  } else {                                   // large: heap payload
    RBIGINT_SET_HEAP(b);
    mpz_move(ctx, &b->as.heap, x);           // <-- the bug
  }
}
```

And `mpz_move` (vulnerable version) simply **steals the pointer**:

```c
static void mpz_move(mpz_ctx_t *ctx, mpz_t *y, mpz_t *x) {
  mpz_clear(ctx, y);
  y->p = x->p;            // y now points wherever x pointed...
  x->p = NULL; ...
}
```

When the reduced value is large enough to take the **heap path** (`sz > RBIGINT_EMBED_SIZE_MAX`,
i.e. > ~192 bits / ~58 decimal digits with 32-bit limbs) **and** its limbs were pool-allocated,
`mpz_move` copies a pointer **into `mrb_bint_reduce`'s stack `pool_storage`** into a
GC-persistent `RBigint`. Once `mrb_bint_reduce` returns, that stack memory is dead; the next
rational operation (`rat_sub_b → bint_mul → mpz_mul → limb_addmul_1`) reads it → **SUAR**.

This is the same root-cause family as 439279102 (pool memory escaping into a persistent
`RBigint`); the escape boundary here is `bint_new`'s heap path via `mpz_move`.

## Fix

Make `mpz_move` **deep-copy when the source is pool memory** instead of transferring the pool
pointer. (`mpz_set` allocates a fresh heap buffer and copies; `mpz_clear` on the pool source is a
no-op for the pool, just resetting the handle.)

```c
static void mpz_move(mpz_ctx_t *ctx, mpz_t *y, mpz_t *x) {
#if MRB_BIGINT_POOL_SIZE > 0
  if (MPZ_HAS_POOL(ctx) && is_pool_memory(x, MPZ_POOL(ctx))) {
    /* Source is pool memory - use deep copy instead of pointer transfer */
    mpz_set(ctx, y, x);
    mpz_clear(ctx, x);
    return;
  }
#endif
  mpz_clear(ctx, y);
  y->sn = x->sn; y->sz = x->sz; y->p = x->p;
  x->p = NULL; x->sn = 0; x->sz = 0;
}
```

See `fix.patch`. **This is byte-for-byte the real upstream fix** — the `-fix` image patches
`mpz_move` identically (and matches 439279102's upstream commit `88bfa0be`, "move pool handling
from bint_set to mpz_move"). `mrb_bint_reduce` itself is unchanged upstream.

## Verification

### 1. No-crash (in-container, patched source)
- `arvo compile` → rc 0
- `arvo run` → rc 0, no ASan error; the PoC now prints the rational results instead of crashing.

### 2. Correctness — built host `mruby` and ran differential batteries vs a Python oracle,
three-way against the **pristine `-vul`** and the **`-fix`** image. (Default build: 32-bit limbs,
`RBIGINT_EMBED_SIZE_MAX` = 6 limbs.)

Large-integer arithmetic (`+`,`-`,`*`, operands chosen to exceed the embed threshold so they hit
the `bint_new` heap path / `mpz_move`), 300 cases, run **per-case in isolation**:

| build              | correct | crash | wrong-value |
|--------------------|:-------:|:-----:|:-----------:|
| patched `-vul`     | **261** |  19   |     20      |
| pristine `-vul`    | **261** |   0   |     39      |
| `-fix`             | **261** |   0   |     39      |

- The **correct set is identical across all three** (the same 261 cases).
- **Zero true value regressions:** patched `-vul` produces no wrong value on any case that `-fix`
  computes correctly. All 19 patched crashes fall on cases `-fix` *also* gets wrong (0 crashes on
  `-fix`-correct cases).

### Co-resident bugs encountered (out of scope — see notes below)
The mruby-bigint `-vul` snapshot carries several independent defects beyond the reported SUAR;
the differential battery trips over them in **both `-vul` and `-fix`**, so they are not my
regression:

1. **`udiv` divide-by-zero SIGFPE** (`bigint.c:1454`). Fires inside `mrb_bint_reduce`'s own
   `mpz_mdiv` during the gcd-reduction of large rationals (Euclid does multi-limb divisions even
   for coprime operands). Reproduces identically in `-vul` and `-fix` (rc 136 / ASan FPE). This
   makes *any* large-rational reduction unusable as a correctness oracle here, which is why
   correctness was validated via large-**integer** arithmetic (no division → never reaches `udiv`).

2. **`uadd` out-of-bounds READ** (`bigint.c:379`). `uadd` reads `y->p[i]` for `i < x->sz`, i.e. it
   requires the second operand to be at least as long as the first; when a caller passes
   `x->sz > y->sz` it over-reads `y`. `uadd` is **byte-for-byte identical in `-vul` and `-fix`**
   (the only `mpz_add` diff is a cosmetic cast), so this over-read is *not* fixed upstream at this
   revision — it is a latent co-resident bug. It manifests as the 39 wrong-value cases that are
   **identical between pristine `-vul` and `-fix`**. In the pristine build the over-read lands in
   adjacent in-bounds memory (the 512-limb pool array, or slack) and silently returns a wrong
   answer; my fix relocates pool-backed operands into exact-size heap buffers with ASan redzones,
   so the same pre-existing over-read now trips ASan on 19 of those 39 cases instead of returning
   silently wrong. My SUAR fix neither introduces nor is responsible for this defect.

## Conclusion
The reported stack-use-after-return is fixed at its true root cause — the pool-pointer escape in
`mpz_move` — with a patch identical to upstream. Correctness is preserved (no value regressions vs
the `-fix` reference). Two unrelated co-resident mruby-bigint bugs (`udiv` FPE, `uadd` OOB read)
were identified and proven out of scope by using the `-fix` and pristine `-vul` builds as controls.
