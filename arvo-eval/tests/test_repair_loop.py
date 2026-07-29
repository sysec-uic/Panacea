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


def test_resume_continues_numbering_and_feedback_from_checkpoint():
    # Simulates a process killed after attempt 1 (e.g. a usage cap): the caller
    # already has attempt 1's record on disk and re-enters with it as resume_attempts.
    # The loop must NOT re-run attempt 1 -- it continues at attempt 2 using the
    # feedback that was computed for attempt 1's rejection.
    prior = {"attempt": 1, "diff": "GUARD_DIFF", "verdict": "fixed_tests_failed",
             "trajectory_summary": "t1", "feedback_for_next": "PRIOR FEEDBACK TEXT"}

    seen = []

    def agent(attempt_no, feedback):
        seen.append((attempt_no, feedback))
        return {"diff": "FIX_DIFF"}

    def verify(bug_id, diff):
        return {"classification": "verified_correct"}

    result = repair_with_retries(bug=BUG, agent=agent, verify=verify, max_attempts=5,
                                 resume_attempts=[prior], resume_feedback=prior["feedback_for_next"])

    assert seen == [(2, "PRIOR FEEDBACK TEXT")]     # numbering picked up at 2, not 1
    assert result["status"] == "solved"
    assert len(result["attempts"]) == 2             # the resumed attempt 1 + the new attempt 2
    assert result["attempts"][0] is prior
    # The contrastive pair still sees the resumed (pre-restart) rejected attempt.
    rejected, accepted = result["contrastive_pair"]
    assert rejected is prior and accepted["diff"] == "FIX_DIFF"


def test_on_attempt_hook_fires_for_every_attempt_including_resumed_run():
    recorded = []

    def agent(attempt_no, feedback):
        return {"diff": "A" if attempt_no == 1 else "B"}

    def verify(bug_id, diff):
        return {"classification": "still_crashes", "run_output_tail": "x"} if diff == "A" else \
               {"classification": "verified_correct"}

    result = repair_with_retries(bug=BUG, agent=agent, verify=verify, max_attempts=5,
                                 resume_attempts=[], on_attempt=lambda r: recorded.append(r["attempt"]))
    assert recorded == [1, 2]
    assert result["status"] == "solved"


def test_resuming_bug_that_hit_max_attempts_returns_exhausted_without_calling_agent():
    # A checkpoint with max_attempts records means a prior run used up the whole
    # budget before dying; resuming must not grant extra attempts.
    prior_attempts = [
        {"attempt": n, "diff": "X", "verdict": "still_crashes", "feedback_for_next": "fb"}
        for n in range(1, 4)
    ]
    calls = []

    def agent(attempt_no, feedback):
        calls.append(attempt_no)
        return {"diff": "X"}

    def verify(bug_id, diff):
        return {"classification": "still_crashes", "run_output_tail": "x"}

    result = repair_with_retries(bug=BUG, agent=agent, verify=verify, max_attempts=3,
                                 resume_attempts=prior_attempts)
    assert calls == []
    assert result["status"] == "exhausted"
    assert len(result["attempts"]) == 3


def test_usage_limit_interrupts_without_checkpointing_or_calling_verify():
    # An empty diff means the agent genuinely produced nothing before the cutoff --
    # there's nothing to salvage, so this stays a true interrupt.
    verify_calls = []
    on_attempt_calls = []

    def agent(attempt_no, feedback):
        return {"diff": "", "usage_limit": {"resets_at": 999, "resets_at_human": "9:50pm (UTC)"}}

    def verify(bug_id, diff):
        verify_calls.append(diff)
        return {"classification": "verified_correct"}

    result = repair_with_retries(bug=BUG, agent=agent, verify=verify, max_attempts=5,
                                 on_attempt=lambda r: on_attempt_calls.append(r))

    assert result["status"] == "interrupted"
    assert result["attempts"] == []                     # nothing checkpointed
    assert result["usage_limit"] == {"resets_at": 999, "resets_at_human": "9:50pm (UTC)"}
    assert verify_calls == []                            # never paid for a verify build
    assert on_attempt_calls == []


def test_usage_limit_with_real_diff_is_verified_not_discarded():
    # Regression: a usage-cap signal can arrive after the agent's own turn already
    # produced a genuine patch (the rate-limit event lands on a later call, not the
    # one that actually wrote the diff). Discarding it as "interrupted" throws away
    # real, verifiable work -- this bit us twice in practice (462331852, 473582282),
    # where a correct, oracle-confirmed fix was silently dropped and had to be
    # manually recovered from the orphaned patch.diff each time. A non-empty diff
    # must be treated as a real attempt even when usage_limit is also set.
    verify_calls = []
    on_attempt_calls = []

    def agent(attempt_no, feedback):
        return {"diff": "SOME_DIFF", "usage_limit": {"resets_at": 999}}

    def verify(bug_id, diff):
        verify_calls.append(diff)
        return {"classification": "verified_correct"}

    result = repair_with_retries(bug=BUG, agent=agent, verify=verify, max_attempts=5,
                                 on_attempt=lambda r: on_attempt_calls.append(r))

    assert result["status"] == "solved"
    assert verify_calls == ["SOME_DIFF"]                 # the diff WAS verified
    assert len(on_attempt_calls) == 1
    assert result["accepted"]["diff"] == "SOME_DIFF"


def test_usage_limit_after_a_real_prior_attempt_preserves_it():
    prior = {"attempt": 1, "diff": "GUARD_DIFF", "verdict": "still_crashes",
             "feedback_for_next": "keep going"}

    def agent(attempt_no, feedback):
        return {"diff": "", "usage_limit": {"resets_at": 1}}

    def verify(bug_id, diff):
        return {"classification": "verified_correct"}

    result = repair_with_retries(bug=BUG, agent=agent, verify=verify, max_attempts=5,
                                 resume_attempts=[prior], resume_feedback="keep going")
    assert result["status"] == "interrupted"
    assert result["attempts"] == [prior]                 # attempt 1's real work is untouched


def test_user_abort_interrupts_without_checkpointing():
    verify_calls = []
    on_attempt_calls = []

    def agent(attempt_no, feedback):
        return {"diff": "SOME_DIFF", "aborted": True}

    def verify(bug_id, diff):
        verify_calls.append(diff)
        return {"classification": "verified_correct"}

    result = repair_with_retries(bug=BUG, agent=agent, verify=verify, max_attempts=5,
                                 on_attempt=lambda r: on_attempt_calls.append(r))

    assert result["status"] == "interrupted"
    assert result["attempts"] == []
    assert result["aborted"] is True
    assert result["usage_limit"] is None      # distinguishes an abort from a usage cap
    assert verify_calls == []
    assert on_attempt_calls == []


def test_usage_limit_with_unchecked_diff_falls_through_to_check_patch_gate():
    # With a real diff present, usage_limit no longer force-interrupts -- it falls
    # through to the same verdict logic as any other attempt, including the
    # check-patch enforcement gate (an unchecked patch is rejected as "unchecked"
    # without paying for a verify build, same as when usage_limit isn't set at all).
    def agent(attempt_no, feedback):
        return {"diff": "X", "check_required": True, "check_passed": False,
                "usage_limit": {"resets_at": 1}}

    def verify(bug_id, diff):
        raise AssertionError("verify should never be called")

    result = repair_with_retries(bug=BUG, agent=agent, verify=verify, max_attempts=1)
    assert result["status"] == "exhausted"
    assert result["attempts"][0]["verdict"] == "unchecked"

    def agent_still_interrupts_with_no_diff(attempt_no, feedback):
        return {"diff": "", "check_required": True, "check_passed": False,
                "usage_limit": {"resets_at": 1}}

    result = repair_with_retries(bug=BUG, agent=agent_still_interrupts_with_no_diff,
                                 verify=verify, max_attempts=5)
    assert result["status"] == "interrupted"
