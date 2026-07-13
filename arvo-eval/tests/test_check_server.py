"""Host-side responder for the agent's in-turn check-patch tool.

The agent (which has no Docker and none of this code) drops its current diff into a
request file on a shared channel; this responder runs the real build+PoC+rake-test
against a warm -vul container and writes the verdict back. serve_one is
channel-agnostic (paths + exec_fn are parameters) so it is unit-testable without
Docker and reusable whichever OSS-CRS dir turns out to be the live channel."""
import os

import check_server


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


BUG = {"localId": 449429295, "sanitizer": "asan", "project": "mruby"}


def test_serve_one_no_request_is_noop(tmp_path):
    req, resp = tmp_path / "req.diff", tmp_path / "resp.txt"
    called = []
    out = check_server.serve_one(
        req, resp, bug=BUG, project="mruby", exec_fn=None,
        run_check=lambda *a, **k: called.append(1) or {})
    assert out is None
    assert not resp.exists()
    assert called == []   # nothing to check, so the (expensive) check never runs


def test_serve_one_runs_check_and_writes_verdict(tmp_path):
    req, resp = tmp_path / "req.diff", tmp_path / "resp.txt"
    req.write_text("--- a/mrbgems/mruby-bigint/core/bigint.c\n+++ b/...\n")
    seen = {}

    def fake_run_check(bug, diff, exec_fn, *, project):
        seen["diff"] = diff
        return {"classification": "verified_correct"}

    fb = check_server.serve_one(req, resp, bug=BUG, project="mruby",
                                exec_fn="EXEC", run_check=fake_run_check)
    assert "PASS" in fb and "submit" in fb.lower()      # real check_feedback rendered
    assert resp.read_text() == fb
    assert "bigint.c" in seen["diff"]                   # the agent's diff reached run_check
    assert not req.exists()                             # request consumed, won't reprocess


def test_serve_one_consumes_request_so_it_runs_once(tmp_path):
    req, resp = tmp_path / "req.diff", tmp_path / "resp.txt"
    req.write_text("DIFF")
    runs = []
    kw = dict(bug=BUG, project="mruby", exec_fn=None,
              run_check=lambda *a, **k: runs.append(1) or {"classification": "build_failed"})
    check_server.serve_one(req, resp, **kw)
    check_server.serve_one(req, resp, **kw)   # request gone now
    assert len(runs) == 1


def test_prepare_channel_drops_executable_client_and_returns_paths(tmp_path):
    req, resp = check_server.prepare_channel(tmp_path)
    client = tmp_path / "check-patch"
    assert client.exists() and os.access(client, os.X_OK)   # agent can execute it
    assert "git diff" in client.read_text()                  # it captures the agent's patch
    assert req == tmp_path / ".check_request.diff"
    assert resp == tmp_path / ".check_response.txt"


def test_run_service_waits_for_channel_prepares_it_and_tears_down(tmp_path):
    # find_dir returns None once (channel not up yet), then the dir; stop() then ends
    # the serve loop. Asserts it drops the client and always removes the container.
    dirs = iter([None, tmp_path])
    stops = iter([False, False, True, True, True])
    removed = []

    check_server.run_service(
        BUG, {"instance_id": "1", "image_name": "img"}, "mruby",
        find_dir=lambda: next(dirs, tmp_path),
        stop=lambda: next(stops, True),
        warm=lambda inst: "cont",
        remove=removed.append,
        sleep=lambda s: None)

    assert (tmp_path / "check-patch").exists()   # channel prepared once it appeared
    assert removed == ["cont"]                   # warm container always cleaned up


def test_run_service_gives_up_if_channel_never_appears(tmp_path):
    # If the agent run ends before SHARED_DIR shows up, stop() flips true and we bail
    # without warming a container (nothing to clean up).
    warmed = []
    check_server.run_service(
        BUG, {"instance_id": "1", "image_name": "img"}, "mruby",
        find_dir=lambda: None, stop=lambda: True,
        warm=lambda inst: warmed.append(1), remove=warmed.append, sleep=lambda s: None)
    assert warmed == []


def test_warm_container_recreates_and_returns_name():
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = ""
        return R()

    name = check_server.warm_container(
        {"instance_id": "449429295", "image_name": "n132/arvo:449429295-vul"}, run=fake_run)
    assert name == "arvo-449429295-check"
    # Force-remove any stale one first, then start a long-lived container from the -vul image.
    assert calls[0][:3] == ["docker", "rm", "-f"]
    run_cmd = next(c for c in calls if "run" in c)
    assert "n132/arvo:449429295-vul" in run_cmd and "-d" in run_cmd
