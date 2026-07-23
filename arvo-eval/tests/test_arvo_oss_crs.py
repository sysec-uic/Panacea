"""Compose-file selection, preflight reachability, and token-count parsing."""
import json
from pathlib import Path

import pytest

import arvo_oss_crs


def test_compose_file_defaults_to_local(monkeypatch):
    monkeypatch.delenv("OSS_CRS_COMPOSE_FILE", raising=False)
    assert arvo_oss_crs._compose_file() == (
        arvo_oss_crs.OSS_CRS_DIR / "example/crs-claude-code/compose-local.yaml")


def test_compose_file_env_override(monkeypatch):
    monkeypatch.setenv("OSS_CRS_COMPOSE_FILE", "/tmp/other-compose.yaml")
    assert arvo_oss_crs._compose_file() == Path("/tmp/other-compose.yaml")


def test_uses_local_model_true_for_local_compose():
    assert arvo_oss_crs._uses_local_model(
        Path("/x/example/crs-claude-code/compose-local.yaml")) is True


def test_uses_local_model_false_for_oauth_compose():
    assert arvo_oss_crs._uses_local_model(
        Path("/x/example/crs-claude-code/compose-oauth.yaml")) is False


def test_bug_workdir_respects_learn_pass(monkeypatch):
    # The agent's project tree must be namespaced by pass so a control and a
    # treatment run of the same bug never share (and clobber) a working dir --
    # the same isolation verify_fix.results_dir() already applies to results/.
    monkeypatch.setattr(arvo_oss_crs, "PROJECTS_DIR", Path("/wk"))
    monkeypatch.setenv("LEARN_PASS", "control")
    assert arvo_oss_crs.bug_workdir(439237851) == Path("/wk/control/439237851")
    monkeypatch.setenv("LEARN_PASS", "treatment")
    assert arvo_oss_crs.bug_workdir(439237851) == Path("/wk/treatment/439237851")
    # Unset LEARN_PASS keeps the flat path for standalone single-bug runs.
    monkeypatch.delenv("LEARN_PASS", raising=False)
    assert arvo_oss_crs.bug_workdir(439237851) == Path("/wk/439237851")


def test_reachability_passes_when_endpoint_up():
    calls = []
    # A stub opener that "succeeds" records the call and returns without raising.
    arvo_oss_crs.check_local_model_reachable(
        "http://172.17.0.1:8080/v1/models",
        opener=lambda url, timeout: calls.append((url, timeout)))
    assert calls == [("http://172.17.0.1:8080/v1/models", 4.0)]


def test_reachability_raises_actionable_error_when_endpoint_down():
    def dead(url, timeout):
        raise OSError("Connection refused")

    with pytest.raises(RuntimeError) as exc:
        arvo_oss_crs.check_local_model_reachable(
            "http://172.17.0.1:8080/v1/models", opener=dead)
    msg = str(exc.value)
    assert "unreachable" in msg
    assert "172.17.0.1:8080:localhost:8080" in msg   # tells the user how to fix it


def test_wait_returns_immediately_when_endpoint_up():
    sleeps = []
    arvo_oss_crs.wait_for_local_model(
        "http://172.17.0.1:8080/v1/models",
        opener=lambda url, timeout: None,
        sleep=sleeps.append)
    assert sleeps == []


def test_wait_blocks_until_tunnel_comes_back(capsys):
    # Tunnel is down for the first two probes, then recovers. The wait must
    # survive the outage (no exception), sleeping between probes, and return
    # once the endpoint answers.
    attempts = []

    def flaky(url, timeout):
        attempts.append(url)
        if len(attempts) <= 2:
            raise OSError("Connection refused")

    sleeps = []
    arvo_oss_crs.wait_for_local_model(
        "http://172.17.0.1:8080/v1/models",
        poll_seconds=7, opener=flaky, sleep=sleeps.append)
    assert len(attempts) == 3
    assert sleeps == [7, 7]
    out = capsys.readouterr().out
    assert "172.17.0.1:8080:localhost:8080" in out   # tells the user how to restart the tunnel
    assert "reachable again" in out                  # announces recovery


# (repo, tag) pairs as `docker images` reports them; order intentionally shuffled
# because staleness must come from the epoch embedded in the tag, not list order.
DOCKER_IMAGES = [
    ("oss-crs-snapshot", "test-1783442751bt"),                                  # old
    ("crs_compose_1783791693ef-oss-crs-runner-sidecar", "latest"),              # current run
    ("oss-crs-snapshot", "test-1783791693ef"),                                  # newest
    ("oss-crs-snapshot", "build-crs-claude-code-default-build-1783791693ef"),   # newest
    ("oss-crs-snapshot", "content-6ff03574d8b02273"),                           # cache: keep
    ("oss-crs-snapshot", "build-crs-claude-code-default-build-1783442751bt"),   # old
    ("oss-crs-snapshot", "test-1783757189oo"),                                  # 2nd newest
    ("crs_compose_1783528430al-oss-crs-runner-sidecar", "latest"),              # dead run
    ("crs_compose_1783528430al-crs-claude-code_patcher", "latest"),             # dead run
    ("oss-crs-snapshot", "build-crs-claude-code-default-build-1783757189oo"),   # 2nd newest
    ("n132/arvo", "439237851-vul"),                                             # not ours
]


def test_stale_images_keeps_newest_snapshots_per_kind():
    stale = arvo_oss_crs.stale_docker_images(DOCKER_IMAGES, keep=2)
    assert "oss-crs-snapshot:test-1783442751bt" in stale
    assert "oss-crs-snapshot:build-crs-claude-code-default-build-1783442751bt" in stale
    assert "oss-crs-snapshot:test-1783791693ef" not in stale
    assert "oss-crs-snapshot:test-1783757189oo" not in stale
    assert "oss-crs-snapshot:build-crs-claude-code-default-build-1783791693ef" not in stale


def test_stale_images_never_touches_content_cache_or_foreign_images():
    stale = arvo_oss_crs.stale_docker_images(DOCKER_IMAGES, keep=2)
    assert not any("content-" in s for s in stale)
    assert not any(s.startswith("n132/arvo") for s in stale)


def test_stale_images_deletes_compose_sets_of_dead_runs_only():
    stale = arvo_oss_crs.stale_docker_images(DOCKER_IMAGES, keep=2)
    assert "crs_compose_1783528430al-oss-crs-runner-sidecar:latest" in stale
    assert "crs_compose_1783528430al-crs-claude-code_patcher:latest" in stale
    assert not any(s.startswith("crs_compose_1783791693ef") for s in stale)


def test_cleanup_docker_images_rmis_stale_and_prunes(monkeypatch):
    monkeypatch.delenv("OSS_CRS_DOCKER_CLEANUP", raising=False)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R:
            stdout = "\n".join(f"{r} {t}" for r, t in DOCKER_IMAGES)
            returncode = 0
        return R()

    removed = arvo_oss_crs.cleanup_docker_images(keep=2, run=fake_run)
    assert len(removed) == 4
    rmi_calls = [c for c in calls if c[:2] == ["docker", "rmi"]]
    # No -f: an image still used by a container must survive, not be torn away.
    assert all(len(c) == 3 for c in rmi_calls)
    assert {c[2] for c in rmi_calls} == set(removed)
    assert ["docker", "image", "prune", "-f"] in calls


def test_cleanup_docker_images_disabled_by_env(monkeypatch):
    monkeypatch.setenv("OSS_CRS_DOCKER_CLEANUP", "0")
    def fail_run(cmd, **kw):
        raise AssertionError("must not touch docker when disabled")
    assert arvo_oss_crs.cleanup_docker_images(run=fail_run) == []


def test_run_timeout_unset_is_none(monkeypatch):
    monkeypatch.delenv("OSS_CRS_RUN_TIMEOUT", raising=False)
    assert arvo_oss_crs._run_timeout() is None


def test_run_timeout_env_parsed_as_seconds(monkeypatch):
    monkeypatch.setenv("OSS_CRS_RUN_TIMEOUT", "7200")
    assert arvo_oss_crs._run_timeout() == 7200.0


def test_agent_runs_to_completion_without_timeout():
    # No cap: run once, no teardown, and report the run did NOT time out.
    calls = []
    teardowns = []

    def fake_run(cmd, **kw):
        calls.append(kw.get("timeout", "MISSING"))

    timed_out = arvo_oss_crs._run_agent_with_timeout(
        ["uv", "run", "oss-crs"], cwd="/x", timeout=None,
        run=fake_run, teardown=lambda: teardowns.append(True))
    assert timed_out is False
    assert calls == [None]          # the cap is threaded through to subprocess.run
    assert teardowns == []          # nothing to tear down on a clean finish


def test_agent_timeout_tears_down_containers_and_reports_timed_out():
    # A run that blows the cap: subprocess.run raises TimeoutExpired (it SIGKILLs the
    # oss-crs process), but the orphaned compose containers survive -- so we must tear
    # them down and tell the caller this was a no-patch, timed-out attempt.
    import subprocess
    teardowns = []

    def slow_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    timed_out = arvo_oss_crs._run_agent_with_timeout(
        ["uv", "run", "oss-crs"], cwd="/x", timeout=1.0,
        run=slow_run, teardown=lambda: teardowns.append(True))
    assert timed_out is True
    assert teardowns == [True]


def test_terminate_crs_run_force_removes_live_containers():
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R:
            stdout = "abc123\ndef456\n" if cmd[:2] == ["docker", "ps"] else ""
            returncode = 0
        return R()

    removed = arvo_oss_crs.terminate_crs_run(run=fake_run)
    assert removed == ["abc123", "def456"]
    # Filtered to our compose containers by name prefix, then force-removed.
    ps = next(c for c in calls if c[:2] == ["docker", "ps"])
    assert "name=crs_compose" in ps
    assert ["docker", "rm", "-f", "abc123", "def456"] in calls


def test_terminate_crs_run_noop_when_nothing_running():
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R:
            stdout = ""
            returncode = 0
        return R()

    assert arvo_oss_crs.terminate_crs_run(run=fake_run) == []
    # No containers => no destructive `docker rm` is issued.
    assert not any(c[:3] == ["docker", "rm", "-f"] for c in calls)


def test_find_shared_dir_returns_newest_run(tmp_path, monkeypatch):
    # SHARED_DIR is the rw bind mount backing the agent's /OSS_CRS_SHARED_DIR -- the
    # live channel for check-patch. Path mirrors oss-crs get_shared_dir.
    import os as _os, time as _time
    monkeypatch.setattr(arvo_oss_crs, "OSS_CRS_DIR", tmp_path)
    base = tmp_path / ".oss-crs-workdir" / "crs_compose"

    def mk(run):
        p = (base / "c1" / "address" / "runs" / run / "crs" / "crs-claude-code"
             / "tgt" / "SHARED_DIR" / "mruby_proto_fuzzer")
        p.mkdir(parents=True)
        return p

    mk("100")
    newest = mk("200")
    _os.utime(newest, (_time.time() + 100, _time.time() + 100))
    assert arvo_oss_crs.find_shared_dir("asan") == newest


def test_find_shared_dir_none_when_no_run(tmp_path, monkeypatch):
    monkeypatch.setattr(arvo_oss_crs, "OSS_CRS_DIR", tmp_path)
    assert arvo_oss_crs.find_shared_dir("asan") is None


def test_find_shared_dir_ignores_dirs_older_than_reference(tmp_path, monkeypatch):
    # Race guard: a SHARED_DIR left by a PRIOR/killed campaign must not be latched.
    # Only accept the current run's dir, i.e. created after the service started.
    import os as _os, time as _time
    monkeypatch.setattr(arvo_oss_crs, "OSS_CRS_DIR", tmp_path)
    base = tmp_path / ".oss-crs-workdir" / "crs_compose"

    def mk(run):
        p = (base / "c1" / "address" / "runs" / run / "crs" / "crs-claude-code"
             / "tgt" / "SHARED_DIR" / "mruby_proto_fuzzer")
        p.mkdir(parents=True)
        return p

    stale = mk("old")
    _os.utime(stale, (1000, 1000))          # long in the past
    ref = 2000
    # Only a stale dir exists and it predates the reference -> nothing to latch.
    assert arvo_oss_crs.find_shared_dir("asan", newer_than=ref) is None
    # Once the current run's dir appears (newer than ref), it's selected over the stale one.
    current = mk("new")
    _os.utime(current, (3000, 3000))
    assert arvo_oss_crs.find_shared_dir("asan", newer_than=ref) == current


def test_find_target_source_dir_isolates_current_build(tmp_path, monkeypatch):
    # Injection race guard: the workdir accumulates a target-source per build across every
    # run and never cleans them; picking the global max-mtime landed HEURISTICS.md in the
    # wrong run's dir -- the agent saw "No HEURISTICS.md" and treatment silently degraded to
    # control. Isolate THIS run's freshly-built dir via a before-snapshot (exclude) + the
    # build-start time (newer_than), so a stale dir can't win even if something bumps its mtime.
    import os as _os
    monkeypatch.setattr(arvo_oss_crs, "OSS_CRS_DIR", tmp_path)
    base = tmp_path / ".oss-crs-workdir" / "crs_compose"

    def mk(build, mtime):
        p = base / "c1" / "memory" / "builds" / build / "targets" / "t" / "target-source"
        p.mkdir(parents=True)
        _os.utime(p, (mtime, mtime))
        return p

    stale = mk("old", 1000)                       # a prior run's leftover dir
    before = set(arvo_oss_crs._target_source_glob("msan"))
    build_start = 2000
    current = mk("new", 3000)                      # this run's freshly built dir
    _os.utime(stale, (9000, 9000))                # noisy workdir bumps the stale dir's mtime
    # Filtered: must select THIS build's dir despite the stale dir now being mtime-newest.
    assert arvo_oss_crs.find_target_source_dir(
        "msan", newer_than=build_start, exclude=before) == current
    # Unfiltered global-max picks the wrongly-touched stale dir -- the original bug.
    assert arvo_oss_crs.find_target_source_dir("msan") == stale


def _write_log(tmp_path, records):
    p = tmp_path / "claude_stdout.log"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def test_token_counts_keyed_on_message_id(tmp_path):
    # Local model / Claude Code CLI: usage rides on assistant messages keyed by
    # message.id, with NO top-level request_id. The same id repeats across stream
    # events (partial then final usage), so we take the max per id then sum.
    log = _write_log(tmp_path, [
        # streaming start: input known, output partial
        {"type": "assistant", "message": {"id": "resp_A",
            "usage": {"input_tokens": 67020, "output_tokens": 2}}},
        # streaming final for the SAME turn: full output — must not double-count input
        {"type": "assistant", "message": {"id": "resp_A",
            "usage": {"input_tokens": 67020, "output_tokens": 941}}},
        # a pure content delta with zeroed usage — ignored
        {"type": "assistant", "message": {"id": "resp_A",
            "usage": {"input_tokens": 0, "output_tokens": 0}}},
        # a second distinct turn
        {"type": "assistant", "message": {"id": "resp_B",
            "usage": {"input_tokens": 100, "output_tokens": 50,
                      "cache_read_input_tokens": 30, "cache_creation_input_tokens": 10}}},
    ])
    assert arvo_oss_crs.parse_token_counts(log) == {
        "input_tokens": 67020 + 100,
        "output_tokens": 941 + 50,
        "cache_read_tokens": 30,
        "cache_write_tokens": 10,
    }


def test_token_counts_backward_compatible_with_request_id(tmp_path):
    # Older logs keyed usage by top-level request_id — still supported.
    log = _write_log(tmp_path, [
        {"request_id": "req_1", "message": {"usage": {"input_tokens": 5, "output_tokens": 7}}},
        {"request_id": "req_1", "message": {"usage": {"input_tokens": 5, "output_tokens": 7}}},
        {"request_id": "req_2", "message": {"usage": {"input_tokens": 3, "output_tokens": 2}}},
    ])
    assert arvo_oss_crs.parse_token_counts(log) == {
        "input_tokens": 8, "output_tokens": 9,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }


def test_token_counts_missing_file_is_zeroed(tmp_path):
    assert arvo_oss_crs.parse_token_counts(tmp_path / "nope.log") == {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }


# --- check-patch auto-submit: promote the validated diff when the agent never submits ---

def test_resolve_autosubmit_promotes_validated_diff_when_no_agent_patch():
    # The attempt-1 loss mode: check-patch PASSed but the agent never wrote /patches/.
    promoted = arvo_oss_crs.resolve_autosubmit_patch(
        collected=[], check_passed=True, autosubmit_diff="DIFF that passed")
    assert promoted == "DIFF that passed"


def test_resolve_autosubmit_keeps_agent_patch_when_present():
    # The agent's own submission always wins; auto-submit is only a fallback.
    promoted = arvo_oss_crs.resolve_autosubmit_patch(
        collected=[Path("oss_crs_patch_0.diff")], check_passed=True, autosubmit_diff="X")
    assert promoted is None


def test_resolve_autosubmit_none_without_a_pass():
    # No check-patch PASS this run -> nothing validated to fall back on.
    assert arvo_oss_crs.resolve_autosubmit_patch(
        collected=[], check_passed=False, autosubmit_diff="X") is None


def test_resolve_autosubmit_none_when_saved_diff_empty():
    assert arvo_oss_crs.resolve_autosubmit_patch(
        collected=[], check_passed=True, autosubmit_diff="") is None
    assert arvo_oss_crs.resolve_autosubmit_patch(
        collected=[], check_passed=True, autosubmit_diff="   \n") is None


# --- injected check-patch guidance: cooperate with the base CLAUDE.md clean-src flow ---

def test_check_patch_instruction_names_the_clean_src_git_tree():
    # Fix 2 (reworked): the base CLAUDE.md is authoritative and tells the agent to
    # download-source into /work/agent/clean-src and edit there. Cooperate with that --
    # name the project's git repo INSIDE clean-src (/work/agent/clean-src/<project>) so
    # the agent stops re-discovering the layout and stops git-init-ing the wrong dir.
    text = arvo_oss_crs.check_patch_instruction("mruby")
    assert "/work/agent/clean-src/mruby" in text
    assert "check-patch" in text
    low = text.lower()
    assert "git init" in low                                   # warned against (it's already a repo)


def test_check_patch_instruction_does_not_fight_the_base_workflow():
    # It must NOT tell the agent to edit in /src (CLAUDE.md says /src is read-only
    # reference) nor forbid download-source (that IS the sanctioned setup step).
    text = arvo_oss_crs.check_patch_instruction("mruby")
    assert "/src/mruby" not in text
    assert "do not download-source" not in text.lower()
    assert "don't download-source" not in text.lower()


def test_check_patch_instruction_makes_pass_the_finish_line():
    # Fix 1: a PASS is the submission (auto-submit records it), so the agent must NOT
    # hand-write a diff, run apply-patch-build, or hunt for /patches/ path prefixes.
    text = arvo_oss_crs.check_patch_instruction("mruby")
    assert "PASS" in text
    low = text.lower()
    assert "automatically" in low or "for you" in low          # PASS is recorded for them
    assert "apply-patch-build" in low                          # steered away from the manual build
    assert "/patches/" in text                                 # tells it not to write there itself


def test_check_patch_instruction_runs_check_from_the_clean_src_repo():
    # check-patch does `git diff` from cwd, so the command must cd into the clean-src
    # project repo -- the friction that cost attempt 2 ~50 minutes.
    text = arvo_oss_crs.check_patch_instruction("mruby")
    assert "cd /work/agent/clean-src/mruby" in text
    assert "$OSS_CRS_SHARED_DIR/check-patch" in text


def test_check_patch_instruction_is_project_parameterized():
    assert "/work/agent/clean-src/openssl" in arvo_oss_crs.check_patch_instruction("openssl")
    assert "/work/agent/clean-src/mruby" not in arvo_oss_crs.check_patch_instruction("openssl")


def test_inject_orientation_inlines_briefing_into_heuristics(tmp_path, monkeypatch):
    import arvo_oss_crs
    monkeypatch.setenv("OSS_CRS_ORIENT", "1")
    monkeypatch.setattr(arvo_oss_crs, "find_target_source_dir", lambda san, newer_than=None, exclude=(): tmp_path)
    (tmp_path / "HEURISTICS.md").write_text("EXISTING PLAYBOOK\n")
    bug = {
        "localId": 439494108, "project": "mruby",
        "crash_type": "Stack-use-after-return READ 4",
        "crash_output": (
            "==7==ERROR: AddressSanitizer: stack-use-after-return on address 0x1\n"
            "    #0 0x1 in limb_addmul_1 /src/mruby/mrbgems/mruby-bigint/core/bigint.c:726:58\n"
            "SUMMARY: AddressSanitizer: stack-use-after-return bigint.c:726\n"
        ),
    }
    assert arvo_oss_crs.inject_orientation("address", bug) is True
    # ORIENTATION.md is still written as an inspectable artifact...
    assert "limb_addmul_1" in (tmp_path / "ORIENTATION.md").read_text()
    # ...but the fix: the full briefing is INLINED at the top of HEURISTICS.md (the
    # file the agent reliably reads), not behind a pointer it must choose to open.
    heur = (tmp_path / "HEURISTICS.md").read_text()
    assert heur.startswith("# Crash orientation")
    assert "limb_addmul_1" in heur                        # fault site inline
    assert "mrbgems/mruby-bigint/core/bigint.c:726" in heur
    assert "EXISTING PLAYBOOK" in heur                    # playbook preserved below


def test_inject_orientation_disabled_by_default(tmp_path, monkeypatch):
    import arvo_oss_crs
    monkeypatch.delenv("OSS_CRS_ORIENT", raising=False)
    monkeypatch.setattr(arvo_oss_crs, "find_target_source_dir", lambda san, newer_than=None, exclude=(): tmp_path)
    bug = {"localId": 1, "project": "mruby", "crash_type": "x", "crash_output": "==ERROR: ..."}
    assert arvo_oss_crs.inject_orientation("address", bug) is False
    assert not (tmp_path / "ORIENTATION.md").exists()


def test_force_edit_flag_and_recon_timeout(monkeypatch):
    import arvo_oss_crs as a
    monkeypatch.delenv("OSS_CRS_FORCE_EDIT", raising=False)
    monkeypatch.delenv("OSS_CRS_RECON_TIMEOUT", raising=False)
    assert a._force_edit_enabled() is False
    assert a._recon_timeout() == 1800.0
    monkeypatch.setenv("OSS_CRS_FORCE_EDIT", "1")
    monkeypatch.setenv("OSS_CRS_RECON_TIMEOUT", "600")
    assert a._force_edit_enabled() is True
    assert a._recon_timeout() == 600.0


def test_agent_edit_count(tmp_path):
    import arvo_oss_crs as a, json
    log = tmp_path / "claude_stdout.log"
    lines = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "let me look"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "grep x"}}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.c"}}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "b.c"}}]}},
        "this is not json",
    ]
    log.write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x for x in lines))
    assert a.agent_edit_count(log) == 2
    assert a.agent_edit_count(tmp_path / "missing.log") == 0


def test_find_agent_stdout_log(tmp_path):
    import arvo_oss_crs as a
    run_dir = tmp_path / "run"
    p = run_dir / "crs/crs-claude-code/proj/LOG_DIR/mruby_fuzzer/agent/claude_stdout.log"
    p.parent.mkdir(parents=True)
    p.write_text("{}")
    assert a.find_agent_stdout_log(run_dir) == p
    assert a.find_agent_stdout_log(tmp_path / "empty") is None


def test_live_edit_count(tmp_path, monkeypatch):
    import arvo_oss_crs as a, json
    run_dir = tmp_path / "run"
    p = run_dir / "crs/crs-claude-code/proj/LOG_DIR/mruby_fuzzer/agent/claude_stdout.log"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.c"}}]}}))
    monkeypatch.setattr(a, "find_latest_run_dir", lambda san: run_dir)
    assert a._live_edit_count("address") == 1
    monkeypatch.setattr(a, "find_latest_run_dir", lambda san: None)
    assert a._live_edit_count("address") == 0


def test_extract_root_cause(tmp_path):
    import arvo_oss_crs as a, json
    log = tmp_path / "claude_stdout.log"
    big = "The set kset_put path stores keys without a write barrier. " * 6  # >200 chars
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": big}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines))
    assert a.extract_root_cause(log).startswith("The set kset_put path")
    assert a.extract_root_cause(tmp_path / "missing.log") == ""


def test_build_forced_edit_directive():
    import arvo_oss_crs as a
    out = a.build_forced_edit_directive(
        project="mruby", root_cause="Missing write barrier in kset_put.",
        orientation="# Crash orientation\nClass: heap-use-after-free")
    # Mandate is first and unmissable
    assert out.lstrip().startswith("# FORCED EDIT")
    idx_mandate = out.index("only task")
    idx_repo = out.index("/work/agent/clean-src/mruby")
    assert idx_mandate < idx_repo               # mandate before mechanics
    assert "Missing write barrier in kset_put." in out   # agent's own words fed back
    assert "check-patch" in out
    assert "Crash orientation" in out
    # Empty analysis is safe: still a valid directive standing on crash + recipe
    out2 = a.build_forced_edit_directive(project="mruby", root_cause="", orientation="")
    assert out2.lstrip().startswith("# FORCED EDIT")
    assert "You already concluded" not in out2


def test_phase2_timeout():
    import arvo_oss_crs as a
    # Normal: hard cap 7200, phase1 ran 1800 -> 5400 remaining
    assert a.phase2_timeout(7200, 1800) == 5400
    # Floor protects a tiny remainder
    assert a.phase2_timeout(7200, 7000, floor=900) == 900
    # No hard cap -> no phase-2 cap
    assert a.phase2_timeout(None, 1800) is None


def test_inject_forced_edit_overwrites_target_source(tmp_path, monkeypatch):
    import arvo_oss_crs as a, json
    ts = tmp_path / "target-source"
    ts.mkdir()
    (ts / "HEURISTICS.md").write_text("OLD PLAYBOOK CONTENT")
    # stream log with the agent's diagnosis
    log = tmp_path / "claude_stdout.log"
    big = "Root cause: kset_put stores keys without a write barrier. " * 5
    log.write_text(json.dumps({"type": "assistant",
        "message": {"content": [{"type": "text", "text": big}]}}))
    monkeypatch.setattr(a, "find_target_source_dir", lambda san, newer_than=None, exclude=(): ts)
    monkeypatch.setattr(a, "find_latest_run_dir", lambda san: tmp_path)
    monkeypatch.setattr(a, "find_agent_stdout_log", lambda rd: log)
    monkeypatch.setattr(a, "parse_crash_output", lambda *args, **kw: None)  # no orientation
    ok = a.inject_forced_edit("address", {"localId": 1, "project": "mruby",
                                          "crash_output": "", "crash_type": ""}, "mruby",
                              newer_than=None, exclude=())
    assert ok is True
    written = (ts / "HEURISTICS.md").read_text()
    assert written.lstrip().startswith("# FORCED EDIT")
    assert "OLD PLAYBOOK CONTENT" not in written          # overwritten, not appended
    assert "kset_put stores keys" in written              # agent's diagnosis fed back


def test_edit_helpers_tolerate_non_object_json_lines(tmp_path):
    import arvo_oss_crs as a, json
    log = tmp_path / "claude_stdout.log"
    log.write_text("\n".join([
        "null", "42", "[1, 2, 3]",
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.c"}}]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Root cause: missing write barrier. " * 8}]}}),
    ]))
    assert a.agent_edit_count(log) == 1          # does not raise on null/42/list
    assert a.extract_root_cause(log).startswith("Root cause")


def test_inject_forced_edit_returns_false_when_no_target_source(monkeypatch):
    import arvo_oss_crs as a
    monkeypatch.setattr(a, "find_target_source_dir", lambda san, newer_than=None, exclude=(): None)
    ok = a.inject_forced_edit("address", {"localId": 1, "project": "mruby",
                                          "crash_output": "", "crash_type": ""}, "mruby")
    assert ok is False


def test_run_agent_recon_phase_forces_when_zero_edits():
    import arvo_oss_crs as a

    class FakeProc:
        def __init__(self): self.killed = False
        def poll(self): return None        # never exits on its own
        def kill(self): self.killed = True
        def wait(self, timeout=None): return 0

    proc = FakeProc()
    torn = []
    clock = {"t": 0.0}
    def fake_now(): clock["t"] += 10; return clock["t"]   # +10s per poll
    timed_out, forced = a._run_agent_recon_phase(
        ["run"], cwd=".", hard_cap=100, recon_cap=30,
        edit_probe=lambda: 0,                  # agent made no edits
        popen=lambda cmd, cwd: proc,
        teardown=lambda: torn.append(True),
        now=fake_now, sleep=lambda s: None)
    assert (timed_out, forced) == (False, True)
    assert proc.killed and torn == [True]


def test_run_agent_recon_phase_lets_worker_run_to_hard_cap():
    import arvo_oss_crs as a

    class FakeProc:
        def __init__(self): self.killed = False
        def poll(self): return None
        def kill(self): self.killed = True
        def wait(self, timeout=None): return 0

    proc = FakeProc()
    clock = {"t": 0.0}
    def fake_now(): clock["t"] += 10; return clock["t"]
    timed_out, forced = a._run_agent_recon_phase(
        ["run"], cwd=".", hard_cap=100, recon_cap=30,
        edit_probe=lambda: 3,                  # already editing at the recon mark
        popen=lambda cmd, cwd: proc,
        teardown=lambda: None,
        now=fake_now, sleep=lambda s: None)
    # editing -> not forced; runs until hard_cap -> timed_out
    assert (timed_out, forced) == (True, False)
    assert proc.killed


def test_run_agent_recon_phase_natural_exit():
    import arvo_oss_crs as a

    class FakeProc:
        def __init__(self): self.calls = 0
        def poll(self):
            self.calls += 1
            return None if self.calls < 2 else 0   # exits on the 2nd poll
        def kill(self): pass
        def wait(self, timeout=None): return 0

    clock = {"t": 0.0}
    def fake_now(): clock["t"] += 10; return clock["t"]
    timed_out, forced = a._run_agent_recon_phase(
        ["run"], cwd=".", hard_cap=100, recon_cap=30,
        edit_probe=lambda: 0, popen=lambda cmd, cwd: FakeProc(),
        teardown=lambda: None, now=fake_now, sleep=lambda s: None)
    assert (timed_out, forced) == (False, False)


def test_run_agent_phases_flag_off_single_call(monkeypatch):
    import arvo_oss_crs as a
    calls = []
    monkeypatch.delenv("OSS_CRS_FORCE_EDIT", raising=False)
    res = a.run_agent_phases(
        run_cmd=["run"], cwd=".", sanitizer="address", bug={"localId": 1},
        project="mruby", hard_cap=100, newer_than=None, exclude=(),
        single=lambda cmd, cwd, timeout: (calls.append(("single", timeout)) or False),
        recon=lambda **k: (_ for _ in ()).throw(AssertionError("recon must not run")),
        inject_forced=lambda *a_, **k: True,
        edit_probe=lambda: 0)
    assert res["forced_edit_triggered"] is False
    assert calls == [("single", 100)]


def test_run_agent_phases_forced_pair(monkeypatch):
    import arvo_oss_crs as a
    monkeypatch.setenv("OSS_CRS_FORCE_EDIT", "1")
    calls = []
    injected = []
    res = a.run_agent_phases(
        run_cmd=["run"], cwd=".", sanitizer="address", bug={"localId": 1},
        project="mruby", hard_cap=7200, newer_than=None, exclude=(),
        single=lambda cmd, cwd, timeout: calls.append(("single", timeout)) or False,
        recon=lambda **k: (False, True),          # recon ends with 0 edits -> force
        inject_forced=lambda *a_, **k: injected.append(True) or True,
        edit_probe=lambda: 0,
        phase1_elapsed_override=1800)
    assert res["forced_edit_triggered"] is True
    assert injected == [True]
    # Phase 2 is the single-call path, capped at remaining budget (7200-1800=5400)
    assert calls == [("single", 5400)]


def test_run_agent_recon_phase_no_hard_cap_runs_until_exit():
    import arvo_oss_crs as a
    class FakeProc:
        def __init__(self): self.calls = 0; self.killed = False
        def poll(self):
            self.calls += 1
            return None if self.calls < 4 else 0   # exits on the 4th poll
        def kill(self): self.killed = True
        def wait(self, timeout=None): return 0
    clock = {"t": 0.0}
    def fake_now(): clock["t"] += 10; return clock["t"]
    timed_out, forced = a._run_agent_recon_phase(
        ["run"], cwd=".", hard_cap=None, recon_cap=30,
        edit_probe=lambda: 5,                       # editing -> not forced
        popen=lambda cmd, cwd: FakeProc(),
        teardown=lambda: None, now=fake_now, sleep=lambda s: None)
    assert (timed_out, forced) == (False, False)    # natural exit, no None-comparison crash


def test_run_agent_recon_phase_probe_error_does_not_leak():
    import arvo_oss_crs as a
    class FakeProc:
        def __init__(self): self.killed = False
        def poll(self): return None
        def kill(self): self.killed = True
        def wait(self, timeout=None): return 0
    proc = FakeProc(); torn = []
    clock = {"t": 0.0}
    def fake_now(): clock["t"] += 10; return clock["t"]
    def boom(): raise PermissionError("root-owned log")
    timed_out, forced = a._run_agent_recon_phase(
        ["run"], cwd=".", hard_cap=100, recon_cap=30,
        edit_probe=boom, popen=lambda cmd, cwd: proc,
        teardown=lambda: torn.append(True), now=fake_now, sleep=lambda s: None)
    # probe raised -> not forced; runs to hard_cap -> killed + torn down, no leak, no raise
    assert (timed_out, forced) == (True, False)
    assert proc.killed and torn == [True]


def test_run_agent_phases_flag_off_does_not_probe(monkeypatch):
    import arvo_oss_crs as a
    monkeypatch.delenv("OSS_CRS_FORCE_EDIT", raising=False)
    calls = {"n": 0}
    def probe(): calls["n"] += 1; return 2
    res = a.run_agent_phases(
        run_cmd=["run"], cwd=".", sanitizer="address", bug={"localId": 1},
        project="mruby", hard_cap=100, newer_than=None, exclude=(),
        single=lambda cmd, cwd, to: False,
        recon=lambda **k: (_ for _ in ()).throw(AssertionError("recon must not run")),
        inject_forced=lambda *a_, **k: True, edit_probe=probe)
    assert calls["n"] == 0                    # feature off -> two-pass fields meaningless
    assert res["phase2_edits"] is None
    assert res["phase1_edits"] is None
    assert res["edit_phase"] is None          # flag off -> no phase label


def test_run_agent_phases_calls_handoff_between_inject_and_phase2(monkeypatch):
    import arvo_oss_crs as a
    monkeypatch.setenv("OSS_CRS_FORCE_EDIT", "1")
    events = []
    a.run_agent_phases(
        run_cmd=["run"], cwd=".", sanitizer="address", bug={"localId": 1},
        project="mruby", hard_cap=7200, newer_than=None, exclude=(),
        single=lambda cmd, cwd, to: events.append("single") or False,
        recon=lambda **k: (False, True),
        inject_forced=lambda *a_, **k: events.append("inject") or True,
        edit_probe=lambda: 0,
        on_forced_handoff=lambda: events.append("recycle"),
        phase1_elapsed_override=1800)
    assert events == ["inject", "recycle", "single"]


def test_run_agent_phases_no_handoff_when_not_forced(monkeypatch):
    import arvo_oss_crs as a
    monkeypatch.setenv("OSS_CRS_FORCE_EDIT", "1")
    called = []
    a.run_agent_phases(
        run_cmd=["run"], cwd=".", sanitizer="address", bug={"localId": 1},
        project="mruby", hard_cap=7200, newer_than=None, exclude=(),
        single=lambda cmd, cwd, to: False,
        recon=lambda **k: (False, False),          # recon made edits -> not forced
        inject_forced=lambda *a_, **k: True, edit_probe=lambda: 2,
        on_forced_handoff=lambda: called.append(1), phase1_elapsed_override=1800)
    assert called == []
