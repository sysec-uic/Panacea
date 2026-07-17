"""Host-side responder for the repair agent's in-turn `check-patch` tool.

The agent can't build or test in its own container (no toolchain, no Docker), so it
edits blind and flails. This gives it a self-check: it drops its current diff onto a
shared channel as a request file; this responder runs the SAME deployment-faithful
build+PoC+rake-test the outer loop uses (verify_fix.run_check), against a warm -vul
container it keeps hot across checks, and writes the verdict back for the agent to read.

Faithfulness: this is exactly the crash-gone + `rake test` signal the repair loop
already exposes between attempts -- never the -fix image -- just delivered in-turn. The
differential -fix oracle stays the separate, agent-invisible, post-hoc gate.

serve_one is channel-agnostic: the request/response paths and the container `exec_fn`
are parameters, so it is unit-testable without Docker and works whichever OSS-CRS
bind-mounted dir (SUBMIT_DIR/EXCHANGE_DIR/FETCH_DIR) turns out to be the live channel.
Wiring that channel + launching serve_loop from a run is confirmed against a live
campaign before it ships.
"""
import subprocess
import time
from pathlib import Path

import verify_fix

COMPILE_TIMEOUT = verify_fix.COMPILE_TIMEOUT
CLIENT_SCRIPT = Path(__file__).parent / "oss-crs-local" / "check-patch"
REQUEST_NAME = ".check_request.diff"
RESPONSE_NAME = ".check_response.txt"


def prepare_channel(shared_dir, *, client_src=CLIENT_SCRIPT):
    """Drop the `check-patch` client into the agent's SHARED_DIR and return the
    (request, response) paths the responder watches. The agent runs the client from
    its source tree; it writes requests here and reads verdicts back."""
    shared_dir = Path(shared_dir)
    dest = shared_dir / "check-patch"
    dest.write_text(Path(client_src).read_text())
    dest.chmod(0o755)
    return shared_dir / REQUEST_NAME, shared_dir / RESPONSE_NAME


def warm_container(instance: dict, *, run=subprocess.run) -> str:
    """Start a long-lived -vul container for repeated in-turn checks; the caller
    removes it when the run ends. Reused across checks so each self-check is an
    incremental rebuild (seconds), not a cold build (~40 min)."""
    name = f"arvo-{instance['instance_id']}-check"
    run(["docker", "rm", "-f", name], capture_output=True)
    run(["docker", "run", "-d", "--name", name, instance["image_name"], "sleep", "86400"],
        check=True, capture_output=True)
    return name


def serve_one(req_path: Path, resp_path: Path, *, bug, project, exec_fn,
              run_check=verify_fix.run_check, feedback=verify_fix.check_feedback,
              marker_path=None, autosubmit_path=None):
    """Process one pending check request, if any. Returns the feedback string, or
    None when there is no request. The response is written atomically (temp + rename)
    and the request is consumed so it is never re-run.

    On a PASS (verified_correct): touch `marker_path` if given (the repair loop reads it
    to require the agent to have self-validated before accepting a submission), and save
    the passing diff to `autosubmit_path` if given, overwriting any earlier one so it
    holds the latest validated fix. run_oss_crs promotes that saved diff as the
    submission when the agent gets a PASS but never writes a patch to /patches/ -- the
    loss mode where a validated fix was thrown away (see the Jul 17 postmortem)."""
    req_path, resp_path = Path(req_path), Path(resp_path)
    if not req_path.exists():
        return None
    diff = req_path.read_text()
    verification = run_check(bug, diff, exec_fn, project=project)
    if verification.get("classification") == "verified_correct":
        if marker_path is not None:
            Path(marker_path).write_text("pass")
        if autosubmit_path is not None:
            Path(autosubmit_path).write_text(diff)
    fb = feedback(verification)
    tmp = resp_path.with_name(resp_path.name + ".tmp")
    tmp.write_text(fb)
    tmp.replace(resp_path)
    req_path.unlink()
    return fb


def serve_loop(req_path: Path, resp_path: Path, *, bug, project, exec_fn, stop,
               poll_seconds=2.0, sleep=time.sleep, marker_path=None, autosubmit_path=None):
    """Poll the channel for check requests until `stop()` is true. Run in a background
    thread for the duration of an agent run; `stop` is typically a threading.Event.is_set."""
    while not stop():
        serve_one(req_path, resp_path, bug=bug, project=project, exec_fn=exec_fn,
                  marker_path=marker_path, autosubmit_path=autosubmit_path)
        sleep(poll_seconds)


def run_service(bug, instance, project, *, find_dir, stop, poll=2.0, sleep=time.sleep,
                warm=warm_container, remove=None, marker_path=None, autosubmit_path=None):
    """Serve in-turn check-patch requests for the duration of one agent run.

    The agent's SHARED_DIR only appears once OSS-CRS renders the run compose mid-launch,
    so wait for it, then drop the client, warm one -vul container (reused across checks),
    and serve until stop(). Best-effort: gated by OSS_CRS_CHECK_PATCH and wrapped so any
    failure here can never break the agent run. End-to-end behavior is validated on a
    live campaign; the unit tests cover channel prep + teardown."""
    shared = None
    while not stop() and shared is None:
        shared = find_dir()
        if shared is None:
            sleep(poll)
    if shared is None:
        return
    req, resp = prepare_channel(shared)
    container = warm(instance)
    remove = remove or (lambda c: subprocess.run(["docker", "rm", "-f", c], capture_output=True))
    try:
        exec_fn = lambda cmd, input=None, timeout=60: verify_fix.docker_exec(
            container, cmd, input=input, timeout=timeout)
        serve_loop(req, resp, bug=bug, project=project, exec_fn=exec_fn, stop=stop,
                   poll_seconds=poll, sleep=sleep, marker_path=marker_path,
                   autosubmit_path=autosubmit_path)
    finally:
        remove(container)
