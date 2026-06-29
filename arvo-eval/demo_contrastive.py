"""Demo: contrastive heuristic extraction on a real ARVO (wrong-attempt, gold-fix) pair.

Bug 449429295 is the ideal case: there is a recorded INADEQUATE agent attempt (it
guarded the crash *site* in mrb_prev_pc) and the correct -fix (it corrected the
instruction emission in codegen_colon3). Different file, different function — exactly
the gap contrastive learning should turn into a lesson.

Run:
    PYTHONPATH=. python3 demo_contrastive.py

If ANTHROPIC_API_KEY is set it calls claude-opus-4-8 for real; otherwise it uses a
clearly-labelled stub so you can see the data flow without a key.
"""
import json
import os
from pathlib import Path

from contrastive_extract import build_contrastive_prompt, extract_contrastive_heuristic

PAIR_DIR = Path(__file__).resolve().parent.parent / "bug-runs" / "results" / "449429295"

BUG = {
    "localId": 449429295,
    "crash_type": "Global-buffer-overflow READ 1",
    "sanitizer": "asan",
    "fuzz_target": "mruby_fuzzer",
    "crash_output": "ERROR: AddressSanitizer: global-buffer-overflow ... "
                    "mrb_prev_pc codegen.c:692 (READ of size 1 in mrb_insn_size[])",
}


def _stub_llm(prompt: str, system: str = "") -> str:
    """Stand-in for the model when no API key is present. Hand-written to match THIS
    bug so the demo shows a believable end-to-end result; a real model generates this
    from the prompt above. Clearly a stub."""
    return json.dumps({
        "trigger": "ASan buffer-overflow READ inside a bytecode/iseq scanner (e.g. mrb_insn_size[] indexing)",
        "wrong_approach": "[STUB] Hardened the reader: added bounds guards in mrb_prev_pc so the out-of-range "
                          "index returns NULL. Silences ASan but the iseq is still corrupt and mis-executes.",
        "correct_approach": "[STUB] Fixed the WRITER: codegen_colon3 emitted OP_OCLASS with a stray operand "
                            "(genop_2); change to genop_1(OP_OCLASS)+genop_2(OP_GETMCNST) so no junk byte is written.",
        "lesson": "[STUB] A buffer-overflow READ over a bytecode table usually means corrupted bytecode, not a "
                  "missing bounds check. Guarding the reader hides the corruption (and can add a runtime crash).",
        "how_to_apply": "[STUB] When an OOB READ indexes an instruction-size/opcode table, trace back to where those "
                        "bytes were emitted (codegen/genop_*). Fix the malformed emission; don't clamp the reader.",
        "tags": ["asan", "buffer-overflow-read", "codegen", "iseq-corruption", "fix-writer-not-reader"],
        "confidence": "high",
    })


def main():
    agent_diff = (PAIR_DIR / "initial-guard-attempt-INADEQUATE.patch").read_text()
    gold_diff = (PAIR_DIR / "fix.patch").read_text()

    print("=" * 78)
    print("PROMPT THE MODEL RECEIVES (assembled from the real wrong-attempt + gold-fix)")
    print("=" * 78)
    print(build_contrastive_prompt(BUG, agent_diff, gold_diff, verdict="still_crashes_or_wrong"))

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    llm = None if has_key else _stub_llm  # None -> contrastive_extract uses real call_llm
    print("\n" + "=" * 78)
    print(f"LLM: {'REAL claude-opus-4-8' if has_key else 'STUB (no ANTHROPIC_API_KEY) — labelled output'}")
    print("=" * 78)

    kwargs = dict(bug=BUG, agent_diff=agent_diff, gold_diff=gold_diff, verdict="still_crashes_or_wrong")
    heuristic = extract_contrastive_heuristic(**kwargs) if has_key else extract_contrastive_heuristic(**kwargs, llm=llm)

    print("CONTRASTIVE HEURISTIC (this is what gets added to the playbook):\n")
    print(json.dumps(heuristic, indent=2))


if __name__ == "__main__":
    main()
