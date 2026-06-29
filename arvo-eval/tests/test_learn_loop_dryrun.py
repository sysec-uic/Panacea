import pytest
from pathlib import Path
from learn_loop import run_pass


def stub_agent(bug_id, project_dir, skip_build):
    # Pretend the agent produced a patch; echo the injected playbook back so the
    # test can assert holdout (a bug must never see its own lesson).
    injected = ""
    hfile = Path(project_dir) / "HEURISTICS.md"
    if hfile.exists():
        injected = hfile.read_text()
    return {"diff": f"--- a/x\n+++ b/x\n# fix {bug_id}\n", "injected_seen": injected,
            "trajectory_summary": f"fixed {bug_id}"}


def stub_verify(bug_id, diff):
    return {"classification": "verified_correct", "make_test_ok": True}


def stub_extract(bug, diff, trajectory_summary, verdict):
    return {"trigger": f"pattern from {bug['localId']}", "root_cause_lesson": f"lesson {bug['localId']}",
            "how_to_apply": "apply", "tags": ["t"], "confidence": "high"}


def _grade_stub(label):
    """Return a grade collaborator that always produces the given label."""
    def _g(bug, diff):
        n = 1 if label == "divergent" else 0
        avail = label not in ("no_fix_available",)
        return {"label": label, "fix_image_available": avail,
                "divergences": [{"probe": "poc", "kind": "stdout"}] * n}
    return _g


@pytest.fixture
def dryrun_kwargs(tmp_path):
    """Factory fixture: returns collaborator stubs + tmp paths for a single solved bug."""
    def _make(solved=True):
        def _verify(bug_id, diff):
            if solved:
                return {"classification": "verified_correct", "make_test_ok": True}
            return {"classification": "still_crashes", "run_output_tail": "crash"}

        return {
            "bugs": [{"localId": 100, "crash_type": "c", "sanitizer": "asan",
                      "fuzz_target": "f", "crash_output": ""}],
            "pass_name": "treatment",
            "inject_enabled": True,
            "state_path": tmp_path / "state.json",
            "ledger_path": tmp_path / "ledger.jsonl",
            "project_dir_for": lambda bid: tmp_path / f"proj-{bid}",
            "agent": stub_agent,
            "verify": _verify,
            "extract": stub_extract,
            "grade": _grade_stub("no_fix_available"),
        }
    return _make


def test_dryrun_treatment_pass_is_holdout_safe(tmp_path):
    bugs = [{"localId": 100, "crash_type": "c", "sanitizer": "asan", "fuzz_target": "f", "crash_output": ""},
            {"localId": 200, "crash_type": "c", "sanitizer": "asan", "fuzz_target": "f", "crash_output": ""}]
    result = run_pass(
        bugs=bugs, pass_name="treatment", inject_enabled=True,
        state_path=tmp_path / "state.json", ledger_path=tmp_path / "ledger.jsonl",
        project_dir_for=lambda bid: tmp_path / f"proj-{bid}",
        agent=stub_agent, verify=stub_verify, extract=stub_extract,
        grade=_grade_stub("no_fix_available"),
    )
    # Bug 100 ran with an empty playbook (nothing learned yet).
    assert "lesson 100" not in result[0]["injected_seen"]
    # Bug 200 saw bug 100's lesson but NOT its own.
    assert "lesson 100" in result[1]["injected_seen"]
    assert "lesson 200" not in result[1]["injected_seen"]


def test_dryrun_control_pass_injects_nothing(tmp_path):
    bugs = [{"localId": 100, "crash_type": "c", "sanitizer": "asan", "fuzz_target": "f", "crash_output": ""},
            {"localId": 200, "crash_type": "c", "sanitizer": "asan", "fuzz_target": "f", "crash_output": ""}]
    result = run_pass(
        bugs=bugs, pass_name="control", inject_enabled=False,
        state_path=tmp_path / "state.json", ledger_path=tmp_path / "ledger.jsonl",
        project_dir_for=lambda bid: tmp_path / f"proj-{bid}",
        agent=stub_agent, verify=stub_verify, extract=stub_extract,
        grade=_grade_stub("no_fix_available"),
    )
    assert result[1]["injected_seen"] == ""   # never injected, even though store grew


GUARD, FIX = "GUARD_DIFF", "FIX_DIFF"


def retrying_agent(bug_id, project_dir, skip_build):
    # The agent reads its context (HEURISTICS.md). It only switches to the real fix
    # once it has received feedback about the failing attempt -- mirroring recovery.
    hfile = Path(project_dir) / "HEURISTICS.md"
    ctx = hfile.read_text() if hfile.exists() else ""
    diff = FIX if "Feedback" in ctx else GUARD
    return {"diff": diff, "trajectory_summary": "t"}


def retry_verify(bug_id, diff):
    if diff == FIX:
        return {"classification": "verified_correct", "make_test_ok": True}
    return {"classification": "fixed_tests_failed", "make_test_tail": "::FOO expected 42 got Object"}


def test_retry_learns_contrastively_from_own_attempts(tmp_path):
    captured = {}

    def contrastive(bug, rejected_diff, accepted_diff, rejected_verdict):
        captured.update(rejected=rejected_diff, accepted=accepted_diff, verdict=rejected_verdict)
        return {"trigger": "t", "wrong_approach": "guarded reader", "correct_approach": "fixed writer",
                "lesson": "fix the writer", "how_to_apply": "trace emission", "tags": ["asan"],
                "confidence": "high", "kind": "contrastive"}

    bugs = [{"localId": 300, "crash_type": "c", "sanitizer": "asan", "fuzz_target": "f", "crash_output": ""}]
    result = run_pass(
        bugs=bugs, pass_name="treatment", inject_enabled=True,
        state_path=tmp_path / "state.json", ledger_path=tmp_path / "ledger.jsonl",
        project_dir_for=lambda bid: tmp_path / f"proj-{bid}",
        agent=retrying_agent, verify=retry_verify, contrastive=contrastive, max_attempts=5,
        grade=_grade_stub("no_fix_available"),
    )
    # Solved on the second attempt after recovering from feedback.
    assert result[0]["classification"] == "verified_correct"
    assert result[0]["n_attempts"] == 2
    # The contrastive lesson was learned from the agent's OWN rejected vs accepted attempts.
    assert captured["rejected"] == GUARD and captured["accepted"] == FIX
    assert captured["verdict"] == "fixed_tests_failed"


# ---------------------------------------------------------------------------
# Oracle veto-and-promote tests
# ---------------------------------------------------------------------------

def test_confirmed_lesson_is_added_high_confidence(dryrun_kwargs):
    kw = dryrun_kwargs(solved=True)
    run_pass(**{**kw, "grade": _grade_stub("oracle_confirmed")})
    from playbook_store import load_state
    state = load_state(kw["state_path"])
    assert len(state["heuristics"]) == 1
    assert state["heuristics"][0]["oracle"] == "confirmed"
    assert state["heuristics"][0]["confidence"] == "high"


def test_divergent_lesson_is_suppressed(dryrun_kwargs):
    kw = dryrun_kwargs(solved=True)
    run_pass(**{**kw, "grade": _grade_stub("divergent")})
    from playbook_store import load_state
    state = load_state(kw["state_path"])
    assert state["heuristics"] == []           # vetoed: nothing learned
    from ledger import read_records
    rec = read_records(kw["ledger_path"])[-1]
    assert rec["oracle_label"] == "divergent"
    assert rec["n_divergences"] == 1


def test_no_fix_learns_as_tests_only(dryrun_kwargs):
    kw = dryrun_kwargs(solved=True)
    run_pass(**{**kw, "grade": _grade_stub("no_fix_available")})
    from playbook_store import load_state
    state = load_state(kw["state_path"])
    assert len(state["heuristics"]) == 1
    assert state["heuristics"][0]["oracle"] == "tests_only"
    from ledger import read_records
    rec = read_records(kw["ledger_path"])[-1]
    assert rec["fix_image_available"] is False
