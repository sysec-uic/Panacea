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
              run_check=verify_fix.run_check, feedback=verify_fix.check_feedback):
    """Process one pending check request, if any. Returns the feedback string, or
    None when there is no request. The response is written atomically (temp + rename)
    and the request is consumed so it is never re-run."""
    req_path, resp_path = Path(req_path), Path(resp_path)
    if not req_path.exists():
        return None
    diff = req_path.read_text()
    fb = feedback(run_check(bug, diff, exec_fn, project=project))
    tmp = resp_path.with_name(resp_path.name + ".tmp")
    tmp.write_text(fb)
    tmp.replace(resp_path)
    req_path.unlink()
    return fb


def serve_loop(req_path: Path, resp_path: Path, *, bug, project, exec_fn, stop,
               poll_seconds=2.0, sleep=time.sleep):
    """Poll the channel for check requests until `stop()` is true. Run in a background
    thread for the duration of an agent run; `stop` is typically a threading.Event.is_set."""
    while not stop():
        serve_one(req_path, resp_path, bug=bug, project=project, exec_fn=exec_fn)
        sleep(poll_seconds)
