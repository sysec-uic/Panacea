"""Demo: -fix-free retry loop that learns from its own failed -> successful transition.

Uses bug 449429295's two real on-disk patches as a stand-in for two AGENT attempts:
  attempt 1 = guard the crash site   -> crash gone but `make test` fails (wrong result)
  attempt 2 = fix the emitter         -> crash gone AND `make test` passes  (accepted)

Nothing here reads the ARVO `-fix` image. "Accepted" is decided purely by the
deployment oracle (crash gone + make test). The loop feeds deployment-faithful feedback
between attempts, and on success distills a contrastive lesson from attempt 1 vs 2.

Run:
    PYTHONPATH=. python3 demo_retry_learn.py
With ANTHROPIC_API_KEY set it calls the model for real; otherwise a labelled stub is used.
"""
import json
import os
from pathlib import Path

from repair_loop import repair_with_retries
from contrastive_extract import extract_contrastive_heuristic

PAIR_DIR = Path(__file__).resolve().parent.parent / "bug-runs" / "results" / "449429295"

BUG = {
    "localId": 449429295,
    "crash_type": "Global-buffer-overflow READ 1",
    "sanitizer": "asan",
    "fuzz_target": "mruby_fuzzer",
    "crash_output": "ERROR: AddressSanitizer: global-buffer-overflow ... mrb_prev_pc codegen.c:692",
}

GUARD = (PAIR_DIR / "initial-guard-attempt-INADEQUATE.patch").read_text()   # agent attempt 1
ROOTFIX = (PAIR_DIR / "fix.patch").read_text()                              # agent attempt 2 (passes oracle)


def stub_agent(attempt_no, feedback):
    if attempt_no > 1:
        print(f"\n--- agent received feedback before attempt {attempt_no} ---\n{feedback}\n")
    diff = GUARD if attempt_no == 1 else ROOTFIX
    return {"diff": diff, "trajectory_summary": f"attempt {attempt_no}"}


def stub_verify(bug_id, diff):
    # Deployment oracle: crash gone + make test. Attempt 1 silences the crash but the
    # suite catches the wrong ::FOO result; attempt 2 passes.
    if diff == GUARD:
        return {"classification": "fixed_tests_failed",
                "make_test_tail": "FAIL test/t/syntax.rb: `::FOO` expected 42, got Object"}
    return {"classification": "verified_correct", "make_test_ok": True}


def _stub_llm(prompt, system=""):
    return json.dumps({
        "trigger": "ASan buffer-overflow READ inside a bytecode/iseq scanner (mrb_insn_size[] indexing)",
        "wrong_approach": "[STUB] First attempt guarded the reader (mrb_prev_pc) so the bad index bails to NULL; "
                          "crash gone but make test failed — ::FOO still miscompiled.",
        "correct_approach": "[STUB] Passing attempt fixed the emitter (codegen_colon3): genop_1(OP_OCLASS)+OP_GETMCNST "
                            "so no stray byte is written.",
        "lesson": "[STUB] An OOB READ over a bytecode table is usually corrupt bytecode, not a missing bounds check. "
                  "If make test fails after guarding the reader, the writer is the real bug.",
        "how_to_apply": "[STUB] When an OOB read indexes an opcode/size table, trace to the genop_* that emitted it "
                        "and fix the emission; don't clamp the reader.",
        "tags": ["asan", "buffer-overflow-read", "codegen", "iseq-corruption", "fix-writer-not-reader"],
        "confidence": "high",
    })


def main():
    print("=" * 78)
    print("RETRY LOOP (deployment oracle: crash gone + make test, no -fix)")
    print("=" * 78)
    result = repair_with_retries(bug=BUG, agent=stub_agent, verify=stub_verify, max_attempts=5)

    print(f"status: {result['status']}")
    for a in result["attempts"]:
        print(f"  attempt {a['attempt']}: verdict = {a['verdict']}")

    pair = result["contrastive_pair"]
    if not pair:
        print("\nNo failed->success transition; nothing to learn contrastively.")
        return

    rejected, accepted = pair
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    print("\n" + "=" * 78)
    print(f"LEARN FROM OWN ATTEMPTS  (rejected attempt {rejected['attempt']} vs accepted attempt {accepted['attempt']})")
    print(f"LLM: {'REAL claude-opus-4-8' if has_key else 'STUB (no ANTHROPIC_API_KEY) — labelled output'}")
    print("=" * 78)

    kw = dict(bug=BUG, rejected_diff=rejected["diff"], accepted_diff=accepted["diff"],
              rejected_verdict=rejected["verdict"])
    heuristic = extract_contrastive_heuristic(**kw) if has_key else extract_contrastive_heuristic(**kw, llm=_stub_llm)
    print("CONTRASTIVE LESSON (added to the playbook for future bugs):\n")
    print(json.dumps(heuristic, indent=2))


if __name__ == "__main__":
    main()
