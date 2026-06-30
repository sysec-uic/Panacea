"""Chronological self-improving repair loop for mruby ARVO bugs.

Per bug (in localId order): render the holdout-filtered playbook, optionally inject
it, then make up to `max_attempts` repair attempts with deployment-faithful feedback
between them (crash trace + make test only -- never the -fix image). A lesson is
learned only from SOLVED bugs, and added to the store AFTER the bug is evaluated:
  - solved after a failure -> a CONTRASTIVE lesson from the agent's own rejected vs
    accepted attempts (richer; warns future bugs off the dead end);
  - solved on the first try -> a plain success lesson.
"""
import os
import sys
from pathlib import Path

from playbook_store import load_state, save_state, add_heuristic, render_playbook
from injector import inject
from curator import maybe_compress
from ledger import append_record
from mruby_bugs import mruby_bug_ids
from repair_loop import repair_with_retries


def _default_agent(bug_id, project_dir, skip_build):
    """Real agent: drive OSS-CRS, then return the chosen patch + trajectory tail."""
    from arvo_oss_crs import run_oss_crs
    summary = run_oss_crs(bug_id, skip_build=skip_build)
    _pass = os.environ.get("LEARN_PASS", "")
    results_dir = Path(__file__).parent / "results" / _pass / str(bug_id) if _pass else Path(__file__).parent / "results" / str(bug_id)
    patch = results_dir / "oss_crs_patch_0.diff"
    diff = patch.read_text() if patch.exists() else ""
    log = results_dir / "oss_crs_claude_stdout.log"
    trajectory = "\n".join(log.read_text().splitlines()[-80:]) if log.exists() else ""
    # verify_fix reads results/<id>/patch.diff; bridge the OSS-CRS naming.
    if diff:
        (results_dir / "patch.diff").write_text(diff)
    return {"diff": diff, "trajectory_summary": trajectory, "summary": summary}


def _default_verify(bug_id, diff):
    from verify_fix import verify
    return verify(bug_id)


def _default_extract(bug, diff, trajectory_summary, verdict):
    from extract_heuristic import extract_heuristic
    return extract_heuristic(bug=bug, diff=diff, trajectory_summary=trajectory_summary, verdict=verdict)


def _default_contrastive(bug, rejected_diff, accepted_diff, rejected_verdict):
    from contrastive_extract import extract_contrastive_heuristic
    return extract_contrastive_heuristic(bug=bug, rejected_diff=rejected_diff,
                                         accepted_diff=accepted_diff, rejected_verdict=rejected_verdict)


def _default_grade(bug, diff):
    from differential_oracle import grade
    return grade(bug, diff)


def run_pass(*, bugs, pass_name, inject_enabled, state_path, ledger_path,
             project_dir_for, agent=_default_agent, verify=_default_verify,
             extract=_default_extract, contrastive=_default_contrastive,
             grade=_default_grade, max_attempts=5, skip_build=False):
    """Run one full pass over `bugs` (already in chronological order).

    Each bug gets up to `max_attempts` attempts; between them the agent is re-run with
    deployment-faithful feedback (delivered the same way as the playbook -- written into
    the agent's project dir). Both the playbook and the feedback reach the agent via
    `inject`, so the agent contract stays `agent(bug_id, project_dir, skip_build)`.
    """
    state = load_state(state_path)
    records = []
    for bug in bugs:
        bug_id = bug["localId"]
        project_dir = Path(project_dir_for(bug_id))
        project_dir.mkdir(parents=True, exist_ok=True)
        last_run = {}

        def attempt_agent(attempt_no, feedback, _bug_id=bug_id, _project_dir=project_dir):
            # Render is holdout-filtered: only lessons from strictly-earlier bugs. Read at
            # call time, so it reflects this bug's pre-update store.
            text = maybe_compress(render_playbook(state, before_bug=_bug_id)) if inject_enabled else ""
            if feedback:
                text = (text + "\n\n## Feedback on your previous attempt\n" + feedback).strip()
            inject(text, _project_dir)
            run = agent(_bug_id, _project_dir, skip_build)
            last_run.clear()
            last_run.update(run)
            return {"diff": run.get("diff", ""), "trajectory_summary": run.get("trajectory_summary", "")}

        result = repair_with_retries(bug=bug, agent=attempt_agent, verify=verify,
                                     max_attempts=max_attempts)
        solved = result["status"] == "solved"
        final_verdict = "verified_correct" if solved else (
            result["attempts"][-1]["verdict"] if result["attempts"] else "no_changes")

        oracle_fields = {}
        playbook_version_snap = state["version"]   # snapshot before any add_heuristic bumps it
        if solved:
            accepted = result["accepted"]
            pair = result["contrastive_pair"]
            if pair:                      # failed-then-succeeded: contrastive lesson
                rejected, _ = pair
                lesson = contrastive(bug, rejected["diff"], accepted["diff"], rejected["verdict"])
            else:                         # solved first try: plain success lesson
                lesson = extract(bug, accepted["diff"], accepted.get("trajectory_summary", ""),
                                 "verified_correct")

            verdict = grade(bug, accepted["diff"])
            oracle_fields = {"oracle_label": verdict["label"],
                             "fix_image_available": verdict["fix_image_available"],
                             "n_divergences": len(verdict["divergences"])}

            if verdict["label"] == "oracle_confirmed":
                lesson["oracle"] = "confirmed"
                lesson["confidence"] = "high"
                state = add_heuristic(state, lesson, source_bug=bug_id, after_bug=bug_id)
                save_state(state, state_path)
            elif verdict["label"] == "divergent":
                pass                      # VETO: patch diverges from canonical fix; learn nothing
            else:                         # no_fix_available | oracle_error
                lesson["oracle"] = "tests_only"
                state = add_heuristic(state, lesson, source_bug=bug_id, after_bug=bug_id)
                save_state(state, state_path)

        record = {"bug_id": bug_id, "pass": pass_name, "classification": final_verdict,
                  "n_attempts": len(result["attempts"]), "playbook_version": playbook_version_snap,
                  **oracle_fields}
        append_record(ledger_path, record)

        records.append({**record, **{k: last_run[k] for k in ("injected_seen",) if k in last_run}})
    return records


def _arg_value(flag):
    """Read `--flag value` or `--flag=value` from argv; None if absent."""
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def main():
    db = Path(os.environ.get("ARVO_DB_PATH", Path(__file__).parent / "arvo_new.db"))
    bugs_ids = mruby_bug_ids(db)

    # Subset selection for cheap smoke tests before the full multi-hour run.
    bugs_arg = _arg_value("--bugs")
    if bugs_arg:
        wanted = {int(x) for x in bugs_arg.split(",")}
        bugs_ids = [b for b in bugs_ids if b in wanted]
    limit = _arg_value("--limit")
    if limit:
        bugs_ids = bugs_ids[:int(limit)]

    from build_instance import load_bug
    bugs = [load_bug(b) for b in bugs_ids]

    base = Path(__file__).parent
    pb_dir = base / "playbook"
    learn_dir = base / "results" / "learn"
    project_dir_for = lambda bid: Path.home() / ".arvo-oss-crs" / str(bid) / "project"

    pass_name = os.environ.get("LEARN_PASS", "treatment")
    inject_enabled = pass_name == "treatment"
    print(f"[learn_loop] pass={pass_name} inject={inject_enabled} "
          f"bugs={len(bugs)} max_attempts={os.environ.get('LEARN_MAX_ATTEMPTS', '5')}")
    run_pass(
        bugs=bugs, pass_name=pass_name, inject_enabled=inject_enabled,
        state_path=pb_dir / f"playbook_state_{pass_name}.json",
        ledger_path=learn_dir / "ledger.jsonl",
        project_dir_for=project_dir_for,
        max_attempts=int(os.environ.get("LEARN_MAX_ATTEMPTS", "5")),
        skip_build="--skip-build" in sys.argv,
    )


if __name__ == "__main__":
    main()
