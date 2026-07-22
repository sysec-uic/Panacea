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
from ledger import append_record, read_records
from mruby_bugs import mruby_bug_ids
from repair_loop import repair_with_retries
from attempt_checkpoint import read_checkpoint, append_checkpoint, clear_checkpoint


RESULTS_BASE = Path(__file__).parent / "results"


def _agent_results_dir(bug_id):
    _pass = os.environ.get("LEARN_PASS", "")
    return RESULTS_BASE / _pass / str(bug_id) if _pass else RESULTS_BASE / str(bug_id)


def _default_agent(bug_id, project_dir, skip_build, abort_controller=None, on_phase=None,
                   on_line=None):
    """Real agent: drive OSS-CRS, then return the chosen patch + trajectory tail.

    Only patches listed in THIS run's summary count. Globbing the results dir
    resurrected stale oss_crs_patch_*.diff files from earlier runs whenever the
    agent produced nothing, so a dead run got verified (and fed back on) as if it
    had emitted the old patch.

    `abort_controller`, `on_phase`, and `on_line` are passed straight through to
    run_oss_crs -- see _make_agent, which binds real ones in for a live-status-panel
    run.
    """
    import arvo_oss_crs
    summary = arvo_oss_crs.run_oss_crs(bug_id, skip_build=skip_build,
                                       abort_controller=abort_controller, on_phase=on_phase,
                                       on_line=on_line)
    results_dir = _agent_results_dir(bug_id)
    patch_files = [Path(p) for p in summary.get("patch_files") or []]
    diff = patch_files[0].read_text() if patch_files and patch_files[0].exists() else ""
    log = results_dir / "oss_crs_claude_stdout.log"
    trajectory = "\n".join(log.read_text().splitlines()[-80:]) if log.exists() else ""
    # verify_fix reads results/<id>/patch.diff; bridge the OSS-CRS naming.
    if diff:
        results_dir.mkdir(parents=True, exist_ok=True)
        (results_dir / "patch.diff").write_text(diff)
    return {"diff": diff, "trajectory_summary": trajectory, "summary": summary,
            "timed_out": summary.get("timed_out", False),
            "check_required": summary.get("check_required", False),
            "check_passed": summary.get("check_passed", False),
            "usage_limit": summary.get("usage_limit"),
            "aborted": summary.get("aborted", False)}


def _make_agent(abort_controller=None, on_phase=None, before_attempt=None, on_line=None):
    """Bind abort_controller/on_phase/before_attempt/on_line into a fresh
    agent(bug_id, project_dir, skip_build) callable, keeping that 3-arg contract
    stable for every existing caller/test (run_pass's attempt_agent never needs to
    know about any of these). `before_attempt(bug_id)`, if given, fires once per
    agent() call -- each one is a fresh run_oss_crs invocation, so a PhaseTracker
    resets here."""
    def agent(bug_id, project_dir, skip_build):
        if before_attempt is not None:
            before_attempt(bug_id)
        return _default_agent(bug_id, project_dir, skip_build,
                              abort_controller=abort_controller, on_phase=on_phase,
                              on_line=on_line)
    return agent


PHASE_LABELS = {
    "prepare": "prepare environment",
    "build": "build target",
    "agent": "running agent",
    "verify": "verify fix · rebuild + PoC + rake test",
    "grade": "differential oracle · 6 probes + PoC",
}
PHASE_ORDER = ["prepare", "build", "agent", "verify", "grade"]


class PhaseTracker:
    """Translates arvo_oss_crs's on_phase(key, event) callbacks into live_status
    Phase updates for one LiveStatus panel. Call reset_for_bug(bug_id) at the start
    of each attempt (each agent() call is one fresh run_oss_crs invocation, so
    phases always restart), then pass .on_phase as the on_phase= callback.

    Kept independent of live_status's Phase/PhaseStatus imports at module scope so
    this stays easy to unit test without rich/terminal machinery -- imported lazily
    inside the methods that need it.
    """

    def __init__(self, status):
        self.status = status
        self._phases = []
        self._start_times = {}
        self._attempt_counts = {}

    def reset_for_bug(self, bug_id) -> int:
        from live_status import Phase
        n = self._attempt_counts.get(bug_id, 0) + 1
        self._attempt_counts[bug_id] = n
        self._phases = [Phase(PHASE_LABELS[k]) for k in PHASE_ORDER]
        self._start_times = {}
        self.status.set_phases(list(self._phases))
        return n

    def on_phase(self, key, event) -> None:
        import time
        from live_status import Phase, PhaseStatus
        if key not in PHASE_ORDER:
            return
        idx = PHASE_ORDER.index(key)
        if event == "start":
            self._start_times[key] = time.time()
            self._phases[idx] = Phase(PHASE_LABELS[key], PhaseStatus.ACTIVE)
        elif event == "done":
            elapsed = time.time() - self._start_times.get(key, time.time())
            self._phases[idx] = Phase(PHASE_LABELS[key], PhaseStatus.DONE, f"{elapsed:.0f}s")
        self.status.set_phases(list(self._phases))


def pass_tallies(ledger_path):
    """[Tally("control", verified, total), Tally("treatment", ...)] for the panel's
    footer line, straight from the ledger -- no new bookkeeping, just counting."""
    from live_status import Tally
    try:
        records = read_records(ledger_path)
    except (OSError, ValueError):
        records = []
    tallies = []
    for pass_name in ("control", "treatment"):
        rows = [r for r in records if r.get("pass") == pass_name]
        verified = sum(1 for r in rows if r.get("classification") == "verified_correct")
        tallies.append(Tally(pass_name, verified, len(rows)))
    return tallies


def playbook_stat(state_path):
    """{'playbook': 'v7 · 14 heuristics'} for the panel's stat line, or {} if
    the pass doesn't inject (control) or the state can't be read."""
    try:
        state = load_state(state_path)
    except (OSError, ValueError, KeyError):
        return {}
    heuristics = state.get("heuristics", [])
    return {"playbook": f"v{state.get('version', 0)} · {len(heuristics)} heuristics"}


def _default_verify(bug_id, diff, quiet=False):
    """Real verification: rebuild in a fresh -vul container, re-run the PoC, run the
    correctness gate. verify_fix reads results/<pass>/<id>/patch.diff, which
    _default_agent bridges from the OSS-CRS patch naming. `quiet` is passed straight
    through to verify_fix.verify -- see _make_verify."""
    if not diff.strip():
        return {"classification": "no_changes"}
    import verify_fix
    return verify_fix.verify(bug_id, quiet=quiet)


def _make_verify(on_phase=None):
    """Wrap _default_verify with "verify" phase start/done timing for the live status
    panel. verify_fix knows nothing about phases itself -- this just brackets the
    (already synchronous, already slow) call, same as _make_agent does for
    run_oss_crs's own on_phase callbacks. Only called when there's a real diff to
    check (repair_loop short-circuits verify() entirely on an empty/unchecked diff),
    so the phase legitimately stays pending on those attempts.

    Also passes quiet=True through to verify_fix (whenever a panel is actually up,
    i.e. on_phase is given) so its own prints don't land straight in the terminal
    and corrupt the panel's redraw -- the same reason arvo_oss_crs.py suppresses its
    own prints via `_log`/`live_mode` for the prepare/build/agent phases."""
    def verify(bug_id, diff):
        if on_phase is not None:
            on_phase("verify", "start")
        try:
            return _default_verify(bug_id, diff, quiet=on_phase is not None)
        finally:
            if on_phase is not None:
                on_phase("verify", "done")
    return verify


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


def _make_grade(on_phase=None):
    """Wrap _default_grade with "grade" phase start/done timing, mirroring
    _make_verify. Only fires once per bug -- run_pass calls grade() a single time,
    after a solve -- so the row stays pending on bugs that never got fixed."""
    def grade(bug, diff):
        if on_phase is not None:
            on_phase("grade", "start")
        try:
            return _default_grade(bug, diff)
        finally:
            if on_phase is not None:
                on_phase("grade", "done")
    return grade


def run_pass(*, bugs, pass_name, inject_enabled, state_path, ledger_path,
             project_dir_for, agent=_default_agent, verify=_default_verify,
             extract=_default_extract, contrastive=_default_contrastive,
             grade=_default_grade, max_attempts=5, skip_build=False,
             checkpoint_path_for=None):
    """Run one full pass over `bugs` (already in chronological order).

    Each bug gets up to `max_attempts` attempts; between them the agent is re-run with
    deployment-faithful feedback (delivered the same way as the playbook -- written into
    the agent's project dir). Both the playbook and the feedback reach the agent via
    `inject`, so the agent contract stays `agent(bug_id, project_dir, skip_build)`.

    `checkpoint_path_for(bug_id) -> Path | None`, if given, enables attempt-level resume:
    each attempt is durably checkpointed as it completes, so a run killed mid-bug (e.g. a
    usage cap) picks back up at the next attempt on re-run instead of restarting the bug's
    whole `max_attempts` budget. Bug-level resume (the `done` set below) already covers a
    bug that's fully finished; this covers one still in progress. Off by default so
    existing callers/tests are unaffected.
    """
    state = load_state(state_path)
    # Resume: a bug already recorded in the ledger for this pass is done -- its
    # heuristic (if any) is already in the loaded state. Skip it so a crash never
    # discards completed agent runs and re-runs don't re-pay for solved bugs.
    done = {r["bug_id"] for r in read_records(ledger_path) if r.get("pass") == pass_name}
    records = []
    for bug in bugs:
        bug_id = bug["localId"]
        if bug_id in done:
            print(f"[{bug_id}] already recorded for pass={pass_name}; skipping (resume)")
            continue
        # Per-bug crash isolation: agent()/verify()/grade() shell out with check=True
        # (build-target, docker run, extract_poc), so any one bug's build failure or
        # infra hiccup raises. Without this guard that exception aborts the WHOLE
        # campaign and the in-progress bug leaves no ledger entry -- exactly the Jul 17
        # loss where 439279102 vanished and no later bug ran. Record it as `error`
        # (so resume skips it and it's diagnosable) and move on to the next bug.
        try:
            project_dir = Path(project_dir_for(bug_id))
            project_dir.mkdir(parents=True, exist_ok=True)
            last_run = {}
            total_tokens: dict = {}

            checkpoint_path = checkpoint_path_for(bug_id) if checkpoint_path_for else None
            resume_attempts = read_checkpoint(checkpoint_path) if checkpoint_path else []
            resume_feedback = resume_attempts[-1].get("feedback_for_next", "") if resume_attempts else ""
            for prior in resume_attempts:
                for k, v in prior.get("tokens", {}).items():
                    total_tokens[k] = total_tokens.get(k, 0) + v
            if resume_attempts:
                print(f"[{bug_id}] resuming pass={pass_name} at attempt {len(resume_attempts) + 1} "
                      f"({len(resume_attempts)} attempt(s) already checkpointed)")

            def on_attempt(record, _checkpoint_path=checkpoint_path):
                if _checkpoint_path is not None:
                    tokens = dict(last_run.get("summary", {}).get("tokens", {}))
                    append_checkpoint(_checkpoint_path, {**record, "tokens": tokens})

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
                for k, v in run.get("summary", {}).get("tokens", {}).items():
                    total_tokens[k] = total_tokens.get(k, 0) + v
                return {"diff": run.get("diff", ""), "trajectory_summary": run.get("trajectory_summary", ""),
                        "timed_out": run.get("timed_out", False),
                        "check_required": run.get("check_required", False),
                        "check_passed": run.get("check_passed", False),
                        "usage_limit": run.get("usage_limit"),
                        "aborted": run.get("aborted", False)}

            result = repair_with_retries(bug=bug, agent=attempt_agent, verify=verify,
                                         max_attempts=max_attempts,
                                         resume_attempts=resume_attempts,
                                         resume_feedback=resume_feedback,
                                         on_attempt=on_attempt)

            if result["status"] == "interrupted":
                # A usage cap or a user-requested abort cut the agent off mid-attempt,
                # not a genuine failure -- that attempt was never checkpointed
                # (repair_with_retries left `attempts` untouched), so there's nothing
                # to write to the ledger and whatever real attempts already exist stay
                # on disk for the next run to resume into. Either way the pass stops
                # here rather than grinding through the rest of `bugs` (a usage cap
                # would hit the next bug identically; an abort means the user is done).
                if result.get("aborted"):
                    reason = "aborted by user"
                else:
                    info = result.get("usage_limit") or {}
                    reset_msg = f" ({info['resets_at_human']})" if info.get("resets_at_human") else ""
                    reason = f"usage limit hit{reset_msg}"
                print(f"[{bug_id}] {reason} -- stopping pass={pass_name}, "
                      f"{len(result['attempts'])} real attempt(s) already checkpointed")
                break

            solved = result["status"] == "solved"
            final_verdict = "verified_correct" if solved else (
                result["attempts"][-1]["verdict"] if result["attempts"] else "no_changes")

            oracle_fields = {}
            playbook_version_snap = state["version"]   # snapshot before any add_heuristic bumps it
            verdict = None
            if solved:
                # Oracle grade first: it's a Docker differential test (no LLM), so it can't
                # rate-limit, and its label feeds both the ledger record and the veto below.
                verdict = grade(bug, result["accepted"]["diff"])
                oracle_fields = {"oracle_label": verdict["label"],
                                 "fix_image_available": verdict["fix_image_available"],
                                 "n_divergences": len(verdict["divergences"]),
                                 # Keep the grader's error string, or oracle_error
                                 # records are undiagnosable after the fact.
                                 **({"oracle_error": verdict["error"]} if "error" in verdict else {})}

            # Record the outcome BEFORE the fragile LLM extraction below. A solved bug's
            # repair is expensive; if the extractor rate-limits we must not discard it.
            # Once recorded, the `done` resume-set skips this bug on the next run.
            record = {"bug_id": bug_id, "pass": pass_name, "classification": final_verdict,
                      "n_attempts": len(result["attempts"]), "playbook_version": playbook_version_snap,
                      **oracle_fields, **({"tokens": total_tokens} if total_tokens else {})}
            append_record(ledger_path, record)
            if checkpoint_path is not None:
                clear_checkpoint(checkpoint_path)
            records.append({**record, **{k: last_run[k] for k in ("injected_seen",) if k in last_run}})
        except Exception as exc:
            import traceback
            traceback.print_exc()
            err = {"bug_id": bug_id, "pass": pass_name, "classification": "error",
                   "n_attempts": 0, "error": f"{type(exc).__name__}: {exc}"}
            append_record(ledger_path, err)
            records.append(err)
            print(f"[{bug_id}] crashed ({type(exc).__name__}: {exc}); recorded as error, "
                  f"continuing to next bug")
            continue

        # Learn from all solved bugs. Extraction (an LLM call) runs AFTER the ledger
        # write, so a failure here costs at most this bug's lesson -- never the
        # completed repair, which is already durably recorded above. Guarded for the
        # same reason: a rate-limited or crashing extractor must not abort the campaign.
        if inject_enabled and solved:
            try:
                accepted = result["accepted"]
                pair = result["contrastive_pair"]
                if pair:                      # failed-then-succeeded: contrastive lesson
                    rejected, _ = pair
                    lesson = contrastive(bug, rejected["diff"], accepted["diff"], rejected["verdict"])
                else:                         # solved first try: plain success lesson
                    lesson = extract(bug, accepted["diff"], accepted.get("trajectory_summary", ""),
                                     "verified_correct")

                if verdict["label"] == "oracle_confirmed":
                    lesson["oracle"] = "confirmed"
                    lesson["confidence"] = "high"
                else:                         # divergent | no_fix_available | oracle_error
                    lesson["oracle"] = "tests_only"
                state = add_heuristic(state, lesson, source_bug=bug_id, after_bug=bug_id)
                save_state(state, state_path)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"[{bug_id}] lesson extraction failed ({type(exc).__name__}: {exc}); "
                      f"repair already recorded, skipping lesson")
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
    ledger_path = learn_dir / "ledger.jsonl"
    project_dir_for = lambda bid: Path.home() / ".arvo-oss-crs" / str(bid) / "project"

    pass_name = os.environ.get("LEARN_PASS", "treatment")
    inject_enabled = pass_name == "treatment"
    max_attempts = int(os.environ.get("LEARN_MAX_ATTEMPTS", "5"))
    state_path = pb_dir / f"playbook_state_{pass_name}.json"
    print(f"[learn_loop] pass={pass_name} inject={inject_enabled} "
          f"bugs={len(bugs)} max_attempts={max_attempts}")
    checkpoint_path_for = lambda bid: RESULTS_BASE / pass_name / str(bid) / "attempts.jsonl"

    common_kwargs = dict(
        bugs=bugs, pass_name=pass_name, inject_enabled=inject_enabled,
        state_path=state_path, ledger_path=ledger_path,
        project_dir_for=project_dir_for, max_attempts=max_attempts,
        skip_build="--skip-build" in sys.argv,
        checkpoint_path_for=checkpoint_path_for,
    )

    if os.environ.get("LEARN_LIVE_UI") != "1":
        run_pass(agent=_default_agent, **common_kwargs)
        return

    # LEARN_LIVE_UI=1: replace raw OSS-CRS log spam with a live status panel.
    # Ctrl-C is remapped to the SAME graceful stop as pressing q -- both just call
    # controller.abort(), so nothing downstream cares which one fired. Only
    # installed here, never at import time, so importing this module (e.g. for
    # tests) never touches the process's real SIGINT handling.
    import signal
    import arvo_oss_crs
    from live_status import LiveStatus

    controller = arvo_oss_crs.AbortController()
    status = LiveStatus(command=" ".join(sys.argv) or "learn_loop.py",
                        subject=f"pass={pass_name}", on_abort=controller.abort)
    tracker = PhaseTracker(status)

    def before_attempt(bug_id):
        n = tracker.reset_for_bug(bug_id)
        idx = next((i for i, b in enumerate(bugs) if b["localId"] == bug_id), None)
        status.position = (idx + 1, len(bugs_ids)) if idx is not None else None
        status.subject = f"bug {bug_id} · {pass_name} · attempt {n}/{max_attempts}"
        status.set_tallies(pass_tallies(ledger_path))
        status.set_stats(playbook_stat(state_path) if inject_enabled else {})

    agent = _make_agent(abort_controller=controller, on_phase=tracker.on_phase,
                        before_attempt=before_attempt, on_line=status.feed_raw)
    verify = _make_verify(on_phase=tracker.on_phase)
    grade = _make_grade(on_phase=tracker.on_phase)

    old_sigint = signal.signal(signal.SIGINT, lambda signum, frame: controller.abort())
    try:
        with status:
            run_pass(agent=agent, verify=verify, grade=grade, **common_kwargs)
    finally:
        signal.signal(signal.SIGINT, old_sigint)


if __name__ == "__main__":
    main()
