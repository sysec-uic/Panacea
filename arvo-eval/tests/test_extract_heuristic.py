import json
from extract_heuristic import extract_heuristic

VALID = json.dumps({
    "trigger": "Stack-use-after-return in bigint pool path",
    "root_cause_lesson": "mpz values built in a stack pool escape via bint_new heap path",
    "how_to_apply": "pool-aware mpz_move / deep-copy before the value escapes the frame",
    "tags": ["suar", "bigint-pool"],
    "confidence": "high",
})


def test_extract_parses_structured_heuristic():
    h = extract_heuristic(
        bug={"localId": 439494108, "crash_type": "Stack-use-after-return READ 4",
             "sanitizer": "asan", "fuzz_target": "mruby_fuzzer", "crash_output": "..."},
        diff="--- a/x\n+++ b/x\n",
        trajectory_summary="agent traced the escape",
        verdict="verified_correct",
        llm=lambda prompt, system="": VALID,
    )
    assert h["trigger"].startswith("Stack-use-after-return")
    assert "suar" in h["tags"]


def test_extract_tolerates_fenced_json():
    h = extract_heuristic(
        bug={"localId": 1, "crash_type": "x", "sanitizer": "asan",
             "fuzz_target": "f", "crash_output": ""},
        diff="d", trajectory_summary="", verdict="verified_correct",
        llm=lambda prompt, system="": f"```json\n{VALID}\n```",
    )
    assert h["confidence"] == "high"
