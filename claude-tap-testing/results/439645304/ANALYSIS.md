# ARVO Bug 439645304 — From-Scratch Analysis & Fix

**Project:** mruby · **Fuzz target:** `mruby_fuzzer` · **Engine/Sanitizer:** libFuzzer / ASan
**Crash type:** heap-use-after-free, READ of size 4 · **SCARINESS:** 45
**Faulting frame:** `mrb_type` @ `include/mruby/boxing_word.h:235` (via `mrb_obj_hash_code` @ `src/hash.c:329`)
**Images:** `n132/arvo:439645304-vul` / `-fix`
**Real upstream fix commit:** `d42326ce8` — *"khash.h: make khash rebuild GC-safe"*
(followed by an unrelated perf refactor `ac3c160c3` *"refactor rebuild to handle linear tables"*)

> Analyzed from the `-vul` container only, then compared against `-fix`. This bug is a GC
> reentrancy / object-lifetime bug, not arithmetic — a different class than the bigint bugs
> in this results set.

---

## 1. Reproduce

```
==ERROR: AddressSanitizer: heap-use-after-free  READ of size 4
SCARINESS: 45 (4-byte-read-heap-use-after-free)
    #0 mrb_type            include/mruby/boxing_word.h:235
    #1 mrb_obj_hash_code   src/hash.c:329
    #2 kset_hash_value     mrbgems/mruby-set/src/set.c:26
    #3 kh__key_idx_set_val ...set.c:35
    #4 kh_put_set_val      ...set.c:35
    #5 kh__rebuild_set_val ...set.c:35      <-- rehash during table resize
    #6 kh_resize_set_val   ...set.c:35
    #7 kh_put_set_val      ...set.c:35
    #8 set_add             ...set.c:291
    #9 mrb_vm_exec
freed by: incremental_sweep_phase  (src/gc.c:1089)  <-- GC freed the object
```

`docker run --rm n132/arvo:439645304-vul arvo` reproduces it deterministically.

Note: the stored `/tmp/poc` is a 7288-byte fuzzed Ruby program that creates a `Set`,
adds objects, and uses blocks/procs (`.map.with_index`, `||= Set[...]`). The fuzzer
harness (`oss-fuzz/mruby_fuzzer.c`) simply runs the bytes via `mrb_load_string`.

**Note on this `mruby-set`:** unlike upstream mruby (where `Set` is pure Ruby), this
snapshot ships a **custom C khash-based** `mruby-set` (`KHASH_DECLARE(set_val, mrb_value, char, FALSE)`).
The bug lives in the shared `include/mruby/khash.h` rebuild path.

## 2. Root cause

`mrb_obj_hash_code` (`src/hash.c`) hashes a Set key. For object keys that are not
immediates/strings/numbers it hits the **default branch** (`hash.c:361`) and calls the
Ruby `hash` method via `mrb_funcall` — i.e. **arbitrary Ruby runs while computing a key's
hash**. That Ruby allocates and can trigger the incremental GC.

The defect is in `kh__rebuild_##name` (khash.h). The original code:

```c
void *old_data = h->data;
khkey_t *old_keys = kh_keys_##name(h);   /* points into old_data */
...
h->n_buckets = new_n_buckets;
h->size = 0;
kh__alloc_##name(mrb, h);                /* h->data REPOINTED to the new, empty table */
for (i ...) kh_put_##name(mrb, h, old_keys[i], NULL);  /* hash() -> Ruby -> GC */
mrb_free(mrb, old_data);
```

`h` is the `kset_t` embedded in the live `RSet` GC object. The moment `kh__alloc` repoints
`h->data` to the **new, still-empty** table, the not-yet-migrated keys exist *only* in the
C local `old_keys`/`old_data`, which the GC cannot see. When `kh_put` runs a key's Ruby
`hash` and triggers GC, `mrb_gc_mark_set` walks the **new (partial)** table and never marks
the pending keys → `incremental_sweep_phase` frees them → the rehash loop then hashes a
freed key → `mrb_type` reads freed memory. **UAF.**

A **14-line deterministic reproducer** (`repro_minimal.rb`) — a `Set` of ≥5 objects whose
`hash` allocates heavily — produces the **identical** stack and DEDUP_TOKEN
(`mrb_type--mrb_obj_hash_code--kset_hash_value`).

## 3. Fix (`fix.patch`)

Keep `*h` describing the **complete old table** for the entire rehash; build the resized
table in a temporary header and publish it only after every key is migrated:

```c
kh_##name##_t nh = *h;
nh.n_buckets = new_n_buckets; nh.size = 0;
kh__alloc_##name(mrb, &nh);                 /* allocate into the temp header only */
... for (i ...) kh_put_##name(mrb, &nh, old_keys[i], NULL); ...  /* *h still old */
void *old_data = h->data;
*h = nh;                                     /* atomic publish */
mrb_free(mrb, old_data);
```

Now any GC during the rehash marks all keys through the still-intact `*h`. This is
**type-agnostic** (fixes every khash instantiation, not just Set) and addresses the **root
cause** (the GC-visibility invariant), not the crash site.

## 4. Verification

| input | pristine `-vul` | **my-fix** | official `-fix` |
|---|---|---|---|
| **real PoC** (`/tmp/poc`) | UAF crash | **clean (19 ms)** | clean |
| minimal repro | UAF crash | (see §5) | (see §5) |
| `REBUILD_LOGIC_OK` (GC-disabled / immediate keys; 2000 strings + 5000 ints, ~13 rebuilds, deletes, regrows) | — | **PASS** | PASS |

- The **reported bug is fixed** — the real PoC runs cleanly under ASan.
- My fix is the **same approach** as the upstream GC-safe commit `d42326ce8` (temp header +
  swap). `set.c` is **byte-identical** between `-vul` and `-fix`; the entire official fix is
  in `khash.h`.
- **Rebuild migration is correct and equivalent to upstream** (`REBUILD_LOGIC_OK` on both),
  so the temp-header swap loses/duplicates no keys.

## 5. Co-resident bugs (out of scope — present in official `-fix` too)

Per the multi-bug nature of these `-vul` snapshots, two *separate* GC bugs surfaced; both
reproduce on the official `-fix`, so neither is my regression nor the reported bug:

1. **Missing write barrier in `set_add`.** A benign test that adds many *strings* to a Set
   under the *incremental* GC silently loses members (heap keys added to an
   already-blackened `RSet` are swept). Result is **identical on pristine `-vul` and my-fix
   (214 lost)** and similar on `-fix` (257). Immediate keys (ints) and an explicit full
   `GC.start` are unaffected — classic missing `mrb_field_write_barrier` on key insertion.
2. **GC reentrancy in the hash/eql callbacks during `kh_put` probing.** An over-aggressive
   repro that also allocates in `hash`/`eql?` still UAFs on the official `-fix`
   (`kset_equal_value → mrb_eql → mrb_class`). The rebuild fix (ours and upstream's) does not
   cover GC triggered *inside* the per-probe callbacks.

Both are the same family as the reported UAF (Set members not protected across GC during a
user callback) but are distinct code paths the accepted upstream fix does not address.

## 6. Cleanup

Containers `arvo439`, `arvo439fix`, `arvo439orig` removed after analysis.

**Artifacts:** `fix.patch`, `repro_minimal.rb`, `test_rebuild_logic.rb`,
`test_set_correctness.rb`.
