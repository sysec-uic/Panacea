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
        avail = label != "no_fix_available"
        return {"label": label, "fix_image_available": avail,
                "divergences": [{"probe": "poc", "kind": "stdout"}] * n}
    return _g


@pytest.fixture
def dryrun_kwargs(tmp_path):
    """Factory fixture: returns collaborator stubs + tmp paths for a single solved bug.

    The `grade` collaborator is intentionally NOT provided here -- each oracle test
    supplies its own `grade=_grade_stub(...)` so the verdict under test is explicit.
    """
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


def _bug(localId):
    return {"localId": localId, "crash_type": "c", "sanitizer": "asan",
            "fuzz_target": "f", "crash_output": ""}


def test_resume_skips_bugs_already_recorded_for_this_pass(tmp_path):
    from ledger import append_record
    ledger = tmp_path / "ledger.jsonl"
    append_record(ledger, {"bug_id": 100, "pass": "treatment",
                           "classification": "verified_correct", "n_attempts": 1})

    called = []

    def recording_agent(bug_id, project_dir, skip_build):
        called.append(bug_id)
        return stub_agent(bug_id, project_dir, skip_build)

    result = run_pass(
        bugs=[_bug(100), _bug(200)], pass_name="treatment", inject_enabled=True,
        state_path=tmp_path / "state.json", ledger_path=ledger,
        project_dir_for=lambda bid: tmp_path / f"proj-{bid}",
        agent=recording_agent, verify=stub_verify, extract=stub_extract,
        grade=_grade_stub("no_fix_available"),
    )
    assert called == [200]                          # bug 100 skipped, agent not re-run
    assert [r["bug_id"] for r in result] == [200]   # only the newly-run bug returned


def test_resume_is_scoped_to_pass_name(tmp_path):
    # A record from the 'control' pass must not make the 'treatment' pass skip it.
    from ledger import append_record
    ledger = tmp_path / "ledger.jsonl"
    append_record(ledger, {"bug_id": 100, "pass": "control",
                           "classification": "verified_correct", "n_attempts": 1})

    called = []

    def recording_agent(bug_id, project_dir, skip_build):
        called.append(bug_id)
        return stub_agent(bug_id, project_dir, skip_build)

    run_pass(
        bugs=[_bug(100)], pass_name="treatment", inject_enabled=True,
        state_path=tmp_path / "state.json", ledger_path=ledger,
        project_dir_for=lambda bid: tmp_path / f"proj-{bid}",
        agent=recording_agent, verify=stub_verify, extract=stub_extract,
        grade=_grade_stub("no_fix_available"),
    )
    assert called == [100]   # different pass -> not skipped


def test_attempt_level_resume_continues_after_simulated_crash(tmp_path):
    # Simulates the real usage-cap scenario: attempt 1 fails and gets checkpointed,
    # then the process dies (here: the agent raises) before attempt 2 ever runs. A
    # second run_pass() call, same checkpoint/ledger/state paths, must pick up at
    # attempt 2 -- not re-run attempt 1 -- and the final ledger record must reflect
    # both attempts (count and summed tokens).
    from attempt_checkpoint import read_checkpoint

    ledger = tmp_path / "ledger.jsonl"
    state_path = tmp_path / "state.json"
    checkpoint_path_for = lambda bid: tmp_path / "checkpoints" / str(bid) / "attempts.jsonl"

    class SimulatedCrash(Exception):
        pass

    calls = []

    def dying_agent(bug_id, project_dir, skip_build):
        calls.append(bug_id)
        if len(calls) == 1:
            return {"diff": "GUARD_DIFF", "trajectory_summary": "t1",
                    "summary": {"tokens": {"input_tokens": 10}}}
        raise SimulatedCrash("usage ran out")

    def crash_verify(bug_id, diff):
        return {"classification": "fixed_tests_failed", "make_test_tail": "boom"}

    with pytest.raises(SimulatedCrash):
        run_pass(
            bugs=[_bug(100)], pass_name="treatment", inject_enabled=True,
            state_path=state_path, ledger_path=ledger,
            project_dir_for=lambda bid: tmp_path / f"proj-{bid}",
            agent=dying_agent, verify=crash_verify, extract=stub_extract,
            grade=_grade_stub("no_fix_available"),
            checkpoint_path_for=checkpoint_path_for,
        )

    ckpt = checkpoint_path_for(100)
    saved = read_checkpoint(ckpt)
    assert len(saved) == 1
    assert saved[0]["verdict"] == "fixed_tests_failed"
    from ledger import read_records
    assert read_records(ledger) == []   # never reached the ledger write

    resumed_calls = []

    def resuming_agent(bug_id, project_dir, skip_build):
        resumed_calls.append(bug_id)
        return {"diff": "FIX_DIFF", "trajectory_summary": "t2",
                "summary": {"tokens": {"input_tokens": 5}}}

    def resuming_verify(bug_id, diff):
        return {"classification": "verified_correct", "make_test_ok": True}

    def stub_contrastive(bug, rejected_diff, accepted_diff, rejected_verdict):
        # Solved after a real failure (attempt 1's GUARD_DIFF) -> run_pass takes the
        # contrastive-lesson path, which defaults to a REAL LLM call if not stubbed.
        return {"trigger": "t", "wrong_approach": "guard", "correct_approach": "fix",
                "lesson": "l", "how_to_apply": "a", "tags": ["t"], "confidence": "high",
                "kind": "contrastive"}

    result = run_pass(
        bugs=[_bug(100)], pass_name="treatment", inject_enabled=True,
        state_path=state_path, ledger_path=ledger,
        project_dir_for=lambda bid: tmp_path / f"proj-{bid}",
        agent=resuming_agent, verify=resuming_verify, extract=stub_extract,
        contrastive=stub_contrastive,
        grade=_grade_stub("no_fix_available"),
        checkpoint_path_for=checkpoint_path_for,
    )

    assert len(resumed_calls) == 1                        # only the new attempt ran
    assert result[0]["classification"] == "verified_correct"
    assert result[0]["n_attempts"] == 2                    # 1 resumed + 1 new
    assert result[0]["tokens"] == {"input_tokens": 15}     # 10 checkpointed + 5 new
    assert read_checkpoint(ckpt) == []                     # cleared once fully recorded


def test_usage_limit_stops_pass_without_ledger_write_or_checkpoint_clear(tmp_path):
    # Bug 100 gets cut off by a usage cap on its first attempt; bug 200 (later in
    # `bugs`) must never even be started -- it would hit the identical cap.
    ledger = tmp_path / "ledger.jsonl"
    checkpoint_path_for = lambda bid: tmp_path / "checkpoints" / str(bid) / "attempts.jsonl"

    started = []

    def capped_agent(bug_id, project_dir, skip_build):
        started.append(bug_id)
        return {"diff": "X", "trajectory_summary": "t",
                "usage_limit": {"resets_at": 123, "resets_at_human": "9:50pm (UTC)"}}

    result = run_pass(
        bugs=[_bug(100), _bug(200)], pass_name="treatment", inject_enabled=True,
        state_path=tmp_path / "state.json", ledger_path=ledger,
        project_dir_for=lambda bid: tmp_path / f"proj-{bid}",
        agent=capped_agent, verify=stub_verify, extract=stub_extract,
        grade=_grade_stub("no_fix_available"),
        checkpoint_path_for=checkpoint_path_for,
    )

    assert started == [100]                              # bug 200 never attempted
    assert result == []                                   # nothing completed
    from ledger import read_records
    assert read_records(ledger) == []                     # no ledger entry for bug 100
    from attempt_checkpoint import read_checkpoint
    assert read_checkpoint(checkpoint_path_for(100)) == [] # the capped attempt wasn't checkpointed


def test_user_abort_stops_pass_without_ledger_write(tmp_path, capsys):
    # Same shape as a usage-limit hit, but user-triggered (q-to-abort) -- distinct
    # print message, same "don't checkpoint, don't record, stop the pass" behavior.
    ledger = tmp_path / "ledger.jsonl"
    started = []

    def aborted_agent(bug_id, project_dir, skip_build):
        started.append(bug_id)
        return {"diff": "X", "trajectory_summary": "t", "aborted": True}

    result = run_pass(
        bugs=[_bug(100), _bug(200)], pass_name="treatment", inject_enabled=True,
        state_path=tmp_path / "state.json", ledger_path=ledger,
        project_dir_for=lambda bid: tmp_path / f"proj-{bid}",
        agent=aborted_agent, verify=stub_verify, extract=stub_extract,
        grade=_grade_stub("no_fix_available"),
    )

    assert started == [100]                # bug 200 never attempted
    assert result == []
    from ledger import read_records
    assert read_records(ledger) == []
    assert "aborted by user" in capsys.readouterr().out


def test_usage_limit_after_real_checkpointed_attempts_preserves_them(tmp_path):
    # A bug that already has 2 real checkpointed attempts hits the cap on attempt 3:
    # attempts 1-2 must survive on disk for the next resume, untouched.
    from attempt_checkpoint import append_checkpoint
    ckpt = tmp_path / "checkpoints" / "100" / "attempts.jsonl"
    append_checkpoint(ckpt, {"attempt": 1, "diff": "A", "verdict": "still_crashes",
                             "feedback_for_next": "fb1", "tokens": {}})
    append_checkpoint(ckpt, {"attempt": 2, "diff": "B", "verdict": "still_crashes",
                             "feedback_for_next": "fb2", "tokens": {}})

    def capped_agent(bug_id, project_dir, skip_build):
        return {"diff": "C", "trajectory_summary": "t", "usage_limit": {"resets_at": 1}}

    run_pass(
        bugs=[_bug(100)], pass_name="treatment", inject_enabled=True,
        state_path=tmp_path / "state.json", ledger_path=tmp_path / "ledger.jsonl",
        project_dir_for=lambda bid: tmp_path / f"proj-{bid}",
        agent=capped_agent, verify=stub_verify, extract=stub_extract,
        grade=_grade_stub("no_fix_available"),
        checkpoint_path_for=lambda bid: ckpt,
    )

    from attempt_checkpoint import read_checkpoint
    saved = read_checkpoint(ckpt)
    assert len(saved) == 2                                 # unchanged -- attempt 3 never checkpointed
    assert [r["attempt"] for r in saved] == [1, 2]


def test_checkpoint_path_for_none_disables_resume_entirely(tmp_path):
    # Default behavior (no checkpoint_path_for) must be unaffected -- a bug that
    # "crashes" simply propagates the exception with nothing persisted anywhere.
    class SimulatedCrash(Exception):
        pass

    def dying_agent(bug_id, project_dir, skip_build):
        raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        run_pass(
            bugs=[_bug(100)], pass_name="treatment", inject_enabled=True,
            state_path=tmp_path / "state.json", ledger_path=tmp_path / "ledger.jsonl",
            project_dir_for=lambda bid: tmp_path / f"proj-{bid}",
            agent=dying_agent, verify=stub_verify, extract=stub_extract,
            grade=_grade_stub("no_fix_available"),
        )
    assert not (tmp_path / "checkpoints").exists()


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
    from ledger import read_records
    rec = read_records(kw["ledger_path"])[-1]
    assert rec["oracle_label"] == "oracle_confirmed"


def test_divergent_lesson_is_learned_tests_only(dryrun_kwargs):
    kw = dryrun_kwargs(solved=True)
    run_pass(**{**kw, "grade": _grade_stub("divergent")})
    from playbook_store import load_state
    state = load_state(kw["state_path"])
    assert len(state["heuristics"]) == 1
    assert state["heuristics"][0]["oracle"] == "tests_only"
    from ledger import read_records
    rec = read_records(kw["ledger_path"])[-1]
    assert rec["oracle_label"] == "divergent"
    assert rec["n_divergences"] == 1


def test_oracle_error_detail_is_recorded_in_ledger(dryrun_kwargs):
    # grade()'s error path returns an "error" string; the ledger must keep it or
    # oracle_error records are undiagnosable after the fact (439237851).
    def erroring_grade(bug, diff):
        return {"label": "oracle_error", "fix_image_available": True,
                "divergences": [], "error": "docker: golden probe timed out"}

    kw = dryrun_kwargs(solved=True)
    run_pass(**{**kw, "grade": erroring_grade})
    from ledger import read_records
    rec = read_records(kw["ledger_path"])[-1]
    assert rec["oracle_label"] == "oracle_error"
    assert rec["oracle_error"] == "docker: golden probe timed out"


def test_default_verify_delegates_to_verify_fix(monkeypatch):
    # _default_verify used to bless ANY non-empty diff as verified_correct, so the
    # feedback/retry loop never fired in real runs. It must call verify_fix.verify.
    import learn_loop
    import verify_fix
    calls = []

    def fake_verify(bug_id):
        calls.append(bug_id)
        return {"classification": "still_crashes", "run_output_tail": "boom"}

    monkeypatch.setattr(verify_fix, "verify", fake_verify)
    out = learn_loop._default_verify(7, "--- a/x\n+++ b/x\n")
    assert out["classification"] == "still_crashes"
    assert calls == [7]


def test_default_verify_empty_diff_needs_no_docker():
    import learn_loop
    assert learn_loop._default_verify(7, "   ")["classification"] == "no_changes"


def test_default_agent_ignores_stale_patch_from_previous_run(tmp_path, monkeypatch):
    # 439237851 attempt-1 regression: the CRS run produced no patch, but a stale
    # oss_crs_patch_0.diff from an OLD run was still in the results dir and got
    # verified (and fed back) as if this attempt produced it. Only patches listed
    # in THIS run's summary may count.
    import learn_loop
    import arvo_oss_crs
    monkeypatch.setattr(learn_loop, "RESULTS_BASE", tmp_path)
    monkeypatch.delenv("LEARN_PASS", raising=False)
    d = tmp_path / "42"
    d.mkdir()
    (d / "oss_crs_patch_0.diff").write_text("STALE DIFF")
    monkeypatch.setattr(arvo_oss_crs, "run_oss_crs",
                        lambda bug_id, skip_build=False, abort_controller=None, on_phase=None, on_line=None: {"patch_files": []})
    run = learn_loop._default_agent(42, tmp_path / "proj", True)
    assert run["diff"] == ""


def test_default_agent_reads_patch_from_this_runs_summary(tmp_path, monkeypatch):
    import learn_loop
    import arvo_oss_crs
    monkeypatch.setattr(learn_loop, "RESULTS_BASE", tmp_path)
    monkeypatch.delenv("LEARN_PASS", raising=False)
    d = tmp_path / "42"
    d.mkdir()
    fresh = d / "oss_crs_patch_0.diff"
    fresh.write_text("FRESH DIFF")
    monkeypatch.setattr(arvo_oss_crs, "run_oss_crs",
                        lambda bug_id, skip_build=False, abort_controller=None, on_phase=None, on_line=None: {"patch_files": [str(fresh)]})
    run = learn_loop._default_agent(42, tmp_path / "proj", True)
    assert run["diff"] == "FRESH DIFF"
    # The verify bridge is refreshed from the fresh patch.
    assert (d / "patch.diff").read_text() == "FRESH DIFF"


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


# ---------------------------------------------------------------------------
# Live-status wiring: PhaseTracker, pass_tallies, playbook_stat, _make_agent
# ---------------------------------------------------------------------------

class _FakeStatus:
    """Records every set_phases/set_tallies/set_stats call for assertions,
    without needing a real rich Console/terminal."""
    def __init__(self):
        self.phases_calls = []
        self.tallies_calls = []
        self.stats_calls = []
        self.position = None
        self.subject = None

    def set_phases(self, phases):
        self.phases_calls.append(list(phases))

    def set_tallies(self, tallies):
        self.tallies_calls.append(list(tallies))

    def set_stats(self, stats):
        self.stats_calls.append(dict(stats))


def test_phase_tracker_resets_and_orders_phases_per_bug():
    from learn_loop import PhaseTracker
    status = _FakeStatus()
    tracker = PhaseTracker(status)

    n = tracker.reset_for_bug(100)
    assert n == 1
    labels = [p.label for p in status.phases_calls[-1]]
    assert labels == ["prepare environment", "build target", "running agent"]
    assert all(p.status.value == "pending" for p in status.phases_calls[-1])

    n2 = tracker.reset_for_bug(100)   # a retry on the SAME bug -- attempt count grows
    assert n2 == 2
    n3 = tracker.reset_for_bug(200)   # a different bug starts its own count at 1
    assert n3 == 1


def test_phase_tracker_marks_active_then_done_with_elapsed():
    from learn_loop import PhaseTracker
    from live_status import PhaseStatus
    status = _FakeStatus()
    tracker = PhaseTracker(status)
    tracker.reset_for_bug(100)

    tracker.on_phase("build", "start")
    build_phase = status.phases_calls[-1][1]   # index 1 == "build" in PHASE_ORDER
    assert build_phase.status is PhaseStatus.ACTIVE
    assert build_phase.elapsed is None

    tracker.on_phase("build", "done")
    build_phase = status.phases_calls[-1][1]
    assert build_phase.status is PhaseStatus.DONE
    assert build_phase.elapsed is not None and build_phase.elapsed.endswith("s")


def test_phase_tracker_ignores_unknown_phase_keys():
    from learn_loop import PhaseTracker
    status = _FakeStatus()
    tracker = PhaseTracker(status)
    tracker.reset_for_bug(100)
    calls_before = len(status.phases_calls)
    tracker.on_phase("some_future_phase", "start")   # must not raise or update
    assert len(status.phases_calls) == calls_before


def test_pass_tallies_counts_verified_vs_total_per_pass(tmp_path):
    from learn_loop import pass_tallies
    from ledger import append_record
    ledger = tmp_path / "ledger.jsonl"
    append_record(ledger, {"bug_id": 1, "pass": "control", "classification": "verified_correct"})
    append_record(ledger, {"bug_id": 2, "pass": "control", "classification": "still_crashes"})
    append_record(ledger, {"bug_id": 3, "pass": "treatment", "classification": "verified_correct"})

    tallies = {t.label: t for t in pass_tallies(ledger)}
    assert tallies["control"].done == 1 and tallies["control"].total == 2
    assert tallies["treatment"].done == 1 and tallies["treatment"].total == 1


def test_pass_tallies_missing_ledger_is_zeroed(tmp_path):
    from learn_loop import pass_tallies
    tallies = {t.label: t for t in pass_tallies(tmp_path / "nope.jsonl")}
    assert tallies["control"].done == 0 and tallies["control"].total == 0
    assert tallies["treatment"].done == 0 and tallies["treatment"].total == 0


def test_playbook_stat_reports_version_and_heuristic_count(tmp_path):
    from learn_loop import playbook_stat
    from playbook_store import load_state, save_state, add_heuristic
    state_path = tmp_path / "state.json"
    state = load_state(state_path)
    state = add_heuristic(state, {"trigger": "t", "root_cause_lesson": "l", "how_to_apply": "a",
                                  "tags": ["t"], "confidence": "high"}, source_bug=100, after_bug=100)
    save_state(state, state_path)

    stats = playbook_stat(state_path)
    assert stats["playbook"] == f"v{state['version']} · 1 heuristics"


def test_playbook_stat_missing_state_reports_a_fresh_empty_playbook(tmp_path):
    # playbook_store.load_state gracefully returns a fresh state for a missing
    # path (not an error) -- "v0 - 0 heuristics" is the correct, honest stat.
    from learn_loop import playbook_stat
    assert playbook_stat(tmp_path / "nope.json") == {"playbook": "v0 · 0 heuristics"}


def test_make_agent_before_attempt_fires_once_per_agent_call():
    from learn_loop import _make_agent
    calls = []

    def fake_default_agent(bug_id, project_dir, skip_build, abort_controller=None, on_phase=None, on_line=None):
        return {"diff": "", "trajectory_summary": ""}

    import learn_loop
    orig = learn_loop._default_agent
    learn_loop._default_agent = fake_default_agent
    try:
        agent = _make_agent(before_attempt=lambda bid: calls.append(bid))
        agent(100, "/x", False)
        agent(100, "/x", False)   # a retry -- before_attempt fires again
        assert calls == [100, 100]
    finally:
        learn_loop._default_agent = orig
