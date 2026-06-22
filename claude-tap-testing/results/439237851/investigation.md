# ARVO Bug 439237851 — Investigation & Fix

**Project:** mruby · **Fuzz target:** `mruby_proto_fuzzer` · **Engine/Sanitizer:** libFuzzer / ASan
**Crash type:** Stack-use-after-return, READ of size 4 · **Severity:** SCARINESS 55
**Image:** `n132/arvo:439237851-vul` · **Faulting frame:** `uzero_p` @ `mrbgems/mruby-bigint/core/bigint.c:505`
**Report:** https://issues.oss-fuzz.com/issues/439237851

> Constraint for this exercise: the `-vul` image is treated as the latest version that exists.
> The upstream fix commit / patch URL were **not** consulted; the fix below was derived
> independently from the crash and the source.

---

## 1. Reproduce

Inside the container, `arvo` runs `/out/mruby_proto_fuzzer /tmp/poc` (ASan build):

```
==7==ERROR: AddressSanitizer: stack-use-after-return on address 0x... READ of size 4
SCARINESS: 55 (4-byte-read-stack-use-after-return)
    #0 uzero_p        mrbgems/mruby-bigint/core/bigint.c:505:9
    #1 mrb_bint_mod   mrbgems/mruby-bigint/core/bigint.c:3260:21
    #2 mrb_vm_exec    src/vm.c
    ...
Address ... is located in stack of thread T0 at offset 120 in frame
    #0 mrb_bint_div   mrbgems/mruby-bigint/core/bigint.c:3172
  This frame has 2 object(s):
    [96, 2152) 'pool_storage' (line 3193) <== Memory access is inside this variable
SUMMARY: AddressSanitizer: stack-use-after-return .../bigint.c:505:9 in uzero_p
```

The read happens in `mrb_bint_mod`, but the memory being read belongs to the
**already-returned** stack frame of `mrb_bint_div` — specifically its local
`pool_storage` buffer.

The triggering Ruby program (decoded from the proto PoC) boils down to a
**bignum division feeding a bignum modulo**:

```ruby
... (1 / (1 % (var_2 / var_1))) ...
```

## 2. Root-cause analysis

1. **The trace gives the thread to pull.** A 4-byte read in `uzero_p` of memory
   that lives in `mrb_bint_div`'s returned stack frame, inside a variable named
   `pool_storage`. That name is the lead.

2. **What is `pool_storage`?** It is created by the `MPZ_CTX_INIT` macro
   (bigint.c:~51): each bignum operation declares an on-stack `mpz_pool_t
   pool_storage` — a scratch arena of `mp_limb data[512]`. Temporary `mpz_t`
   values can be backed by this pool (via `mpz_init_temp` → `pool_alloc`) to avoid
   heap traffic. Crucially, **the pool lives only for the duration of the function
   that declared it.**

3. **`uzero_p` is innocent.** It just walks `x->p[i]`. The bug is that the `mpz_t
   b` it was handed already had `b->p` pointing into dead stack memory. So the real
   question: how did a persistent bignum object end up with a pointer into a stack
   pool?

4. **Follow the operand lifetime.** In `mrb_bint_mod`, the operand comes from
   `bint_as_mpz(RBIGINT(y), &b)` — `b->p` is just the limb pointer stored inside
   the persistent `RBigint` object `y`. If that pointer is into a stack pool, then
   `y` was *created* holding a stack pointer. `y` here is the result of the earlier
   `var_2 / var_1` division.

5. **Inspect the create-from-temporary boundary: `bint_new` (bigint.c:2790).**
   This is where a transient `mpz_t` becomes a GC-managed `RBigint`. Two paths:
   - **embed** (`sz <= RBIGINT_EMBED_SIZE_MAX`): `memcpy`s the limbs into the
     object — **safe**, value is copied regardless of source.
   - **heap** (large): `mpz_move(&b->as.heap, x)` — `mpz_move` only transfers the
     **pointer** `x->p` into the object (no copy). If `x->p` is pool/stack memory,
     the persistent object now aliases the stack. **This is the bug.**

   Both `mrb_bint_div` and `mrb_bint_mod` end with `bint_new(ctx, &z)` where `z` is
   computed through pool-backed temporaries; for a large-enough result the heap
   path runs and the division result `y` escapes holding a pointer into
   `mrb_bint_div`'s `pool_storage`. When `div` returns, that memory is gone; the
   following `mod` reads it → stack-use-after-return.

## 3. The fix

The pool aliasing done by `mpz_move` is *intended* and correct *within* a single
operation (temporaries are moved between `mpz_t`s while the pool is still alive).
Changing `mpz_move` to always copy would defeat the pool and is the wrong layer.
The only place a value must stop aliasing the pool is the **escape boundary** —
`bint_new` — which is exactly where the embed path already copies. So make the
heap path copy too, *but only when the source is pool memory* (using the existing
`is_pool_memory()` helper). See `patch.diff`.

```c
  else {
    RBIGINT_SET_HEAP(b);
#if MRB_BIGINT_POOL_SIZE > 0
    if (MPZ_HAS_POOL(ctx) && is_pool_memory(x, MPZ_POOL(ctx))) {
      /* x->p lives in the (stack) memory pool, which is reclaimed when the
         current operation returns.  This RBigint outlives that pool, so we
         must copy the limbs onto the heap rather than aliasing pool memory
         (which would leave the object dangling -> stack-use-after-return). */
      mpz_init(ctx, &b->as.heap);
      mpz_set(ctx, &b->as.heap, x);
      mpz_clear(ctx, x);
    }
    else
#endif
    {
      mpz_move(ctx, &b->as.heap, x);
    }
  }
```

Why this is safe:
- `mpz_init` zeroes the destination, then `mpz_set` → `mpz_realloc` allocates fresh
  **heap** limbs (destination has `p == NULL`, so it is never treated as pool
  memory) and copies the value.
- `mpz_clear(x)` then releases the source; since `x` is pool memory it is simply
  marked unused (not freed), matching the pool contract.
- The guard compiles out entirely when pools are disabled
  (`MRB_BIGINT_POOL_SIZE == 0`), preserving the original behavior.

## 4. Verification

Run inside the ARVO container:

| Step | Result |
|------|--------|
| Unpatched baseline (`arvo`) | ❌ `AddressSanitizer: stack-use-after-return ... in uzero_p` (SCARINESS 55) |
| `arvo compile` with patch | ✅ exit 0 |
| Patched PoC run (`arvo`) | ✅ exit 0, **0** sanitizer errors, PoC runs to completion |

```sh
docker pull n132/arvo:439237851-vul
docker run -d --name arvo439 --network none n132/arvo:439237851-vul sleep infinity
docker exec arvo439 bash -c 'arvo'                     # baseline: crashes
# apply patch.diff to /src/mruby/mrbgems/mruby-bigint/core/bigint.c, then:
docker exec arvo439 bash -c 'arvo compile && arvo'     # patched: exit 0, clean
```
