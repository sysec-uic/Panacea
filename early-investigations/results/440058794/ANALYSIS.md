# ARVO Bug 440058794 — From-Scratch Analysis & Fix

**Project:** mruby · **Fuzz target:** `mruby_fuzzer` · **Engine/Sanitizer:** libFuzzer / **MSan**
**Crash type:** use-of-uninitialized-value (heap UAF, surfaced by MSan as poisoned/freed read)
**Faulting frame:** `mrb_obj_hash_code` @ `src/hash.c:332` (the `switch (tt)` on `mrb_type(key)` read at `hash.c:329`)
**Images:** `n132/arvo:440058794-vul` / `-fix`
**Report:** https://issues.oss-fuzz.com/issues/440058794

> Analyzed from the `-vul` container only, then compared against `-fix`. This is a GC
> object-lifetime bug in the custom C `mruby-set` gem — a **missing write barrier**, a
> different (and later) defect than the khash-rebuild GC-orphan bug in 439645304, even
> though both crash on the same `mrb_obj_hash_code`-during-rehash stack.

---

## 1. Reproduce

`docker run --rm n132/arvo:440058794-vul arvo` reproduces deterministically:

```
==WARNING: MemorySanitizer: use-of-uninitialized-value
    #0 mrb_obj_hash_code   src/hash.c:332            <-- switch (mrb_type(key))
    #1 kset_hash_value     mrbgems/mruby-set/src/set.c:26
    #2 kh__key_idx_set_val ...set.c:35
    #3 kh_put_set_val      ...set.c:35
    #4 kh__rebuild_set_val ...set.c:35                <-- rehash during table resize
    #5 kh_resize_set_val   ...set.c:35
    #6 kh_put_set_val      ...set.c:35
    #7 set_add             ...set.c:291
    #8 mrb_vm_exec
  Uninitialized value was stored to memory at
    #0 mrb_type            include/mruby/boxing_word.h:229
    #1 mrb_obj_hash_code   src/hash.c:329
  Uninitialized value was created by a heap deallocation
    #0 free
    #1 mrb_free            src/gc.c:259
    #2 incremental_sweep_phase  src/gc.c:1089         <-- GC freed the object
    ...
    #14 mrb_obj_hash_code  src/hash.c:361             <-- the #hash funcall that ran the GC
```

The stored `/tmp/poc` is a ~8 KB fuzzed Ruby program that builds `Set` literals and adds
many elements (`|= Set[...]`, `<<`, `%w[...]`, custom objects). The harness
(`oss-fuzz/mruby_fuzzer.c`) just runs the bytes via `mrb_load`. Full trace in `crash_trace.txt`.

**Note on this `mruby-set`:** unlike upstream mruby (pure-Ruby `Set`), this snapshot ships a
**khash-based C `mruby-set`** (`set.c` + a custom `include/mruby/khash.h`). `struct RSet`
embeds the `kset_t` header directly (`MRB_OBJECT_HEADER; kset_t set;`).

---

## 2. Root cause — missing GC write barrier on Set insertion

mruby uses a **tri-color incremental GC** with a write barrier: when you store a white
(unmarked) object into a black (already-fully-scanned) container, you must call a write
barrier to re-gray the container, otherwise the GC never visits the new child and **sweeps
it while it is still referenced**.

In the `-vul` `set.c`, **none of the `kset_put` insertion sites call a write barrier.**
So a heap key (String, or a user object) inserted into an already-black `Set` is left white
and gets freed on the next sweep, while the Set's khash table still holds the pointer.

The crash then surfaces on the next operation that touches that stale pointer. In the PoC
that operation is the **khash rebuild** triggered by a later `set_add`:

```
set_add → kh_put → (table full) → kh_resize → kh__rebuild
  for each existing key: kh_put(&new_table, old_keys[i])
     → kset_hash_value → mrb_obj_hash_code
         default branch (hash.c:361): mrb_funcall(key, :hash)   <-- runs Ruby, allocates
            → mrb_incremental_gc → incremental_sweep_phase → frees an unbarriered key
  ... next loop iteration hashes a key whose object was just swept
     → mrb_type(old_keys[j]) reads freed memory → MSan use-of-uninitialized-value
```

So the `kh__rebuild` itself is *not* the bug here (this snapshot already builds the resized
table in a temporary header and only publishes it after rehashing — that is the GC-safe
rebuild from bug **439645304**). The defect is one level up: the keys were already doomed
because they were added without a write barrier.

This is precisely the **co-resident write-barrier bug** I flagged as out-of-scope in
439645304 (it was present even in *that* bug's `-fix`). By this later snapshot it is the
*primary* reported bug, and upstream fixed it here.

---

## 3. Fix

Add `mrb_field_write_barrier_value(mrb, <RSet>, key)` immediately after **every** `kset_put`
that stores a key into a long-lived `RSet`. Because `set.c` passes around the embedded
`kset_t*`, a helper recovers the owning object:

```c
#define kset_to_rset(kset) ((struct RBasic*)((char*)(kset) - offsetof(struct RSet, set)))
```

Barriered sites: `kset_copy_merge`, `set_add`, `set_add_p`, intersection (`&`), difference
(`-`)/symmetric-difference (`^`) result builds, the `Set[...]`/create loop, the flatten
merge of non-Set elements, and `merge`. `kset_copy_merge` had to be **moved below**
`struct RSet` + the `kset_to_rset` macro (it previously sat before them), or it fails to
compile (`undefined reference to kset_to_rset`).

See `fix.patch`. This is exactly the **write-barrier subset of the official `-fix`**.

---

## 4. Verify

- **No-crash (MSan, authoritative):** patched `set.c` → `arvo compile` (exit 0) →
  `arvo` runs `/tmp/poc` cleanly: `Executed /tmp/poc in 61 ms`, exit 0, **no MSan error**.
  Pristine `-vul` on the same PoC = the UAF above. This is the decisive differential.
- **Correctness / no regression:** built host `mruby` from the patched tree and ran a
  GC-stress Set test (`set_correctness.rb`: 2000 string keys, 1000 custom `#hash` objects,
  union/intersection/difference, `Set[...]` literal, all with interleaved `GC.start` +
  garbage churn). Result **ALL PASS**. The official `-fix` host build gives the **same
  ALL PASS**.
- **Honest caveat:** `set_correctness.rb` does *not* by itself reproduce the UAF on the
  pristine `-vul` — neither in a plain host build (freed slot isn't reused before re-read,
  so the dangling read is silent) nor even under the MSan fuzzer (full `GC.start`
  collections don't land in the same incremental-GC-mid-rebuild window the fuzzed PoC hits).
  The authoritative vul-vs-fix signal is therefore the **PoC under MSan**, not this script;
  the script's role is to confirm the barrier doesn't change Set semantics.

---

## 5. Compare against the real `-fix`

`diff -u` of `-vul` → `-fix` for `set.c`, `khash.h`, `hash.c`:

- **`set.c` (the actual fix):** adds `kset_to_rset` + `mrb_field_write_barrier_value` after
  every insertion — **identical in intent and placement to my patch.** The `-fix` also
  bundles several **unrelated** changes that are *not* needed for this crash:
  - `kh_is_end(h,i) := (i) >= kh_end(h)` / `kset_is_end` and a `KHASH_FOREACH` update — a
    separate iterator-bounds correctness fix for the small-table case (where `kh_end`
    returns `h->size`, not `n_buckets`, so `iter != kh_end` is wrong).
  - `GOLDEN_RATIO_PRIME` macro (cosmetic), a `set_init` "already initialized" guard, a
    `replace`/`flatten` rework to be GC-safe via `mrb_obj_new` instead of raw `kset_init`,
    and a `Set#join` separator cleanup.
- **`hash.c`:** defensive `hash_code = 0` / `eql = FALSE` initializers and
  `MRB_RECURSIVE_BINARY_FUNC_P` — hardening, not the root cause.
- **`khash.h`:** only the `kh_is_end` macro addition (the iterator-bounds fix above).

My patch is the **minimal root-cause fix**: the write barriers. The rest of `-fix` is
co-bundled unrelated maintenance.

---

## 6. Files

- `fix.patch` — the write-barrier fix for `mrbgems/mruby-set/src/set.c`
- `crash_trace.txt` — MSan trace from `n132/arvo:440058794-vul arvo`
- `set_correctness.rb` — GC-stress Set correctness/regression test
