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


def test_feedback_for_harness_patch_redirects_to_project_code():
    fb = describe_feedback({"classification": "patch_touches_harness"})
    assert "harness" in fb.lower()
    # It must steer the agent to the project source, not just say "rejected".
    assert "project" in fb.lower()


def test_feedback_never_references_the_fix_image():
    fb = describe_feedback({"classification": "fixed_tests_failed", "make_test_tail": "x"})
    assert "-fix" not in fb and "ground-truth" not in fb.lower()


def test_feedback_for_timeout_tells_agent_to_commit_to_a_fix():
    fb = describe_feedback({"classification": "timed_out"})
    assert "time" in fb.lower()
    # It must push the agent to stop exploring and actually submit a patch.
    assert "patch" in fb.lower()


def test_feedback_unchecked_directs_agent_to_check_patch():
    fb = describe_feedback({"classification": "unchecked"})
    assert "check-patch" in fb and "PASS" in fb


def test_unchecked_submission_rejected_without_verify_then_solved_when_checked():
    # Enforcement: a submission with check_required but no check_passed is rejected as
    # "unchecked" WITHOUT calling verify; the agent is told to run check-patch. Once it
    # submits a checked patch, verify runs and it can be accepted.
    seen = []
    verified = []

    def agent(n, feedback):
        seen.append(feedback)
        passed = n >= 2
        return {"diff": f"PATCH{n}", "trajectory_summary": "",
                "check_required": True, "check_passed": passed}

    def verify(bug_id, diff):
        verified.append(diff)
        return {"classification": "verified_correct"}

    result = repair_with_retries(bug=BUG, agent=agent, verify=verify, max_attempts=5)
    assert result["attempts"][0]["verdict"] == "unchecked"
    assert verified == ["PATCH2"]          # verify never ran on the unchecked attempt 1
    assert result["status"] == "solved"
    assert "check-patch" in seen[1]


def test_checked_submission_is_verified_normally():
    def agent(n, feedback):
        return {"diff": "P", "check_required": True, "check_passed": True}

    def verify(bug_id, diff):
        return {"classification": "still_crashes", "run_output_tail": "boom"}

    result = repair_with_retries(bug=BUG, agent=agent, verify=verify, max_attempts=1)
    assert result["attempts"][0]["verdict"] == "still_crashes"   # gate passed, verify ran


def test_no_check_gate_when_not_required_is_backcompat():
    # Flag off (no check_required in the run dict): submissions verify as before.
    def agent(n, feedback):
        return {"diff": "P"}

    def verify(bug_id, diff):
        return {"classification": "verified_correct"}

    result = repair_with_retries(bug=BUG, agent=agent, verify=verify, max_attempts=1)
    assert result["attempts"][0]["verdict"] == "verified_correct"


def test_timed_out_no_patch_attempt_is_classified_and_fed_back():
    # A run that hit the wall-clock cap returns no diff but flags timed_out. The loop
    # must classify it as timed_out (not a generic no_changes) and feed a real
    # message forward so the next attempt is steered, not left guessing.
    seen = []

    def agent(attempt_no, feedback):
        seen.append(feedback)
        if attempt_no == 1:
            return {"diff": "", "trajectory_summary": "ran out of time", "timed_out": True}
        return {"diff": "GOOD", "trajectory_summary": "fixed"}

    def verify(bug_id, diff):
        return {"classification": "verified_correct"}

    result = repair_with_retries(bug=BUG, agent=agent, verify=verify, max_attempts=5)
    assert result["attempts"][0]["verdict"] == "timed_out"
    assert result["status"] == "solved"
    # Attempt 2 was told, in words, that attempt 1 ran out of time.
    assert "time" in seen[1].lower()
