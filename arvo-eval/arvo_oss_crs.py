"""Run crs-claude-code (OSS-CRS) on ARVO bugs.

Creates a minimal fake OSS-Fuzz project directory per bug that wraps the
ARVO Docker image, then drives OSS-CRS build-target + run against it.

Usage:
    python arvo_oss_crs.py                     # runs BUG_ID (default or env var)
    OSS_CRS_BUG_ID=40096184 python arvo_oss_crs.py

Prerequisites:
    - ~/oss-crs cloned from https://github.com/ossf/oss-crs
    - arvo-eval/oss-crs-local/install.sh run once (installs compose-local.yaml +
      litellm-config-local.yaml into ~/oss-crs); the SSH tunnel to the local model
      must also listen on 172.17.0.1:8080 -- see arvo-eval/README.md
    - Run `uv run oss-crs prepare --compose-file <COMPOSE_FILE>` once first
    - To use Claude via OAuth instead: export CLAUDE_CODE_OAUTH_TOKEN and
      OSS_CRS_COMPOSE_FILE=$HOME/oss-crs/example/crs-claude-code/compose-oauth.yaml

Env knobs:
    - OSS_CRS_RUN_TIMEOUT   wall-clock cap (seconds) for one agent run; unset = no cap.
                            On timeout the run's compose containers are torn down and
                            the result is a no-patch, timed_out attempt.
    - OSS_CRS_DOCKER_CLEANUP / OSS_CRS_DOCKER_KEEP   stale-image reaping after each run.
"""

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from build_instance import load_bug

OSS_CRS_DIR = Path.home() / "oss-crs"
def _compose_file() -> Path:
    """OSS_CRS_COMPOSE_FILE overrides (e.g. compose-oauth.yaml to use Claude
    via OAuth); the default is the local-model compose."""
    return Path(os.environ.get(
        "OSS_CRS_COMPOSE_FILE",
        str(OSS_CRS_DIR / "example/crs-claude-code/compose-local.yaml")))


COMPOSE_FILE = _compose_file()

# The docker-bridge address the LiteLLM container dials for the tunneled local
# model; the host can reach it only when the SSH tunnel's second -L binding is up,
# so probing it here is a faithful proxy for "the container will be able to connect."
LOCAL_MODEL_HEALTHCHECK = os.environ.get(
    "OSS_CRS_LOCAL_MODEL_HEALTHCHECK", "http://172.17.0.1:8080/v1/models")


def _uses_local_model(compose_file: Path) -> bool:
    """The local-model compose routes through LiteLLM to the tunneled server; the
    OAuth compose talks to Anthropic directly and needs no local endpoint."""
    return "oauth" not in compose_file.name


def check_local_model_reachable(url: str = None, *, timeout: float = 4.0,
                                opener=urllib.request.urlopen) -> None:
    """Fail fast if the local model endpoint is unreachable. A dead SSH tunnel
    otherwise wastes a full CRS spin-up and surfaces only as a buried LiteLLM 500
    on the agent's first turn (num_turns=1, no patch)."""
    url = url or LOCAL_MODEL_HEALTHCHECK
    try:
        opener(url, timeout=timeout)
    except Exception as exc:
        raise RuntimeError(
            f"Local model endpoint unreachable at {url}: {exc}. The repair agent "
            f"runs on the tunneled local model, so this run would fail on its first "
            f"turn with a LiteLLM 500. Restart the SSH tunnel WITH the docker-bridge "
            f"binding:\n"
            f"  ssh -L 8080:localhost:8080 -L 172.17.0.1:8080:localhost:8080 user@llm-server\n"
            f"Or run against Claude via OAuth: export CLAUDE_CODE_OAUTH_TOKEN and set "
            f"OSS_CRS_COMPOSE_FILE=$HOME/oss-crs/example/crs-claude-code/compose-oauth.yaml."
        ) from exc


def wait_for_local_model(url: str = None, *, poll_seconds: float = 15.0,
                         timeout: float = 4.0, opener=urllib.request.urlopen,
                         sleep=time.sleep) -> None:
    """Block until the local model endpoint answers, polling every `poll_seconds`.

    The SSH tunnel to the model server sometimes drops mid-campaign; raising there
    (check_local_model_reachable) kills a multi-hour learn_loop run that would resume
    fine once the tunnel is restarted. So the run-level preflight waits instead of
    failing: print the actionable restart instructions once, then poll quietly until
    the tunnel is back.
    """
    url = url or LOCAL_MODEL_HEALTHCHECK
    waited = 0.0
    while True:
        try:
            check_local_model_reachable(url, timeout=timeout, opener=opener)
            if waited:
                print(f"[preflight] Local model endpoint reachable again after ~{waited:.0f}s; resuming.")
            return
        except RuntimeError as exc:
            if not waited:
                print(f"[preflight] {exc}")
                print(f"[preflight] Waiting for the tunnel instead of failing; "
                      f"re-probing every {poll_seconds:.0f}s...")
            sleep(poll_seconds)
            waited += poll_seconds


PROJECTS_DIR = Path.home() / ".arvo-oss-crs"   # stable per-bug project dirs live here
RESULTS_DIR = Path(__file__).parent / "results"

SANITIZER_DIR = {"asan": "address", "msan": "memory", "ubsan": "undefined", "coverage": "coverage"}


def generate_fake_oss_fuzz_project(bug: dict, project_dir: Path) -> None:
    """Write a minimal OSS-Fuzz-compatible project dir wrapping an ARVO image.

    The Dockerfile just pulls the ARVO image (already compiled). The build.sh
    is a no-op because all binaries are already in /out/ of that image.
    """
    project_dir.mkdir(parents=True, exist_ok=True)

    (project_dir / "Dockerfile").write_text(
        f"FROM n132/arvo:{bug['localId']}-vul\n"
        f"RUN printf '#!/bin/bash\\ncd /src/mruby && rake test\\n'"
        f" > /src/run_tests.sh && chmod +x /src/run_tests.sh\n"
    )

    build_sh = project_dir / "build.sh"
    build_sh.write_text("#!/bin/bash\n# Binaries already compiled in ARVO image.\n")
    build_sh.chmod(0o755)

    (project_dir / "project.yaml").write_text(
        f"language: {bug['language']}\n"
        f"main_repo: {bug['repo_addr']}\n"
        f"fuzzing_engines:\n"
        f"  - {bug['fuzz_engine'].lower()}\n"
        f"sanitizers:\n"
        f"  - {SANITIZER_DIR.get(bug['sanitizer'].lower(), bug['sanitizer'].lower())}\n"
    )


def extract_poc(bug_id: int, pov_path: Path) -> None:
    """Copy /tmp/poc out of the ARVO Docker image to pov_path."""
    result = subprocess.run(
        ["docker", "run", "--rm", f"n132/arvo:{bug_id}-vul", "cat", "/tmp/poc"],
        capture_output=True,
        check=True,
    )
    pov_path.write_bytes(result.stdout)


def find_latest_run_dir(sanitizer: str) -> Path | None:
    """Return the most recently created run directory in the OSS-CRS workdir."""
    base = OSS_CRS_DIR / ".oss-crs-workdir" / "crs_compose"
    sanitizer_dir = SANITIZER_DIR.get(sanitizer.lower(), sanitizer.lower())
    run_dirs = list(base.glob(f"*/{sanitizer_dir}/runs/*/"))
    if not run_dirs:
        return None
    return max(run_dirs, key=lambda p: p.stat().st_mtime)


def find_target_source_dir(sanitizer: str) -> Path | None:
    """Return the most recently modified target-source directory in the OSS-CRS workdir.

    OSS-CRS extracts the bug's Docker image WORKDIR (/src) here; this is the
    directory the agent runs in (cwd=source_dir in claude_code.py).
    """
    base = OSS_CRS_DIR / ".oss-crs-workdir" / "crs_compose"
    sanitizer_dir = SANITIZER_DIR.get(sanitizer.lower(), sanitizer.lower())
    dirs = list(base.glob(f"*/{sanitizer_dir}/builds/*/targets/*/target-source"))
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


def find_shared_dir(sanitizer: str, newer_than: float | None = None) -> Path | None:
    """Host path backing the agent's /OSS_CRS_SHARED_DIR rw bind mount -- the live
    agent<->host channel for the in-turn check-patch tool. Structure mirrors oss-crs
    get_shared_dir: <sanitizer>/runs/<run_id>/crs/<crs_name>/<target>/SHARED_DIR/<harness>.

    `newer_than` (epoch seconds) rejects SHARED_DIRs from prior/killed runs: pass the
    time the current run's service started, so a stale dir can never win the newest-by-
    mtime race and make the responder latch a dead channel."""
    base = OSS_CRS_DIR / ".oss-crs-workdir" / "crs_compose"
    sanitizer_dir = SANITIZER_DIR.get(sanitizer.lower(), sanitizer.lower())
    dirs = [d for d in base.glob(f"*/{sanitizer_dir}/runs/*/crs/*/*/SHARED_DIR/*")
            if newer_than is None or d.stat().st_mtime > newer_than]
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


CHECK_PATCH_INSTRUCTION = (
    "\n\n## Your primary loop: edit -> check-patch -> iterate\n"
    "You have a `check-patch` tool. It builds the sanitizer target with your current "
    "changes, re-runs the crashing input, and runs the test suite, then prints PASS "
    "(the crash is gone and tests pass) or FAIL with exactly what is still wrong. "
    "Run it from your source tree:\n"
    "    bash \"$OSS_CRS_SHARED_DIR/check-patch\"\n"
    "Do NOT try to understand the whole codebase before editing. As soon as you have "
    "a root-cause hypothesis, make your best edit and run check-patch; let its FAIL "
    "output drive the next read or edit. Checks are cheap (incremental rebuild -- "
    "seconds to a couple of minutes after the first), so expect and budget for "
    "several edit -> check cycles. An early wrong edit that check-patch refutes "
    "teaches you more than an hour of reading. Submit only after check-patch prints "
    "PASS.\n"
)


def _check_patch_enabled() -> bool:
    return os.environ.get("OSS_CRS_CHECK_PATCH") == "1"


def inject_heuristics(project_dir: Path, sanitizer: str, bug_id: int) -> None:
    """Deliver the playbook (and, when enabled, the check-patch instruction) into the
    agent's source directory.

    HEURISTICS.md is written to the fake OSS-Fuzz project dir by injector.py but that
    dir never reaches the agent; this bridges the gap by writing into the extracted
    target-source dir where the agent runs. The check-patch instruction is appended
    even when the playbook is empty, so the agent always learns the tool exists.
    """
    target_source = find_target_source_dir(sanitizer)
    if target_source is None:
        print(f"[{bug_id}] Warning: could not find target-source dir, skipping heuristics injection")
        return
    src = project_dir / "HEURISTICS.md"
    text = src.read_text() if src.exists() else ""
    if _check_patch_enabled():
        text += CHECK_PATCH_INSTRUCTION
    if not text.strip():
        return
    (target_source / "HEURISTICS.md").write_text(text)
    print(f"[{bug_id}] Injected HEURISTICS.md into {target_source}")


def collect_patches(run_dir: Path) -> list[Path]:
    """Find patch diff files the agent produced in this run."""
    return list(run_dir.glob("**/SUBMIT_DIR/*/patches/*.diff"))


def copy_session_files(run_dir: Path, output_dir: Path) -> None:
    """Copy claude_stdout.log to output_dir."""
    for log in run_dir.glob("crs/crs-claude-code/*/LOG_DIR/*/agent/claude_stdout.log"):
        shutil.copy2(log, output_dir / "oss_crs_claude_stdout.log")


def parse_token_counts(log_path: Path) -> dict:
    """Sum token usage across all API calls in a claude_stdout.log.

    Usage is deduplicated per response: older logs carry a top-level `request_id`,
    but the Claude Code CLI driving the local model emits none -- it keys each
    response by `message.id` and repeats that id across stream events (a partial
    `output_tokens` at message start, the final count later). So we take the MAX
    usage seen per id, then sum across distinct ids. Keying on request_id alone
    (the old behavior) silently reported zero tokens for every local run.
    """
    per_id: dict = {}
    try:
        for line in log_path.read_text().splitlines():
            obj = json.loads(line)
            msg = obj.get("message", {})
            u = msg.get("usage") or {}
            key = obj.get("request_id") or msg.get("id")
            if not u or not key:
                continue
            d = per_id.setdefault(key, {"in": 0, "out": 0, "cr": 0, "cw": 0})
            d["in"] = max(d["in"], u.get("input_tokens", 0))
            d["out"] = max(d["out"], u.get("output_tokens", 0))
            d["cr"] = max(d["cr"], u.get("cache_read_input_tokens", 0))
            d["cw"] = max(d["cw"], u.get("cache_creation_input_tokens", 0))
    except Exception:
        pass
    return {
        "input_tokens": sum(d["in"] for d in per_id.values()),
        "output_tokens": sum(d["out"] for d in per_id.values()),
        "cache_read_tokens": sum(d["cr"] for d in per_id.values()),
        "cache_write_tokens": sum(d["cw"] for d in per_id.values()),
    }


def _run_epoch(s: str) -> int:
    """OSS-CRS embeds a 10-digit epoch in run/build ids (test-1783442751bt,
    crs_compose_1783528430al-...); it orders disposables by age."""
    m = re.search(r"(\d{10})", s)
    return int(m.group(1)) if m else 0


def stale_docker_images(images: list, *, keep: int = 2) -> list[str]:
    """Pick per-run OSS-CRS disposables to delete: test-*/build-* snapshots beyond
    the newest `keep` per kind, and crs_compose_<runid>-* image sets of every run
    but the newest. content-* snapshots are the incremental-build cache and other
    repos (ARVO images etc.) are not ours -- both stay untouched.
    `images` is a list of (repository, tag) pairs."""
    stale = []
    snap_tags = [t for r, t in images if r == "oss-crs-snapshot"]
    for kind in ("test-", "build-"):
        tags = sorted((t for t in snap_tags if t.startswith(kind)),
                      key=_run_epoch, reverse=True)
        stale += [f"oss-crs-snapshot:{t}" for t in tags[keep:]]

    compose = [(r, t) for r, t in images if r.startswith("crs_compose_")]
    run_ids = sorted({r.split("-", 1)[0] for r, _ in compose},
                     key=_run_epoch, reverse=True)
    dead = set(run_ids[1:])
    stale += [f"{r}:{t}" for r, t in compose if r.split("-", 1)[0] in dead]
    return stale


def cleanup_docker_images(*, keep: int = None, run=subprocess.run) -> list[str]:
    """Delete stale per-run OSS-CRS docker images after each run; every run tags a
    fresh ~9GB snapshot pair that otherwise accumulates forever. `docker rmi` is
    used WITHOUT -f, so anything still referenced by a container survives.
    OSS_CRS_DOCKER_CLEANUP=0 disables; OSS_CRS_DOCKER_KEEP overrides `keep`."""
    if os.environ.get("OSS_CRS_DOCKER_CLEANUP", "1") == "0":
        return []
    keep = keep if keep is not None else int(os.environ.get("OSS_CRS_DOCKER_KEEP", "2"))
    out = run(["docker", "images", "--format", "{{.Repository}} {{.Tag}}"],
              capture_output=True, text=True)
    images = [tuple(parts) for line in out.stdout.splitlines()
              if len(parts := line.split()) == 2]
    stale = stale_docker_images(images, keep=keep)
    for ref in stale:
        run(["docker", "rmi", ref], capture_output=True, text=True)
    run(["docker", "image", "prune", "-f"], capture_output=True, text=True)
    if stale:
        print(f"[cleanup] removed {len(stale)} stale OSS-CRS images")
    return stale


def _run_timeout() -> float | None:
    """Wall-clock cap (seconds) for a single agent run, from OSS_CRS_RUN_TIMEOUT.
    Unset -> no cap (unchanged behavior). A campaign sets e.g. 7200 so a flailing
    attempt can never eat 36h again (see the Jul 13 campaign postmortem)."""
    v = os.environ.get("OSS_CRS_RUN_TIMEOUT")
    return float(v) if v else None


def terminate_crs_run(*, run=subprocess.run) -> list[str]:
    """Force-remove any live OSS-CRS compose containers.

    Killing the `uv run oss-crs` process on timeout only reaps that process --
    docker-compose leaves its services running, and on the local-model box those
    keep the GPU pegged. The compose project is named `crs_compose_<runid>` (see
    oss-crs utils.py), so its containers all carry that name prefix; match and
    remove them. Only invoked on the timeout path, so a serial campaign has at most
    the one dead run to clean up."""
    out = run(["docker", "ps", "-q", "--filter", "name=crs_compose"],
              capture_output=True, text=True)
    ids = out.stdout.split()
    if ids:
        run(["docker", "rm", "-f", *ids], capture_output=True, text=True)
        print(f"[timeout] force-removed {len(ids)} live OSS-CRS containers")
    return ids


def _run_agent_with_timeout(cmd, *, cwd, timeout, run=subprocess.run,
                            teardown=None) -> bool:
    """Run the CRS agent subprocess under `timeout`. Returns whether it timed out.

    On timeout, subprocess.run SIGKILLs the oss-crs process but its docker
    containers survive, so tear them down before reporting. A clean finish (or no
    cap) reports False and tears down nothing."""
    teardown = teardown or terminate_crs_run
    try:
        run(cmd, cwd=cwd, check=False, timeout=timeout)
        return False
    except subprocess.TimeoutExpired:
        teardown()
        return True


def run_oss_crs(bug_id: int, skip_build: bool = False) -> dict:
    """Run crs-claude-code on one ARVO bug. Returns a summary dict."""
    bug = load_bug(bug_id)
    sanitizer = bug["sanitizer"].lower()

    project_dir = PROJECTS_DIR / str(bug_id) / "project"
    pov_path = PROJECTS_DIR / str(bug_id) / "poc"
    _pass = os.environ.get("LEARN_PASS", "")
    output_dir = RESULTS_DIR / _pass / str(bug_id) if _pass else RESULTS_DIR / str(bug_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Preflight: don't spin up a whole CRS run against a dead tunnel. Runs before
    # every attempt, so it also blocks (rather than crashes the campaign) when the
    # tunnel dies mid-run -- the next attempt waits for it to come back.
    if _uses_local_model(COMPOSE_FILE):
        wait_for_local_model()

    generate_fake_oss_fuzz_project(bug, project_dir)

    if not pov_path.exists():
        print(f"[{bug_id}] Extracting POC from ARVO image...")
        extract_poc(bug_id, pov_path)
    else:
        print(f"[{bug_id}] Using cached POC at {pov_path}")

    base = ["uv", "run", "oss-crs"]
    compose_args = ["--compose-file", str(COMPOSE_FILE)]

    if not skip_build:
        print(f"[{bug_id}] Building target ({bug['project']})...")
        subprocess.run(
            [*base, "build-target", *compose_args,
             "--fuzz-proj-path", str(project_dir),
             "--incremental-build"],
            cwd=OSS_CRS_DIR,
            check=True,
        )
    else:
        print(f"[{bug_id}] Skipping build (--skip-build set).")

    inject_heuristics(project_dir, sanitizer, bug_id)

    print(f"[{bug_id}] Running agent (harness: {bug['fuzz_target']})...")
    timeout = _run_timeout()
    if timeout:
        print(f"[{bug_id}] Wall-clock cap OSS_CRS_RUN_TIMEOUT={timeout:.0f}s in effect.")

    # In-turn self-check service (OSS_CRS_CHECK_PATCH=1): a background thread serves
    # the agent's check-patch requests against a warm -vul container for the duration
    # of this run. Best-effort and daemon, so it can never wedge or fail the run.
    check_marker = output_dir / ".check_passed"
    check_stop = check_thread = None
    if _check_patch_enabled():
        import threading
        import check_server
        from build_instance import build_instance
        # Fresh marker per run: only a PASS from THIS run should let a submission through.
        check_marker.unlink(missing_ok=True)
        check_stop = threading.Event()
        # Only latch a SHARED_DIR created after now, so a stale dir from a prior/killed
        # run can't win the newest-by-mtime race (observed live: the responder attached
        # to a dead campaign's channel and the agent got no working check-patch).
        svc_start = time.time()
        check_thread = threading.Thread(
            target=check_server.run_service,
            args=(bug, build_instance(bug), bug["project"]),
            kwargs={"find_dir": lambda: find_shared_dir(sanitizer, newer_than=svc_start),
                    "stop": check_stop.is_set, "marker_path": check_marker},
            daemon=True)
        check_thread.start()
        print(f"[{bug_id}] check-patch self-check service running (OSS_CRS_CHECK_PATCH=1)")

    run_start = time.time()
    try:
        timed_out = _run_agent_with_timeout(
            [*base, "run", *compose_args,
             "--fuzz-proj-path", str(project_dir),
             "--target-harness", bug["fuzz_target"],
             "--pov", str(pov_path),
             "--incremental-build"],
            cwd=OSS_CRS_DIR,
            timeout=timeout,
        )
    finally:
        if check_stop is not None:
            check_stop.set()
            check_thread.join(timeout=30)
    run_elapsed = time.time() - run_start
    if timed_out:
        print(f"[{bug_id}] Agent run hit the {timeout:.0f}s cap after {run_elapsed:.0f}s; "
              f"treating as a no-patch attempt. Any patch written before the cap is "
              f"still collected below.")
    run_dir = find_latest_run_dir(sanitizer)
    meta = {}
    patches = []
    if run_dir:
        meta_path = run_dir / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())

        patches = collect_patches(run_dir)
        for i, patch_file in enumerate(patches):
            dest = output_dir / f"oss_crs_patch_{i}.diff"
            dest.write_bytes(patch_file.read_bytes())
            print(f"[{bug_id}] Saved patch to {dest}")

        copy_session_files(run_dir, output_dir)
        print(f"[{bug_id}] Saved session files to {output_dir}")

    n_patches = meta.get("totals", {}).get("artifacts", {}).get("patches", 0)
    tokens = parse_token_counts(output_dir / "oss_crs_claude_stdout.log")
    summary = {
        "bug_id": bug_id,
        "project": bug["project"],
        "elapsed_seconds": round(run_elapsed),
        "timed_out": timed_out,
        # check_required: enforcement is on; check_passed: the agent got a check-patch
        # PASS this run. The repair loop rejects a submission that is required-but-unchecked.
        "check_required": _check_patch_enabled(),
        "check_passed": check_marker.exists(),
        "patches": n_patches,
        "patch_files": [str(output_dir / f"oss_crs_patch_{i}.diff") for i in range(len(patches))],
        "tokens": tokens,
        "meta": meta,
    }

    # Each run tags fresh multi-GB snapshot/compose images; reap the stale ones
    # now so a long campaign doesn't fill the disk.
    cleanup_docker_images()

    (output_dir / "oss_crs_result.json").write_text(json.dumps(summary, indent=2))
    print(f"[{bug_id}] Done. Patches: {n_patches}, elapsed: {run_elapsed:.0f}s, "
          f"tokens: {tokens['input_tokens']} in / {tokens['output_tokens']} out")
    return summary


if __name__ == "__main__":
    import sys

    bug_id = int(os.environ.get("OSS_CRS_BUG_ID", 42470179))
    skip_build = "--skip-build" in sys.argv

    summary = run_oss_crs(bug_id, skip_build=skip_build)
    print(json.dumps(summary, indent=2))
