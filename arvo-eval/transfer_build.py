"""Phase A of the transfer experiment: the cold build pass that produces frozen H.

Walk bugs in global `localId` order (project-scoped), run the agent COLD (no
injection), verify, and on verified-correct + oracle-non-divergent extract a heuristic
tagged with `source_project` + `crash_class` and append it to H. This mirrors the
learn_loop veto-and-promote, but single-shot and donor-only -- H is the frozen store the
Phase B arm runner reads.

Resumability (essential for one serial OAuth token across usage windows): H and a
progress ledger are append-only on disk. A bug is "done" if it appears in the progress
ledger OR already donated a heuristic to H. H is saved *before* the progress record, so
a donor's presence in H is the durable done-marker even if a crash lands in the gap --
no duplicate donors on resume.

Collaborators are injected so the whole walk is unit-tested without Docker:
  agent(bug_id, project_dir, skip_build) -> {"diff", "trajectory_summary"}
  verify(bug_id, diff)                   -> {"classification", ...}
  grade(bug, diff)                       -> {"label", "divergences", ...}
  extract(bug, diff, trajectory_summary, verdict) -> heuristic dict
"""
import json
import os
import sys
from pathlib import Path

from crash_taxonomy import crash_class
from playbook_store import load_state, save_state, add_heuristic
from injector import inject as _inject
from ledger import append_record


def _default_agent(bug_id, project_dir, skip_build):
    from learn_loop import _default_agent as a
    return a(bug_id, project_dir, skip_build)


def _default_verify(bug_id, diff):
    from learn_loop import _default_verify as v
    return v(bug_id, diff)


def _default_grade(bug, diff):
    from differential_oracle import grade
    return grade(bug, diff)


def _default_extract(bug, diff, trajectory_summary, verdict):
    from learn_loop import _default_extract as e
    return e(bug, diff, trajectory_summary, verdict)


def _processed_ids(progress_path: Path, state: dict) -> set:
    """Bugs already handled: in the progress ledger OR already a donor in H."""
    done = {h["source_bug"] for h in state["heuristics"]}
    if progress_path.exists():
        for line in progress_path.read_text().splitlines():
            if line.strip():
                done.add(json.loads(line)["bug_id"])
    return done


def build_cold_memory(*, bugs, h_path, progress_path, project_dir_for,
                      agent=_default_agent, verify=_default_verify, grade=_default_grade,
                      extract=_default_extract, inject=_inject, skip_build=False):
    h_path, progress_path = Path(h_path), Path(progress_path)
    state = load_state(h_path)
    done = _processed_ids(progress_path, state)

    for bug in bugs:
        bug_id = bug["localId"]
        if bug_id in done:
            continue
        cls = crash_class(bug["crash_type"])
        project_dir = Path(project_dir_for(bug_id))
        project_dir.mkdir(parents=True, exist_ok=True)

        inject("", project_dir)                 # cold: clear any stale playbook
        run = agent(bug_id, project_dir, skip_build)
        diff = run.get("diff", "")
        verdict = verify(bug_id, diff) if diff.strip() else {"classification": "no_changes"}
        solved = verdict.get("classification") == "verified_correct"

        oracle_label, donated, hid = None, False, None
        if solved:
            oracle_label = grade(bug, diff)["label"]
            if oracle_label != "divergent":     # veto divergent; promote/learn otherwise
                lesson = extract(bug, diff, run.get("trajectory_summary", ""), "verified_correct")
                if oracle_label == "oracle_confirmed":
                    lesson["oracle"], lesson["confidence"] = "confirmed", "high"
                else:                           # no_fix_available | oracle_error
                    lesson["oracle"] = "tests_only"
                state = add_heuristic(state, lesson, source_bug=bug_id, after_bug=bug_id,
                                      source_project=bug["project"], crash_class=cls)
                save_state(state, h_path)        # H durable BEFORE the progress record
                donated, hid = True, f"h-{bug_id}"

        append_record(progress_path, {
            "bug_id": bug_id, "project": bug["project"], "crash_class": cls,
            "cold_classification": verdict.get("classification"),
            "oracle_label": oracle_label, "donated": donated, "heuristic_id": hid})
        done.add(bug_id)
    return state


def _arg(flag, default=None):
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return default


def main():
    from build_instance import load_bug
    from arvo_bugs import scoped_bug_ids

    db = Path(os.environ.get("ARVO_DB_PATH", Path(__file__).parent / "arvo_new.db"))
    projects = (p.split(",") if (p := _arg("--projects")) else None)
    ids = scoped_bug_ids(db, projects=projects)
    if (limit := _arg("--limit")):
        ids = ids[:int(limit)]
    bugs = [load_bug(b) for b in ids]

    base = Path(__file__).parent
    out = base / "results" / "transfer"
    print(f"[transfer_build] cold pass: projects={projects or 'ALL'} bugs={len(bugs)} "
          f"resumable -> {out/'cold_H.json'}")
    state = build_cold_memory(
        bugs=bugs, h_path=out / "cold_H.json", progress_path=out / "cold_progress.jsonl",
        project_dir_for=lambda bid: Path.home() / ".arvo-oss-crs" / str(bid) / "project",
        skip_build="--skip-build" in sys.argv)
    print(f"[transfer_build] donors in H: {len(state['heuristics'])}")


if __name__ == "__main__":
    main()
