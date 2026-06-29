import json
from contrastive_extract import extract_contrastive_heuristic, build_contrastive_prompt

VALID = json.dumps({
    "trigger": "OOB read indexing a bytecode size table",
    "wrong_approach": "guarded the reader",
    "correct_approach": "fixed the emitter that wrote the bad byte",
    "lesson": "an OOB read over bytecode usually means corrupt bytecode, not a missing check",
    "how_to_apply": "trace back to the genop_* that emitted the byte",
    "tags": ["asan", "codegen"],
    "confidence": "high",
})

BUG = {"localId": 449429295, "crash_type": "Global-buffer-overflow READ 1",
       "sanitizer": "asan", "fuzz_target": "mruby_fuzzer", "crash_output": "..."}


def test_prompt_includes_both_attempts():
    p = build_contrastive_prompt(BUG, rejected_diff="REJECTED_X", accepted_diff="ACCEPTED_Y",
                                 rejected_verdict="fixed_tests_failed")
    assert "REJECTED_X" in p and "ACCEPTED_Y" in p


def test_prompt_does_not_mention_fix_image():
    p = build_contrastive_prompt(BUG, rejected_diff="a", accepted_diff="b",
                                 rejected_verdict="fixed_tests_failed")
    assert "-fix" not in p


def test_extract_returns_contrastive_schema():
    h = extract_contrastive_heuristic(
        bug=BUG, rejected_diff="a", accepted_diff="b", rejected_verdict="fixed_tests_failed",
        llm=lambda prompt, system="": VALID,
    )
    assert h["kind"] == "contrastive"
    assert h["wrong_approach"] and h["correct_approach"]
    assert "asan" in h["tags"]
