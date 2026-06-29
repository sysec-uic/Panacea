"""Chronological self-improving repair loop for mruby ARVO bugs.

Per bug (in localId order): render the holdout-filtered playbook, optionally
inject it, run the agent, verify, log, and -- only on verified_correct -- extract a
heuristic and add it to the store AFTER the bug has been evaluated.
"""
import os
import sys
from pathlib import Path

from playbook_store import load_state, save_state, add_heuristic, render_playbook
from injector import inject
from curator import maybe_compress
from ledger import append_record
from mruby_bugs import mruby_bug_ids


def _default_agent(bug_id, project_dir, skip_build):
    """Real agent: drive OSS-CRS, then return the chosen patch + trajectory tail."""
    from arvo_oss_crs import run_oss_crs
    summary = run_oss_crs(bug_id, skip_build=skip_build)
    results_dir = Path(__file__).parent / "results" / str(bug_id)
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


def run_pass(*, bugs, pass_name, inject_enabled, state_path, ledger_path,
             project_dir_for, agent=_default_agent, verify=_default_verify,
             extract=_default_extract, skip_build=False):
    """Run one full pass over `bugs` (already in chronological order)."""
    state = load_state(state_path)
    records = []
    for bug in bugs:
        bug_id = bug["localId"]
        project_dir = Path(project_dir_for(bug_id))
        project_dir.mkdir(parents=True, exist_ok=True)

        # Holdout-filtered render: only lessons from strictly-earlier bugs.
        playbook = render_playbook(state, before_bug=bug_id)
        if inject_enabled:
            inject(maybe_compress(playbook), project_dir)

        run = agent(bug_id, project_dir, skip_build)
        diff = run.get("diff", "")
        verdict = verify(bug_id, diff)["classification"] if diff.strip() else "no_changes"

        record = {"bug_id": bug_id, "pass": pass_name, "classification": verdict,
                  "playbook_version": state["version"]}
        append_record(ledger_path, record)

        if verdict == "verified_correct":
            heuristic = extract(bug, diff, run.get("trajectory_summary", ""), verdict)
            state = add_heuristic(state, heuristic, source_bug=bug_id, after_bug=bug_id)
            save_state(state, state_path)

        records.append({**record, **{k: run[k] for k in ("injected_seen",) if k in run}})
    return records


def main():
    db = Path(os.environ.get("ARVO_DB_PATH", Path(__file__).parent / "arvo_new.db"))
    bugs_ids = mruby_bug_ids(db)
    from build_instance import load_bug
    bugs = [load_bug(b) for b in bugs_ids]

    base = Path(__file__).parent
    pb_dir = base / "playbook"
    learn_dir = base / "results" / "learn"
    project_dir_for = lambda bid: Path.home() / ".arvo-oss-crs" / str(bid) / "project"

    pass_name = os.environ.get("LEARN_PASS", "treatment")
    inject_enabled = pass_name == "treatment"
    run_pass(
        bugs=bugs, pass_name=pass_name, inject_enabled=inject_enabled,
        state_path=pb_dir / f"playbook_state_{pass_name}.json",
        ledger_path=learn_dir / "ledger.jsonl",
        project_dir_for=project_dir_for,
        skip_build="--skip-build" in sys.argv,
    )


if __name__ == "__main__":
    main()
