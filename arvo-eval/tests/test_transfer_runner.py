import json

from transfer_runner import run_transfer_arm, score_outcome, _completed_cells


def H(bug_id, project, cls, after):
    return {"id": f"h-{bug_id}", "source_bug": bug_id, "source_project": project,
            "crash_class": cls, "added_after_bug": after, "trigger": f"t{bug_id}",
            "root_cause_lesson": "L", "how_to_apply": "A", "tags": [cls]}


def bug(local_id, project, crash_type):
    return {"localId": local_id, "project": project, "crash_type": crash_type}


HS = [H(1, "php", "uninit", 1), H(2, "vlc", "uninit", 2), H(3, "php", "heap-oob", 3)]
TARGET = bug(20, "mruby", "Use-of-uninitialized-value")


def _runner(arm, *, agent, verify, grade, tmp_path, trials=1, inject=None, heuristics=HS):
    injected = []

    def _inject(text, project_dir):
        injected.append(text)

    records = run_transfer_arm(
        eval_bugs=[TARGET], arm=arm, heuristics=heuristics, trials=trials,
        ledger_path=tmp_path / "ledger.jsonl",
        project_dir_for=lambda bid: tmp_path / str(bid),
        agent=agent, verify=verify, grade=grade,
        inject=inject or _inject,
    )
    return records, injected


def _agent(diff="--- patch ---"):
    return lambda bug_id, project_dir, skip_build: {"diff": diff, "trajectory_summary": "t"}


def _verify(classification):
    return lambda bug_id, diff: {"classification": classification}


def _grade(label):
    return lambda bug, diff: {"label": label, "fix_image_available": True, "divergences": []}


# --- scoring ---------------------------------------------------------------

def test_score_not_solved_is_zero():
    assert score_outcome(solved=False, oracle_label=None) == 0


def test_score_solved_but_not_confirmed_is_one():
    assert score_outcome(solved=True, oracle_label="divergent") == 1
    assert score_outcome(solved=True, oracle_label="no_fix_available") == 1


def test_score_oracle_confirmed_is_two():
    assert score_outcome(solved=True, oracle_label="oracle_confirmed") == 2


# --- arm wiring ------------------------------------------------------------

def test_cold_arm_injects_empty_and_records_no_donors(tmp_path):
    records, injected = _runner("cold", agent=_agent(), verify=_verify("still_crashes"),
                                grade=_grade("oracle_confirmed"), tmp_path=tmp_path)
    assert injected == [""]
    r = records[0]
    assert r["arm"] == "cold" and r["n_donors"] == 0 and r["donor_ids"] == []
    assert r["score"] == 0                       # still_crashes -> not solved


def test_matched_foreign_injects_donor_text_and_logs_ids(tmp_path):
    records, injected = _runner("matched_foreign", agent=_agent(), verify=_verify("verified_correct"),
                                grade=_grade("oracle_confirmed"), tmp_path=tmp_path)
    r = records[0]
    assert sorted(r["donor_ids"]) == ["h-1", "h-2"]   # foreign uninit donors
    assert r["n_donors"] == 2
    assert "t1" in injected[0] and "t2" in injected[0]  # donor triggers rendered
    assert r["score"] == 2                            # solved + confirmed


def test_grade_skipped_when_not_solved(tmp_path):
    sentinel = {"called": False}

    def grade(bug, diff):
        sentinel["called"] = True
        return {"label": "oracle_confirmed", "fix_image_available": True, "divergences": []}

    records, _ = _runner("matched_foreign", agent=_agent(), verify=_verify("build_failed"),
                         grade=grade, tmp_path=tmp_path)
    assert sentinel["called"] is False
    assert records[0]["oracle_label"] is None and records[0]["score"] == 0


def test_trials_produce_one_record_each_and_persist_to_ledger(tmp_path):
    records, _ = _runner("matched_foreign", agent=_agent(), verify=_verify("verified_correct"),
                         grade=_grade("divergent"), tmp_path=tmp_path, trials=3)
    assert [r["trial"] for r in records] == [0, 1, 2]
    assert all(r["score"] == 1 for r in records)      # solved but divergent
    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["bug_id"] == 20


def test_empty_diff_is_no_changes_not_solved(tmp_path):
    records, _ = _runner("cold", agent=_agent(diff="   "), verify=_verify("verified_correct"),
                         grade=_grade("oracle_confirmed"), tmp_path=tmp_path)
    # blank diff never reaches verify; classified no_changes, scored 0.
    assert records[0]["classification"] == "no_changes" and records[0]["score"] == 0


# --- resumability ----------------------------------------------------------

def test_done_cells_are_skipped(tmp_path):
    calls = []

    def agent(bug_id, project_dir, skip_build):
        calls.append(bug_id)
        return {"diff": "--- p ---", "trajectory_summary": "t"}

    records = run_transfer_arm(
        eval_bugs=[TARGET], arm="matched_foreign", heuristics=HS, trials=2,
        ledger_path=tmp_path / "l.jsonl", project_dir_for=lambda b: tmp_path / str(b),
        agent=agent, verify=_verify("verified_correct"), grade=_grade("divergent"),
        inject=lambda t, pd: None, done_cells={(20, "matched_foreign", 0)})
    assert calls == [20]                       # trial 0 skipped, only trial 1 ran
    assert [r["trial"] for r in records] == [1]
    assert (tmp_path / "l.jsonl").read_text().count("\n") == 1


def test_completed_cells_reads_bug_arm_trial_keys(tmp_path):
    p = tmp_path / "l.jsonl"
    p.write_text(json.dumps({"bug_id": 20, "arm": "matched_foreign", "trial": 0}) + "\n" +
                 json.dumps({"bug_id": 20, "arm": "placebo_foreign", "trial": 1}) + "\n")
    assert _completed_cells(p) == {(20, "matched_foreign", 0), (20, "placebo_foreign", 1)}


def test_completed_cells_missing_file_is_empty(tmp_path):
    assert _completed_cells(tmp_path / "nope.jsonl") == set()
