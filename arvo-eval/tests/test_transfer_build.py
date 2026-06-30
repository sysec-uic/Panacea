import json

from transfer_build import build_cold_memory
from playbook_store import load_state


def bug(local_id, project, crash_type):
    return {"localId": local_id, "project": project, "crash_type": crash_type}


def _agent(diff="--- p ---", calls=None):
    def agent(bug_id, project_dir, skip_build):
        if calls is not None:
            calls.append(bug_id)
        return {"diff": diff, "trajectory_summary": "traj"}
    return agent


def _verify(classification):
    return lambda bug_id, diff: {"classification": classification}


def _grade(label):
    return lambda b, diff: {"label": label, "fix_image_available": True, "divergences": []}


def _extract():
    return lambda bug, diff, trajectory_summary, verdict: {
        "trigger": f"T{bug['localId']}", "root_cause_lesson": "L",
        "how_to_apply": "A", "tags": ["x"], "confidence": "medium"}


def _build(tmp_path, bugs, *, agent, verify, grade, extract=None, inject=None, **kw):
    inject_calls = []
    return build_cold_memory(
        bugs=bugs, h_path=tmp_path / "H.json", progress_path=tmp_path / "progress.jsonl",
        project_dir_for=lambda bid: tmp_path / str(bid),
        agent=agent, verify=verify, grade=grade, extract=extract or _extract(),
        inject=inject or (lambda text, pd: inject_calls.append(text)),
        **kw), inject_calls


def test_confirmed_bug_donates_tagged_high_confidence_heuristic(tmp_path):
    _, _ = _build(tmp_path, [bug(1, "php", "Use-of-uninitialized-value")],
                  agent=_agent(), verify=_verify("verified_correct"), grade=_grade("oracle_confirmed"))
    h = load_state(tmp_path / "H.json")["heuristics"][0]
    assert h["source_bug"] == 1 and h["source_project"] == "php"
    assert h["crash_class"] == "uninit"
    assert h["added_after_bug"] == 1
    assert h["oracle"] == "confirmed" and h["confidence"] == "high"


def test_divergent_bug_is_vetoed(tmp_path):
    _build(tmp_path, [bug(1, "php", "Use-of-uninitialized-value")],
           agent=_agent(), verify=_verify("verified_correct"), grade=_grade("divergent"))
    assert load_state(tmp_path / "H.json")["heuristics"] == []
    rec = json.loads((tmp_path / "progress.jsonl").read_text().splitlines()[0])
    assert rec["donated"] is False and rec["oracle_label"] == "divergent"


def test_unsolved_bug_donates_nothing_and_skips_grade(tmp_path):
    graded = {"called": False}

    def grade(b, diff):
        graded["called"] = True
        return {"label": "oracle_confirmed", "fix_image_available": True, "divergences": []}

    _build(tmp_path, [bug(1, "php", "Heap-buffer-overflow READ 1")],
           agent=_agent(), verify=_verify("still_crashes"), grade=grade)
    assert graded["called"] is False
    assert load_state(tmp_path / "H.json")["heuristics"] == []


def test_no_fix_available_donates_tests_only(tmp_path):
    _build(tmp_path, [bug(1, "php", "Heap-use-after-free READ 8")],
           agent=_agent(), verify=_verify("verified_correct"), grade=_grade("no_fix_available"))
    h = load_state(tmp_path / "H.json")["heuristics"][0]
    assert h["oracle"] == "tests_only" and h["confidence"] == "medium"


def test_injects_empty_before_each_cold_run(tmp_path):
    _, inject_calls = _build(tmp_path,
        [bug(1, "php", "Use-of-uninitialized-value"), bug(2, "vlc", "Use-of-uninitialized-value")],
        agent=_agent(), verify=_verify("still_crashes"), grade=_grade("oracle_confirmed"))
    assert inject_calls == ["", ""]   # cold = uncontaminated by any stale playbook


def test_resume_skips_bugs_already_in_progress_ledger(tmp_path):
    (tmp_path / "progress.jsonl").write_text(
        json.dumps({"bug_id": 1, "project": "php", "crash_class": "uninit",
                    "cold_classification": "still_crashes", "oracle_label": None,
                    "donated": False, "heuristic_id": None}) + "\n")
    calls = []
    _build(tmp_path, [bug(1, "php", "Use-of-uninitialized-value"), bug(2, "vlc", "Use-of-uninitialized-value")],
           agent=_agent(calls=calls), verify=_verify("still_crashes"), grade=_grade("divergent"))
    assert calls == [2]   # bug 1 skipped


def test_resume_skips_bug_already_donated_to_H(tmp_path):
    # H was saved but the progress record never landed (crash in the gap). The donor's
    # presence in H is itself the durable "done" marker, so the bug is not re-run.
    from playbook_store import new_state, add_heuristic, save_state
    s = add_heuristic(new_state(), _extract()(bug=bug(1, "php", "x"), diff="", trajectory_summary="", verdict="v"),
                      source_bug=1, after_bug=1, source_project="php", crash_class="uninit")
    save_state(s, tmp_path / "H.json")
    calls = []
    _build(tmp_path, [bug(1, "php", "Use-of-uninitialized-value"), bug(2, "vlc", "Use-of-uninitialized-value")],
           agent=_agent(calls=calls), verify=_verify("still_crashes"), grade=_grade("divergent"))
    assert calls == [2]   # bug 1 not re-run despite no progress record
