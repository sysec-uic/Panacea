"""Run crs-claude-code (OSS-CRS) on ARVO bugs.

Creates a minimal fake OSS-Fuzz project directory per bug that wraps the
ARVO Docker image, then drives OSS-CRS build-target + run against it.

Usage:
    python arvo_oss_crs.py                     # runs BUG_ID (default or env var)
    OSS_CRS_BUG_ID=40096184 python arvo_oss_crs.py

Prerequisites:
    - ~/oss-crs cloned from https://github.com/ossf/oss-crs
    - CLAUDE_CODE_OAUTH_TOKEN exported in your shell (or set in .env)
    - Run `uv run oss-crs prepare --compose-file <COMPOSE_FILE>` once first
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from build_instance import load_bug

OSS_CRS_DIR = Path.home() / "oss-crs"
COMPOSE_FILE = OSS_CRS_DIR / "example/crs-claude-code/compose-oauth.yaml"
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


def inject_heuristics(project_dir: Path, sanitizer: str, bug_id: int) -> None:
    """Copy HEURISTICS.md from project_dir into the agent's source directory.

    HEURISTICS.md is written to the fake OSS-Fuzz project dir by injector.py
    but that dir never reaches the agent.  This bridges the gap by copying it
    directly into the extracted target-source dir where the agent runs.
    """
    src = project_dir / "HEURISTICS.md"
    if not src.exists():
        return
    target_source = find_target_source_dir(sanitizer)
    if target_source is None:
        print(f"[{bug_id}] Warning: could not find target-source dir, skipping heuristics injection")
        return
    shutil.copy2(src, target_source / "HEURISTICS.md")
    print(f"[{bug_id}] Injected HEURISTICS.md into {target_source}")


def collect_patches(run_dir: Path) -> list[Path]:
    """Find patch diff files the agent produced in this run."""
    return list(run_dir.glob("**/SUBMIT_DIR/*/patches/*.diff"))


def copy_session_files(run_dir: Path, output_dir: Path) -> None:
    """Copy claude_stdout.log to output_dir."""
    for log in run_dir.glob("crs/crs-claude-code/*/LOG_DIR/*/agent/claude_stdout.log"):
        shutil.copy2(log, output_dir / "oss_crs_claude_stdout.log")


def run_oss_crs(bug_id: int, skip_build: bool = False) -> dict:
    """Run crs-claude-code on one ARVO bug. Returns a summary dict."""
    bug = load_bug(bug_id)
    sanitizer = bug["sanitizer"].lower()

    project_dir = PROJECTS_DIR / str(bug_id) / "project"
    pov_path = PROJECTS_DIR / str(bug_id) / "poc"
    output_dir = RESULTS_DIR / str(bug_id)
    output_dir.mkdir(parents=True, exist_ok=True)

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
    run_start = time.time()
    subprocess.run(
        [*base, "run", *compose_args,
         "--fuzz-proj-path", str(project_dir),
         "--target-harness", bug["fuzz_target"],
         "--pov", str(pov_path),
         "--incremental-build"],
        cwd=OSS_CRS_DIR,
        check=False,
    )
    run_elapsed = time.time() - run_start
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
    summary = {
        "bug_id": bug_id,
        "project": bug["project"],
        "elapsed_seconds": round(run_elapsed),
        "patches": n_patches,
        "patch_files": [str(output_dir / f"oss_crs_patch_{i}.diff") for i in range(len(patches))],
        "meta": meta,
    }

    (output_dir / "oss_crs_result.json").write_text(json.dumps(summary, indent=2))
    print(f"[{bug_id}] Done. Patches: {n_patches}, elapsed: {run_elapsed:.0f}s")
    return summary


if __name__ == "__main__":
    import sys

    bug_id = int(os.environ.get("OSS_CRS_BUG_ID", 42470179))
    skip_build = "--skip-build" in sys.argv

    summary = run_oss_crs(bug_id, skip_build=skip_build)
    print(json.dumps(summary, indent=2))
