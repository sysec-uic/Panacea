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
    if cls == "build_failed":
        return "Your patch did not compile. Fix the build error and try again."
    if cls == "patch_apply_failed":
        return "Your diff did not apply cleanly. Re-emit it against the current source."
    if cls == "unexpected_exit":
        return "The crash is gone but the target exited abnormally. Investigate the new exit path."
    return f"Attempt rejected ({cls}). Try a different approach."


def repair_with_retries(*, bug, agent, verify, max_attempts=5):
    """Drive up to `max_attempts` repair attempts with feedback between them.

    Returns a dict:
      status: "solved" | "exhausted"
      attempts: [{"attempt": n, "diff": str, "verdict": str}, ...]
      accepted: the winning attempt record (if solved) else None
      contrastive_pair: (rejected_record, accepted_record) | None
                        — present only when the agent failed at least once then succeeded,
                        i.e. there is something to learn from its own gap.
    """
    bug_id = bug["localId"]
    attempts = []
    feedback = ""

    for n in range(1, max_attempts + 1):
        run = agent(n, feedback)
        diff = run.get("diff", "")
        verification = verify(bug_id, diff) if diff.strip() else {"classification": "no_changes"}
        verdict = verification.get("classification", "no_changes")
        record = {"attempt": n, "diff": diff, "verdict": verdict,
                  "trajectory_summary": run.get("trajectory_summary", "")}
        attempts.append(record)

        if verdict == "verified_correct":
            prior_rejected = next((a for a in reversed(attempts[:-1]) if a["verdict"] != "verified_correct"), None)
            pair = (prior_rejected, record) if prior_rejected else None
            return {"status": "solved", "attempts": attempts, "accepted": record,
                    "contrastive_pair": pair}

        feedback = describe_feedback(verification)

    return {"status": "exhausted", "attempts": attempts, "accepted": None, "contrastive_pair": None}
