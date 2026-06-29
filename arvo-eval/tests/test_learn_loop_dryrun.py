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


def test_dryrun_treatment_pass_is_holdout_safe(tmp_path):
    bugs = [{"localId": 100, "crash_type": "c", "sanitizer": "asan", "fuzz_target": "f", "crash_output": ""},
            {"localId": 200, "crash_type": "c", "sanitizer": "asan", "fuzz_target": "f", "crash_output": ""}]
    result = run_pass(
        bugs=bugs, pass_name="treatment", inject_enabled=True,
        state_path=tmp_path / "state.json", ledger_path=tmp_path / "ledger.jsonl",
        project_dir_for=lambda bid: tmp_path / f"proj-{bid}",
        agent=stub_agent, verify=stub_verify, extract=stub_extract,
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
    )
    assert result[1]["injected_seen"] == ""   # never injected, even though store grew
