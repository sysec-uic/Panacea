"""Host-side responder for the agent's in-turn check-patch tool.

The agent (which has no Docker and none of this code) drops its current diff into a
request file on a shared channel; this responder runs the real build+PoC+rake-test
against a warm -vul container and writes the verdict back. serve_one is
channel-agnostic (paths + exec_fn are parameters) so it is unit-testable without
Docker and reusable whichever OSS-CRS dir turns out to be the live channel."""
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
