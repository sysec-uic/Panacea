"""Compose-file selection, preflight reachability, and token-count parsing."""
import json
import subprocess
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


class FakeProc:
    """Minimal Popen stand-in. `wait_results` is consumed in order across
    successive .wait() calls: an Exception instance is raised, an int is
    returned (and stored as .returncode). `stdout_lines`, if given, backs
    .stdout with an io.StringIO so `iter(proc.stdout.readline, "")` (the real
    drain loop in _run_agent_with_timeout) works exactly like it would against
    a real Popen text-mode pipe."""

    def __init__(self, wait_results, stdout_lines=None):
        self._results = list(wait_results)
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0
        if stdout_lines is not None:
            import io
            self.stdout = io.StringIO("".join(l + "\n" for l in stdout_lines))

    def wait(self, timeout=None):
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        self.returncode = result
        return result

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1


def test_agent_runs_to_completion_without_timeout():
    # No cap: run once, no teardown, and report the run did NOT time out.
    teardowns = []
    proc = FakeProc([0])

    timed_out, aborted = arvo_oss_crs._run_agent_with_timeout(
        ["uv", "run", "oss-crs"], cwd="/x", timeout=None,
        popen=lambda cmd, cwd, **kw: proc, teardown=lambda: teardowns.append(True))
    assert timed_out is False
    assert aborted is False
    assert teardowns == []          # nothing to tear down on a clean finish


def test_agent_timeout_gracefully_terminates_then_kills_and_tears_down():
    # A run that blows the cap: SIGTERM first (mirrors OSS-CRS's own graceful
    # shutdown), escalate to SIGKILL only if that doesn't work within _graceful_kill's
    # own wait, then sweep leftover containers and report a no-patch timed-out attempt.
    import subprocess
    teardowns = []
    proc = FakeProc([
        subprocess.TimeoutExpired(cmd=["x"], timeout=1.0),   # original cap blown
        subprocess.TimeoutExpired(cmd=["x"], timeout=20),    # unresponsive to SIGTERM
        -9,                                                   # SIGKILL finishes it
    ])

    timed_out, aborted = arvo_oss_crs._run_agent_with_timeout(
        ["uv", "run", "oss-crs"], cwd="/x", timeout=1.0,
        popen=lambda cmd, cwd, **kw: proc, teardown=lambda: teardowns.append(True))
    assert timed_out is True
    assert aborted is False
    assert proc.terminate_calls == 1
    assert proc.kill_calls == 1
    assert teardowns == [True]


def test_agent_reports_aborted_when_controller_already_requested():
    # Abort raced in before this call even started (e.g. q pressed between phases):
    # the process is terminated immediately on registration, and the clean-ish exit
    # that follows is reported as aborted, with the safety-net teardown sweep run.
    teardowns = []
    controller = arvo_oss_crs.AbortController()
    controller.requested.set()
    proc = FakeProc([-15])

    timed_out, aborted = arvo_oss_crs._run_agent_with_timeout(
        ["uv", "run", "oss-crs"], cwd="/x", timeout=None,
        popen=lambda cmd, cwd, **kw: proc, teardown=lambda: teardowns.append(True),
        abort_controller=controller)
    assert timed_out is False
    assert aborted is True
    assert proc.terminate_calls == 1     # caught up immediately since requested was already set
    assert teardowns == [True]


def test_agent_not_aborted_when_controller_absent_or_unrequested():
    proc = FakeProc([0])
    timed_out, aborted = arvo_oss_crs._run_agent_with_timeout(
        ["uv", "run", "oss-crs"], cwd="/x", timeout=None, popen=lambda cmd, cwd, **kw: proc)
    assert aborted is False   # no abort_controller passed at all

    proc = FakeProc([0])
    controller = arvo_oss_crs.AbortController()   # passed but never requested
    timed_out, aborted = arvo_oss_crs._run_agent_with_timeout(
        ["uv", "run", "oss-crs"], cwd="/x", timeout=None, popen=lambda cmd, cwd, **kw: proc,
        abort_controller=controller)
    assert aborted is False


def test_check_true_raises_on_a_genuine_failure_not_caused_by_abort():
    # build-target's semantics: a real nonzero exit (not an abort) must still raise.
    import subprocess
    proc = FakeProc([1])
    try:
        arvo_oss_crs._run_agent_with_timeout(
            ["uv", "run", "oss-crs"], cwd="/x", timeout=None,
            popen=lambda cmd, cwd, **kw: proc, check=True)
        assert False, "expected CalledProcessError"
    except subprocess.CalledProcessError:
        pass


def test_check_true_does_not_raise_when_the_nonzero_exit_was_our_own_abort():
    proc = FakeProc([-15])
    controller = arvo_oss_crs.AbortController()
    controller.requested.set()

    timed_out, aborted = arvo_oss_crs._run_agent_with_timeout(
        ["uv", "run", "oss-crs"], cwd="/x", timeout=None,
        popen=lambda cmd, cwd, **kw: proc, check=True, abort_controller=controller)
    assert aborted is True   # no CalledProcessError, even though returncode != 0


def test_abort_controller_terminates_the_currently_registered_process():
    controller = arvo_oss_crs.AbortController()
    proc = FakeProc([])
    controller.register(proc)
    controller.abort()
    assert controller.requested.is_set()
    assert proc.terminate_calls == 1


def test_abort_controller_noop_when_nothing_registered():
    controller = arvo_oss_crs.AbortController()
    controller.abort()   # must not raise
    assert controller.requested.is_set()


def test_abort_controller_does_not_terminate_after_unregister():
    controller = arvo_oss_crs.AbortController()
    proc = FakeProc([])
    controller.register(proc)
    controller.unregister(proc)
    controller.abort()
    assert proc.terminate_calls == 0   # already unregistered -- must not touch it


def test_abort_controller_switches_registration_across_phases():
    # Simulates build-target's process finishing and the run phase's process
    # registering next -- abort() must always reach whichever is CURRENT.
    controller = arvo_oss_crs.AbortController()
    build_proc = FakeProc([])
    controller.register(build_proc)
    controller.unregister(build_proc)   # build-target finished, its finally unregisters
    run_proc = FakeProc([])
    controller.register(run_proc)

    controller.abort()
    assert run_proc.terminate_calls == 1
    assert build_proc.terminate_calls == 0


def test_on_line_receives_every_line_from_the_subprocess():
    proc = FakeProc([0], stdout_lines=["CC src/foo.c -> foo.o", "CC src/bar.c -> bar.o", "OK Build"])
    received = []

    timed_out, aborted = arvo_oss_crs._run_agent_with_timeout(
        ["uv", "run", "oss-crs"], cwd="/x", timeout=None,
        popen=lambda cmd, cwd, **kw: proc, on_line=received.append)

    assert received == ["CC src/foo.c -> foo.o", "CC src/bar.c -> bar.o", "OK Build"]
    assert timed_out is False
    assert aborted is False


def test_on_line_none_does_not_pipe_stdout_at_all():
    # Default behavior (no live panel) must be unaffected for stdout/stderr: the
    # child's output inherits the terminal directly, exactly like before this
    # feature existed -- confirmed here by checking NO stdout/stderr kwargs are
    # passed to popen at all when on_line is absent. stdin IS always closed
    # (DEVNULL) regardless of on_line -- see test_stdin_is_always_closed_to_the_child.
    seen_kwargs = {}

    def fake_popen(cmd, cwd, **kw):
        seen_kwargs.update(kw)
        return FakeProc([0])

    arvo_oss_crs._run_agent_with_timeout(
        ["uv", "run", "oss-crs"], cwd="/x", timeout=None, popen=fake_popen)
    assert seen_kwargs == {"stdin": subprocess.DEVNULL}   # no stdout=PIPE, no stderr=STDOUT, no text=True


def test_stdin_is_always_closed_to_the_child():
    # The child (and its own descendants, e.g. build-target's docker compose/buildx
    # chain) must never share the real terminal's stdin fd with live_status's
    # key-listener thread -- two readers racing on the same tty fd is exactly the
    # kind of thing that can make a keystroke (v/q) appear to double-fire or drop.
    seen_kwargs = {}

    def fake_popen(cmd, cwd, **kw):
        seen_kwargs.update(kw)
        return FakeProc([0], stdout_lines=[])

    arvo_oss_crs._run_agent_with_timeout(
        ["uv", "run", "oss-crs"], cwd="/x", timeout=None, popen=fake_popen, on_line=lambda l: None)
    assert seen_kwargs["stdin"] == subprocess.DEVNULL


def test_on_line_pipes_stdout_and_merges_stderr():
    seen_kwargs = {}

    def fake_popen(cmd, cwd, **kw):
        seen_kwargs.update(kw)
        return FakeProc([0], stdout_lines=[])

    arvo_oss_crs._run_agent_with_timeout(
        ["uv", "run", "oss-crs"], cwd="/x", timeout=None, popen=fake_popen, on_line=lambda l: None)
    assert seen_kwargs["stdout"] == subprocess.PIPE
    assert seen_kwargs["stderr"] == subprocess.STDOUT
    assert seen_kwargs["text"] is True


def test_reader_thread_is_joined_before_returning():
    # The finally block must wait for the drain loop to finish before
    # _run_agent_with_timeout returns, so a caller never sees a partial read.
    proc = FakeProc([0], stdout_lines=["a", "b", "c"])
    received = []

    arvo_oss_crs._run_agent_with_timeout(
        ["uv", "run", "oss-crs"], cwd="/x", timeout=None,
        popen=lambda cmd, cwd, **kw: proc, on_line=received.append)
    # By the time the call returns, every line must already be collected --
    # not "eventually" on some still-running background thread.
    assert received == ["a", "b", "c"]


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


def test_detect_usage_limit_none_on_clean_log(tmp_path):
    log = _write_log(tmp_path, [
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 123}},
        {"type": "result", "subtype": "success", "is_error": False},
    ])
    assert arvo_oss_crs.detect_usage_limit(log) is None


def test_detect_usage_limit_none_on_warning_status(tmp_path):
    # allowed_warning (approaching the cap) is not a cutoff -- must not trip the gate.
    log = _write_log(tmp_path, [
        {"type": "rate_limit_event",
         "rate_limit_info": {"status": "allowed_warning", "resetsAt": 123, "utilization": 0.91}},
    ])
    assert arvo_oss_crs.detect_usage_limit(log) is None


def test_detect_usage_limit_missing_file_is_none(tmp_path):
    assert arvo_oss_crs.detect_usage_limit(tmp_path / "nope.log") is None


def test_detect_usage_limit_real_rejected_event_shape(tmp_path):
    # Real payload captured from a live run (bug 455612769, 2026-07-16) that got cut
    # off by the 5-hour cap mid-investigation.
    log = _write_log(tmp_path, [
        {"type": "rate_limit_event",
         "rate_limit_info": {"status": "rejected", "resetsAt": 1784238600,
                             "rateLimitType": "five_hour", "overageStatus": "rejected",
                             "overageDisabledReason": "org_level_disabled", "isUsingOverage": False},
         "uuid": "dcce88a7-42ea-4e85-aacc-99dcd8be3ce1"},
        {"type": "assistant", "error": "rate_limit",
         "message": {"content": [{"type": "text", "text": "You've hit your session limit · resets 9:50pm (UTC)"}]}},
        {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
         "result": "You've hit your session limit · resets 9:50pm (UTC)"},
    ])
    result = arvo_oss_crs.detect_usage_limit(log)
    assert result == {"resets_at": 1784238600,
                      "resets_at_human": "You've hit your session limit · resets 9:50pm (UTC)"}


def test_detect_usage_limit_result_object_alone_is_sufficient(tmp_path):
    # If the log gets cut off before/without a rate_limit_event line (e.g. the CLI
    # process itself got killed), the terminal result object alone must still trip it.
    log = _write_log(tmp_path, [
        {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
         "result": "You've hit your session limit · resets 4:00am (UTC)"},
    ])
    assert arvo_oss_crs.detect_usage_limit(log) == {
        "resets_at": None, "resets_at_human": "You've hit your session limit · resets 4:00am (UTC)",
    }


def test_detect_usage_limit_ignores_unparseable_lines(tmp_path):
    log = tmp_path / "claude_stdout.log"
    log.write_text('not json\n{"type": "rate_limit_event", "rate_limit_info": '
                   '{"status": "rejected", "resetsAt": 5}}\n')
    assert arvo_oss_crs.detect_usage_limit(log) == {"resets_at": 5, "resets_at_human": None}


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
    monkeypatch.setattr(arvo_oss_crs, "find_target_source_dir", lambda san: tmp_path)
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
    monkeypatch.setattr(arvo_oss_crs, "find_target_source_dir", lambda san: tmp_path)
    bug = {"localId": 1, "project": "mruby", "crash_type": "x", "crash_output": "==ERROR: ..."}
    assert arvo_oss_crs.inject_orientation("address", bug) is False
    assert not (tmp_path / "ORIENTATION.md").exists()
