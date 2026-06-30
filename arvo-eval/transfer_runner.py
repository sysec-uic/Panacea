"""Phase B runner for the cross-project transfer experiment.

For each (eval bug, arm, trial): select the arm's eligible donor slice of the frozen
store H, inject it, run the agent ONCE, verify, and (if solved) grade against the
`-fix` image. One ledger record per trial.

Single-shot per trial (not `repair_with_retries`): the injected playbook is the
independent variable and m trials estimate the agent's stochastic success rate, so we
deliberately omit the inter-attempt feedback layer that would confound an arm
comparison. The faithfulness wall is unchanged -- `grade` output only sets the score
and ledger fields; the agent never sees it.

Collaborators are injected so this runs without Docker in tests:
  agent(bug_id, project_dir, skip_build) -> {"diff": str, "trajectory_summary": str}
  verify(bug_id, diff)                   -> {"classification": str, ...}
  grade(bug, diff)                       -> {"label": str, "divergences": list, ...}
"""
import json
import os
import sys
from pathlib import Path

from crash_taxonomy import crash_class
from transfer_filter import select_for_arm
from playbook_store import render_heuristics, load_state
from transfer_eval_set import select_eval_bugs
from injector import inject as _inject
from ledger import append_record

ARMS = ("cold", "matched_foreign", "placebo_foreign", "in_project")


def _completed_cells(ledger_path) -> set:
    """(bug_id, arm, trial) cells already in the ledger -- the resume skip-set."""
    path = Path(ledger_path)
    done = set()
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                done.add((r["bug_id"], r["arm"], r["trial"]))
    return done


def score_outcome(*, solved: bool, oracle_label: str | None) -> int:
    """0 = not solved, 1 = solved but not canonical-confirmed, 2 = oracle_confirmed."""
    if not solved:
        return 0
    return 2 if oracle_label == "oracle_confirmed" else 1


def _default_agent(bug_id, project_dir, skip_build):
    from learn_loop import _default_agent as a
    return a(bug_id, project_dir, skip_build)


def _default_verify(bug_id, diff):
    from learn_loop import _default_verify as v
    return v(bug_id, diff)


def _default_grade(bug, diff):
    from differential_oracle import grade
    return grade(bug, diff)


def run_transfer_arm(*, eval_bugs, arm, heuristics, trials, ledger_path, project_dir_for,
                     agent=_default_agent, verify=_default_verify, grade=_default_grade,
                     inject=_inject, render=render_heuristics, skip_build=False,
                     store_version=None, done_cells=None):
    """Run one arm over `eval_bugs` with `trials` independent attempts per bug.

    `done_cells` is a set of already-completed (bug_id, arm, trial) keys -- those
    cells are skipped, so an interrupted run resumes by re-invoking with the same
    ledger (see `_completed_cells`).
    """
    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm}")
    done = done_cells or set()
    records = []
    for bug in eval_bugs:
        bug_id = bug["localId"]
        cls = crash_class(bug["crash_type"])
        donors = select_for_arm(arm, heuristics, bug=bug)   # same per (bug, arm), all trials
        donor_ids = [h["id"] for h in donors]
        text = render(donors) if donors else ""
        donor_bytes = len(text)
        project_dir = Path(project_dir_for(bug_id))
        project_dir.mkdir(parents=True, exist_ok=True)

        for trial in range(trials):
            if (bug_id, arm, trial) in done:
                continue
            inject(text, project_dir)
            run = agent(bug_id, project_dir, skip_build)
            diff = run.get("diff", "")
            verdict = verify(bug_id, diff) if diff.strip() else {"classification": "no_changes"}
            solved = verdict.get("classification") == "verified_correct"

            oracle_label, n_div = None, 0
            if solved:
                g = grade(bug, diff)
                oracle_label = g["label"]
                n_div = len(g["divergences"])

            record = {"bug_id": bug_id, "project": bug["project"], "crash_class": cls,
                      "arm": arm, "trial": trial,
                      "score": score_outcome(solved=solved, oracle_label=oracle_label),
                      "classification": verdict.get("classification"),
                      "oracle_label": oracle_label, "n_divergences": n_div,
                      "n_donors": len(donor_ids), "donor_ids": donor_ids,
                      "donor_bytes": donor_bytes, "store_version": store_version}
            append_record(ledger_path, record)
            records.append(record)
    return records


def _arg(flag, default=None):
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return default


def main():
    """Phase B driver: loop arms x trials over the eval set, reading frozen H.

    Resumable: re-invoking with the same ledger skips completed (bug, arm, trial)
    cells, so a single OAuth token can grind through in chunks across usage windows.
    Defaults to the pre-registered B vs B' contrast; add cold/in-project via --arms.
    """
    from build_instance import load_bug
    from arvo_bugs import scoped_bug_ids

    db = Path(os.environ.get("ARVO_DB_PATH", Path(__file__).parent / "arvo_new.db"))
    projects = (p.split(",") if (p := _arg("--projects")) else None)
    arms = (a.split(",") if (a := _arg("--arms")) else ["matched_foreign", "placebo_foreign"])
    trials = int(_arg("--trials", "2"))

    ids = scoped_bug_ids(db, projects=projects)
    if (limit := _arg("--limit")):
        ids = ids[:int(limit)]
    bugs = [load_bug(b) for b in ids]

    out = Path(__file__).parent / "results" / "transfer"
    H = load_state(out / "cold_H.json")
    eval_bugs = select_eval_bugs(bugs, H["heuristics"])
    ledger_path = out / "phaseB_ledger.jsonl"
    done = _completed_cells(ledger_path)
    print(f"[transfer_run] arms={arms} trials={trials} eval_bugs={len(eval_bugs)} "
          f"donors={len(H['heuristics'])} resume(done_cells={len(done)})")

    for arm in arms:
        run_transfer_arm(
            eval_bugs=eval_bugs, arm=arm, heuristics=H["heuristics"], trials=trials,
            ledger_path=ledger_path,
            project_dir_for=lambda bid: Path.home() / ".arvo-oss-crs" / str(bid) / "project",
            skip_build="--skip-build" in sys.argv, store_version=H["version"], done_cells=done)


if __name__ == "__main__":
    main()
