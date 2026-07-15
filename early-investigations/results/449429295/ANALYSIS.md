# ARVO 449429295 — mruby `::Name` codegen buffer-overflow

- **Project:** mruby
- **Crash type:** Global-buffer-overflow READ 1 (ASan)
- **Severity:** High
- **Fuzz target:** `mruby_fuzzer` (afl, ASan)
- **Reported crash site:** `mrbgems/mruby-compiler/core/codegen.c:692` in `mrb_prev_pc`
- **Real root cause:** `codegen.c:5276` in `codegen_colon3` (malformed `OP_OCLASS` emission)

> **Honest summary:** my first from-scratch fix was *inadequate*. It silenced the
> reported crash by hardening the peephole scanner, but it did **not** fix the root
> cause — `::Name` constant access stayed miscompiled and gained a new runtime VM
> crash. The correct fix is in `codegen_colon3`. Both the wrong attempt and the
> correct fix are documented below; `fix.patch` is the correct one.

---

## How the bug presents

`docker run --rm n132/arvo:449429295-vul arvo` aborts during **compilation** of the
PoC:

```
==7==ERROR: AddressSanitizer: global-buffer-overflow ... READ of size 1
    #0 mrb_prev_pc      codegen.c:692:12      (i += mrb_insn_size[i[0]])
    #1 gen_addsub       codegen.c:1419
    ... codegen_call / codegen recursion ...
0x...1ec is located 15 bytes after global variable '.str.9' ... / 20 bytes before '.str.10'
SUMMARY: global-buffer-overflow codegen.c:692:12 in mrb_prev_pc
```

`mrb_insn_size[]` is a 106-entry global table indexed by opcode; the OOB index means
`i[0]` was **not a valid opcode**. The PoC is full of `::U` expressions.

---

## Investigation (systematic debugging + instrumentation)

### Phase 1 — what is `mrb_prev_pc` doing
`gen_addsub`'s peephole path calls `mrb_prev_pc(s, data.addr)` to find the instruction
before the last one. `mrb_prev_pc` **linearly decodes the iseq from offset 0**, stepping
by `mrb_insn_size[opcode]`, until it reaches `pc`. The OOB read means that linear scan
decoded a byte that is not a valid opcode → the iseq is not cleanly decodable.

### Phase 2 — gather evidence (instrumentation)
Instrumented `mrb_prev_pc` to print `pc/lastpc/curpc` and to dump the decode trail.
Findings at the crashing call:

```
[PREVPC] pc=1371 lastpc=1371 curpc=1374 icapa=2048     # lastpc < curpc, NOT stale
[TRAIL]  ... off=490 op=16(LOADSYM) sz=3
         off=493 op=91(OCLASS) sz=2
         off=495 op=91(OCLASS) sz=2
         off=497 op=8(LOADI_2) sz=2
         off=499 op=250  <-- BAD OPCODE, OOB read of mrb_insn_size[250]
```

The decode was self-consistent from 0 until it drifted at offset **499** — far below
the target (1371). So it was **not** a "stale lastpc beyond pc" problem (my initial
hypothesis); the iseq genuinely contains bytes that don't form a clean instruction
stream, right around the `OCLASS` instructions.

### Phase 3 — trace the corruption to its source (the step I skipped the first time)
`OP_OCLASS` is declared `OPCODE(OCLASS, B)` — a **1-operand (B-format)** instruction
(opcode + 1 byte = 2 bytes). But `codegen_colon3` emitted it as:

```c
genop_2(s, OP_OCLASS, cursp(), sym);   // writes opcode + TWO operand bytes
```

`genop_2` writes an extra operand byte that `OP_OCLASS` does not have. So **every
`::Name` writes one stray byte into the iseq**, which is not part of any instruction.
That stray byte is exactly what makes the linear decode in `mrb_prev_pc` drift onto an
invalid opcode → the reported global-buffer-overflow. (It is also semantically wrong:
`OP_OCLASS` ignores the symbol, so `::Name` was being miscompiled.)

---

## Two fixes

### ❌ Initial attempt (INADEQUATE) — guard the scanner
`initial-guard-attempt-INADEQUATE.patch`: make `mrb_prev_pc` bail out (return `NULL`)
when it hits an out-of-range opcode, instead of indexing `mrb_insn_size[]` OOB. Callers
treat `NULL` as "no previous instruction" and skip the (optional) peephole, so this
**does** stop the reported compiler crash on the PoC.

**But it only masks the symptom.** The malformed bytecode from `codegen_colon3` is still
emitted, so with this fix:
- `puts ::FOO` (FOO=42) prints **`Object`** — wrong result.
- More involved `::Name` use triggers a **new heap-buffer-overflow at runtime** in
  `mrb_vm_exec` (`src/vm.c:1700`), because the VM reads the stray operand byte as an
  opcode and runs off the rails.

Fixing the crash *site* (the scanner) instead of the *cause* (the emitter) is the
classic systematic-debugging trap. The evidence (OCLASS at the drift point) pointed at
the cause; I should have traced it instead of stopping at "iseq not decodable".

### ✅ Correct fix — emit `OP_OCLASS` properly (`fix.patch`)
```c
-  genop_2(s, OP_OCLASS, cursp(), sym);
+  genop_1(s, OP_OCLASS, cursp());            /* load ::Object (1-operand opcode) */
+  genop_2(s, OP_GETMCNST, cursp(), sym);     /* fetch the named constant from it */
```
This removes the stray byte (no more iseq corruption → no scanner OOB) **and** makes
`::Name` actually mean "look up constant `Name` in `::Object`". This matches the real
upstream fix exactly.

---

## Verification (in-container)

Built both the fuzzer (`arvo compile`) and the host `mruby` (`rake`) for each variant.

| Build | PoC (`arvo run`) | `::FOO` (=42) | `M::X`, `::String.name`, `::FOO+1` |
|---|---|---|---|
| vulnerable | global-buffer-overflow (compile time) | — | — |
| initial guard (mine, ❌) | exit 0 (no crash) | **`Object` (wrong)** + VM heap-overflow | crashes |
| **root-cause fix (`fix.patch`, ✅)** | **exit 0, "Execution successful."** | **42** | **7 / String / 43 — all correct** |
| real upstream `-fix` image | exit 0 | 42 | all correct |

The correct fix and the real upstream fix are identical in approach and both pass the
PoC and the `::Name` correctness checks.

---

## Lesson
Reproducing "no crash on the PoC" is **not** sufficient. The fuzzer caught a *compile-time*
symptom of a *bytecode-corruption* bug; a guard at the scanner hid that symptom while
leaving the corruption (and a runtime crash + wrong results) in place. Always trace a
corrupted-data bug back to whoever *wrote* the bad data, and verify real-program
correctness — not just the absence of the original crash.
