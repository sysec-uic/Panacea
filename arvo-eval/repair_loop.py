"""Bounded retry loop with deployment-faithful feedback, plus own-attempt learning.

For one bug, attempt a repair up to `max_attempts` times. After each rejected attempt
the agent is re-run with feedback describing WHY it was rejected — but only feedback a
real developer would have on a fresh bug: the crash trace and the failing `make test`
output. Nothing here reads the ARVO `-fix` image; the system must keep working on bugs
that have no known fix.

If the agent eventually succeeds after one or more failures, the last rejected attempt
and the accepted attempt form a contrastive pair we can learn from (see contrastive_extract).

Collaborators are injected so this is testable without Docker:
  agent(attempt_no, feedback) -> {"diff": str, "trajectory_summary": str}
  verify(bug_id, diff)        -> {"classification": str, ...}   # e.g. arvo-eval verify_fix
"""


def describe_feedback(verification: dict) -> str:
    """Deployment-faithful feedback for the next attempt. Crash + test output only."""
    cls = verification.get("classification", "")
    if cls == "still_crashes":
        tail = verification.get("run_output_tail", "")
        return ("Your patch did not stop the crash. The target still crashes on the PoC:\n"
                f"{tail}\nFix the underlying defect, not just this symptom.")
    if cls == "fixed_tests_failed":
        tail = verification.get("make_test_tail", "")
        return ("Your patch stopped the crash, but the project's own test suite now FAILS, "
                "so the behavior is wrong:\n"
                f"{tail}\nYou likely silenced the symptom while leaving the real bug (or broke "
                "valid behavior). Trace the bad data back to where it was produced.")
    if cls == "patch_touches_harness":
        return ("Your patch modifies the fuzz harness, which is test scaffolding -- it "
                "cannot be changed in deployment, so this can never be the fix. The defect "
                "is in the project's own source code. Use the crash trace to find the "
                "project code that misbehaves and fix it there.")
    if cls == "unchecked":
        return ("You submitted a patch without validating it. You have a `check-patch` tool: "
                "run it from your source tree and iterate on your fix until it prints PASS, "
                "then submit that patch. Do not submit again until check-patch passes.")
    if cls == "timed_out":
        return ("Your previous attempt ran out of time before submitting a patch. Stop "
                "exploring: use the crash trace to locate the faulting project source, make "
                "the smallest correct change that fixes the root cause, and write the patch "
                "out now. Do not re-read files you have already seen.")
    if cls == "build_failed":
        return "Your patch did not compile. Fix the build error and try again."
    if cls == "patch_apply_failed":
        return "Your diff did not apply cleanly. Re-emit it against the current source."
    if cls == "unexpected_exit":
        return "The crash is gone but the target exited abnormally. Investigate the new exit path."
    return f"Attempt rejected ({cls}). Try a different approach."


def repair_with_retries(*, bug, agent, verify, max_attempts=5,
                        resume_attempts=None, resume_feedback="", on_attempt=None):
    """Drive up to `max_attempts` repair attempts with feedback between them.

    `resume_attempts` (already-completed attempt records from a prior, interrupted
    run of this same bug) and `resume_feedback` (the feedback text for the next
    attempt, as of the last resumed attempt) let a caller continue a bug mid-loop
    instead of restarting at attempt 1 -- e.g. after a usage cap killed the process
    partway through. Numbering picks up at `len(resume_attempts) + 1`.

    `on_attempt`, if given, is called with each attempt's record right after it
    completes (before the loop continues or returns) -- the durable-checkpoint hook,
    so a caller can persist progress attempt-by-attempt without this function doing
    any IO itself.

    Returns a dict:
      status: "solved" | "exhausted" | "interrupted"
      attempts: [{"attempt": n, "diff": str, "verdict": str}, ...]
      accepted: the winning attempt record (if solved) else None
      contrastive_pair: (rejected_record, accepted_record) | None
                        — present only when the agent failed at least once then succeeded,
                        i.e. there is something to learn from its own gap.

    "interrupted" means this attempt doesn't reflect the agent's own capability, so
    it's not a genuine failure -- either a usage cap cut it off (run["usage_limit"],
    set by arvo_oss_crs.detect_usage_limit) or the user pressed q to abort
    (run["aborted"], set by arvo_oss_crs.run_oss_crs via its abort_event). Either
    way that attempt is NOT appended to `attempts` and `on_attempt` is NOT called
    for it -- it costs zero real attempts, unlike every other rejection. `attempts`
    on this return is exactly what went in, so a caller resuming later retries the
    SAME attempt number rather than advancing past it. `usage_limit`/`aborted` are
    included in the result so the caller can report why and when to retry.
    """
    bug_id = bug["localId"]
    attempts = list(resume_attempts) if resume_attempts else []
    feedback = resume_feedback
    start = len(attempts) + 1

    for n in range(start, max_attempts + 1):
        run = agent(n, feedback)
        if run.get("usage_limit") or run.get("aborted"):
            return {"status": "interrupted", "attempts": attempts, "accepted": None,
                    "contrastive_pair": None, "usage_limit": run.get("usage_limit"),
                    "aborted": bool(run.get("aborted"))}
        diff = run.get("diff", "")
        if not diff.strip():
            # No patch. A run that hit the wall-clock cap is a distinct, actionable
            # failure -- feed that back rather than a generic "no changes".
            verification = {"classification": "timed_out" if run.get("timed_out") else "no_changes"}
        elif run.get("check_required") and not run.get("check_passed"):
            # Enforcement (OSS_CRS_CHECK_PATCH): the agent must self-validate with
            # check-patch before submitting. Reject an unchecked patch outright -- don't
            # even pay for a verify build -- and steer it to the tool. check-patch runs
            # the same build+PoC+test as verify, so a truly-correct patch loses only one
            # round; the point is to make the agent converge in-run instead of guessing.
            verification = {"classification": "unchecked"}
        else:
            verification = verify(bug_id, diff)
        verdict = verification.get("classification", "no_changes")
        record = {"attempt": n, "diff": diff, "verdict": verdict,
                  "trajectory_summary": run.get("trajectory_summary", "")}

        if verdict == "verified_correct":
            record["feedback_for_next"] = ""
            attempts.append(record)
            if on_attempt:
                on_attempt(record)
            prior_rejected = next((a for a in reversed(attempts[:-1]) if a["verdict"] != "verified_correct"), None)
            pair = (prior_rejected, record) if prior_rejected else None
            return {"status": "solved", "attempts": attempts, "accepted": record,
                    "contrastive_pair": pair}

        feedback = describe_feedback(verification)
        record["feedback_for_next"] = feedback
        attempts.append(record)
        if on_attempt:
            on_attempt(record)

    return {"status": "exhausted", "attempts": attempts, "accepted": None, "contrastive_pair": None}
