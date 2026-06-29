from repair_loop import repair_with_retries, describe_feedback

BUG = {"localId": 449429295, "crash_type": "Global-buffer-overflow READ 1",
       "sanitizer": "asan", "fuzz_target": "mruby_fuzzer", "crash_output": "..."}


def make_agent(script):
    """script: list of diffs the agent emits on attempts 1..N. Records feedback seen."""
    seen = []

    def agent(attempt_no, feedback):
        seen.append(feedback)
        return {"diff": script[attempt_no - 1], "trajectory_summary": f"attempt {attempt_no}"}

    agent.seen = seen
    return agent


def make_verify(verdicts):
    """verdicts: dict diff -> classification."""
    def verify(bug_id, diff):
        cls = verdicts[diff]
        extra = {"make_test_tail": "1 test failed: ::FOO expected 42 got Object"} if cls == "fixed_tests_failed" else {}
        return {"classification": cls, **extra}
    return verify


def test_solves_after_one_failure_and_returns_contrastive_pair():
    agent = make_agent(["GUARD_DIFF", "ROOT_FIX_DIFF"])
    verify = make_verify({"GUARD_DIFF": "fixed_tests_failed", "ROOT_FIX_DIFF": "verified_correct"})
    result = repair_with_retries(bug=BUG, agent=agent, verify=verify, max_attempts=5)

    assert result["status"] == "solved"
    assert len(result["attempts"]) == 2
    rejected, accepted = result["contrastive_pair"]
    assert rejected["diff"] == "GUARD_DIFF" and accepted["diff"] == "ROOT_FIX_DIFF"
    # The 2nd attempt was given feedback about the failing test (deployment-faithful).
    assert "test suite now FAILS" in agent.seen[1]


def test_no_contrastive_pair_when_first_attempt_succeeds():
    agent = make_agent(["GOOD_DIFF"])
    verify = make_verify({"GOOD_DIFF": "verified_correct"})
    result = repair_with_retries(bug=BUG, agent=agent, verify=verify, max_attempts=5)
    assert result["status"] == "solved"
    assert result["contrastive_pair"] is None


def test_exhausts_attempts_without_solving():
    agent = make_agent(["A", "B", "C"])
    verify = make_verify({"A": "still_crashes", "B": "still_crashes", "C": "fixed_tests_failed"})
    result = repair_with_retries(bug=BUG, agent=agent, verify=verify, max_attempts=3)
    assert result["status"] == "exhausted"
    assert result["accepted"] is None
    assert len(result["attempts"]) == 3


def test_feedback_never_references_the_fix_image():
    fb = describe_feedback({"classification": "fixed_tests_failed", "make_test_tail": "x"})
    assert "-fix" not in fb and "ground-truth" not in fb.lower()
