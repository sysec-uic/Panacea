# ARVO 439291659 — mruby bigint: bad free of stack (pool) memory during GC

- **Project:** mruby (`mrbgems/mruby-bigint`)
- **Sanitizer / engine:** MemorySanitizer / libFuzzer (`mruby_fuzzer`)
- **Crash type:** bad pointer passed to `free()` — MSan `CHECK failed:
  sanitizer_allocator_secondary.h:177 "IsAligned(...)"`. Under glibc (non-MSan
  host build) the same defect surfaces as `munmap_chunk(): invalid pointer`.
- **DEDUP_TOKEN:** `CheckUnwind()--CheckFailed--GetMetaData`

## Crash signature

```
#5  free
#6  mrb_basic_alloc_func              src/allocf.c:30
#7  mpz_clear                         mrbgems/mruby-bigint/core/bigint.c:299
#8  mrb_gc_free_bint                  mrbgems/mruby-bigint/core/bigint.c:2888
#9  obj_free                          src/gc.c:885
#10 incremental_sweep_phase           src/gc.c:1062
...
#14 mrb_obj_alloc                     src/gc.c:502      <- GC triggered here
#15 bint_new                          mrbgems/mruby-bigint/core/bigint.c:2792
#18 mrb_as_bint / mrb_bint_new_int
#19 bint_mul                          mrbgems/mruby-bigint/core/bigint.c:3136
```

The freed pointer is a **stack address** (`0x7ffe…`). GC, running mid-allocation
inside `bint_new`, sweeps a *previously created* `RBigint` whose limb pointer
`as.heap.p` points into a stack region — and `free()`s it.

## Root cause

This build of `bigint.c` is a modified version that adds a **scratch-limb
pool**. `MPZ_CTX_INIT` (bigint.c:53) declares that pool **on the stack** of the
calling function:

```c
#define MPZ_CTX_INIT(mrb_ptr, ctx, pool_ptr) \
  mpz_pool_t pool ## _storage = {0};                       /* <-- on the stack */ \
  mpz_pool_t *pool_ptr = &pool ## _storage; \
  mpz_ctx_t  ctx ## _struct = ((mpz_ctx_t){.mrb=(mrb_ptr), .pool=(pool_ptr)}); \
  mpz_ctx_t *ctx = &(ctx ## _struct);
```

Many operations build temporaries from this pool via `mpz_init_temp` →
`pool_alloc` (e.g. `udiv`, `mpz_div_2exp`, `mpz_mmod`, `mpz_mdivmod`, Barrett
reduction). Some then `mpz_move()` such a temporary into their *result* `z`, so
`z->p` ends up pointing into the **stack pool**.

`bint_new()` (bigint.c:2789) is the single boundary where a transient `mpz_t`
becomes a persistent, GC-managed `RBigint`. Its heap branch was:

```c
else {
  RBIGINT_SET_HEAP(b);
  mpz_move(ctx, &b->as.heap, x);   /* aliases x->p directly into the object */
}
```

`mpz_move` does `y->p = x->p` — **no copy**. So when `x->p` is pool memory, the
long-lived object is left owning a pointer into a stack frame that disappears as
soon as the producing function returns. A later GC sweep calls
`mrb_gc_free_bint → mpz_clear → mrb_free` on that dangling stack pointer.

Why the existing pool guard does not save it: `mpz_clear` skips `free()` when
`is_pool_memory(s, ctx->pool)` is true — but `mrb_gc_free_bint` builds a *fresh,
empty* stack pool for its own `ctx`. A pointer from a different, already-dead
pool is never recognised, so it is freed. The free-time check is structurally
incapable of catching escaped pool pointers.

(The embedded branch of `bint_new` is already safe: it `memcpy`s the limbs into
the object and `mpz_clear`s the source.)

## Fix

Close the escape at the persist chokepoint. In `bint_new`'s heap branch, if the
source limbs live in the pool, copy them onto the heap instead of aliasing:

```c
else {
  RBIGINT_SET_HEAP(b);
#if MRB_BIGINT_POOL_SIZE > 0
  if (MPZ_HAS_POOL(ctx) && is_pool_memory(x, MPZ_POOL(ctx))) {
    mpz_t h;
    mpz_init_heap(ctx, &h, x->sz);
    if (x->p) memcpy(h.p, x->p, x->sz*sizeof(mp_limb));
    h.sn = x->sn;
    b->as.heap = h;
    mpz_clear(ctx, x);          /* pool-aware: leaves pool memory untouched */
  }
  else
#endif
  mpz_move(ctx, &b->as.heap, x);
}
```

This is **value-preserving**: it stores the exact same `x->sz` limbs, sign and
size that `mpz_move` would have aliased — only on durable heap memory. Every
persistent `RBigint` is created through `bint_new`, so this single point covers
all escape paths. See `fix.patch`.

## Verification

All work done with the `n132/arvo:439291659-{vul,fix}` containers.

1. **PoC.** `arvo compile && arvo run` on the patched `-vul` image →
   `Execution successful.`, exit 0, no MSan error (was: MSan bad-pointer
   `CHECK failed`).

2. **Differential test vs. a Python big-int oracle** (host `mruby` rebuilt from
   source). On 400 random large division/modulo expressions — the operations
   that route pool temporaries through `bint_new`:

   | build | crashes | wrong (no crash) | correct |
   |-------|--------:|-----------------:|--------:|
   | original `-vul`       | **74** | 199 | 127 |
   | patched (this fix)    | **0**  | 251 | 149 |
   | pool disabled (`MRB_BIGINT_POOL_SIZE 0`) | **0** | 251 | 149 |

   - The fix eliminates **every** crash and never introduces one.
   - It is exactly equivalent to disabling the pool entirely (0 crashes both),
     confirming the pool was the *sole* source of the bad free.
   - Spot check: `-887…064 / 606086676229` → original prints
     `munmap_chunk(): invalid pointer`; patched returns the oracle-correct
     quotient with no error.

3. **No regression.** Patched output equals original output on every case
   except those poisoned by the unrelated bugs below (which are
   non-deterministic). Where the fix's copy branch runs, results are correct.

## Out of scope — pre-existing bugs in this modified `bigint.c`

These are present in the **unmodified `-vul` source** (verified on a pristine
build and with the pool disabled) and are **not** introduced by this fix. They
are separate from the assigned bad-free crash:

- **`udiv` magnitude error.** Large-operand `/` and `%` return wrong values for
  ~60% of random large cases. Deterministic and identical across original,
  patched and pool-disabled builds (e.g. a 90-digit / 50-digit division returns
  `-206882854553506992627` vs. the correct `-864567625629022855513`). A logic
  bug in the modified division, independent of memory management.
- **Uninitialised-memory reads** in large addition / Karatsuba multiply: MSan
  `use-of-uninitialized-value` in `mpz_get_str`, yielding non-deterministic
  wrong results (original and patched disagree run-to-run). A missing
  limb-initialisation in the modified arithmetic, again unrelated to the free.

Small/medium arithmetic (including negative floor-division semantics) is
correct.

## Comparison with the real upstream fix (`-fix` image)

The `-fix` image is a broad refactor of the same pool-based `bigint.c` and does
**not** crash on the PoC. Notably it does *not* add a pool check to the persist
path — `bint_new` was refactored into a `bint_set` helper whose heap branch
still does `mpz_move`, and `mpz_clear` / `mrb_gc_free_bint` are essentially
unchanged. The upstream approach instead prevents pool memory from ever reaching
a persisted result at the *source* operations. This fix achieves the same
guarantee more locally, at the one boundary (`bint_new`) every persistent bigint
must pass through.
