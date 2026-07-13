"""Verify whether an agent's patch.diff actually fixes an ARVO bug.

Applies results/<bug_id>/patch.diff (written by run_single.py) to a fresh
container of the bug's vulnerable image, rebuilds, and reruns the crashing
input to classify the outcome.

Classifications:
- no_changes: the agent's diff was empty, nothing to verify
- patch_touches_harness: the diff modifies fuzz-harness scaffolding, not the project
- patch_apply_failed: the diff didn't apply cleanly to a fresh checkout
- build_failed: `compile` failed after applying the patch
- still_crashes: the rebuilt fuzz target still crashes on /tmp/poc
- unexpected_exit: crash gone but the target exited non-zero
- fixed_tests_failed: crash gone but `make test` (mruby correctness gate) failed
- verified_correct: crash gone and the correctness gate passed
"""

import argparse
import json
import os
import shlex
import subprocess
from pathlib import Path

from build_instance import build_instance, load_bug

RESULTS_DIR = Path(__file__).parent / "results"


def results_dir(instance_id) -> Path:
    """Per-bug results dir, namespaced by LEARN_PASS to match the producers
    (arvo_oss_crs.py / learn_loop.py) so control and treatment stay separate and
    verify reads the patch the agent actually wrote."""
    _pass = os.environ.get("LEARN_PASS", "")
    return RESULTS_DIR / _pass / str(instance_id) if _pass else RESULTS_DIR / str(instance_id)

COMPILE_TIMEOUT = 2400  # seconds - some projects take a long time to rebuild
RUN_TIMEOUT = 60
TEST_TIMEOUT = 1800

# `compile` rebuilds libmruby.a with whatever FUZZING_ENGINE/SANITIZER the bug
# needs (afl-clang-fast for AFL, -fsanitize=memory for MSan, etc). A plain
# `rake test` reuses that same libmruby.a for mrbtest -- a binary that carries no
# fuzzing driver -- so anything AFL- or coverage-instrumented leaves mrbtest with
# undefined __afl_area_ptr / __sanitizer_cov_* references at link time. Fixing
# this by threading the fuzzing-engine flags into the test build (as a previous
# version tried) only chases those symbols deeper (libAFLDriver.a needs its own
# __afl_fuzz_ptr/__afl_persistent_loop/... runtime, which needs afl-clang-fast to
# provide, and so on). The correctness gate doesn't need any of that: it just
# needs libmruby.a rebuilt CLEAN, with plain clang and only the sanitizer that
# matters for catching real regressions -- no fuzzing engine, no coverage
# instrumentation. Confirmed empirically against both an AFL bug (441405357) and
# an MSan bug (439291659): this reproduces the same clean pass as the untouched
# libfuzzer+asan bugs (1657/1666, 0 crashes).
MEMORY_TRACK_ORIGINS = " -fsanitize-memory-track-origins"


def mruby_test_cmd(bug: dict) -> str:
    sanitizer = SANITIZER_ENV.get((bug.get("sanitizer") or "").lower(), "address")
    extra = MEMORY_TRACK_ORIGINS if sanitizer == "memory" else ""
    flags = f"-fsanitize={sanitizer}{extra}"
    return (
        'cd /src/mruby && rake clean && '
        'export CC=clang CXX=clang++ '
        f'CFLAGS="-O1 {flags}" CXXFLAGS="-O1 {flags} -stdlib=libc++" LDFLAGS="{flags}" && '
        'rake test'
    )

# Crash signatures keyed by sanitizer. A rebuilt target "still crashes" if any
# of its sanitizer's signatures appears in the rerun output.
SANITIZER_SIGNATURES = {
    "asan": ("ERROR: AddressSanitizer", "SUMMARY: AddressSanitizer"),
    "msan": ("WARNING: MemorySanitizer", "ERROR: MemorySanitizer", "SUMMARY: MemorySanitizer"),
    "ubsan": ("runtime error:", "SUMMARY: UndefinedBehaviorSanitizer"),
}


def changed_paths(diff: str) -> list[str]:
    """Paths a git diff touches, in order, deduped. Reads both `--- a/` and
    `+++ b/` headers so deleted files (whose new side is /dev/null) still count."""
    paths = []
    for line in diff.splitlines():
        for prefix in ("--- a/", "+++ b/"):
            if line.startswith(prefix):
                p = line[len(prefix):].strip()
                if p and p not in paths:
                    paths.append(p)
    return paths


def touches_harness(diff: str) -> bool:
    """True if the diff modifies fuzz-harness scaffolding rather than the project.

    A harness rewrite can dodge the PoC AND pass the project's test suite AND show
    no divergence to the differential oracle (none of them exercise the harness), so
    this path check is the only gate that catches it. Deployed harnesses can't
    change, so such a patch is never a fix.
    """
    for path in changed_paths(diff):
        parts = path.split("/")
        if "oss-fuzz" in parts or "fuzz" in parts[-1].lower():
            return True
    return False


def crashed(sanitizer: str, run_output: str) -> bool:
    sigs = SANITIZER_SIGNATURES.get(sanitizer.lower(), ("ERROR: AddressSanitizer",))
    return any(sig in run_output for sig in sigs)


def classify_run(
    *,
    sanitizer: str,
    diff: str,
    apply_ok: bool = True,
    build_ok: bool = True,
    run_output: str = "",
    run_returncode: int = 0,
    make_test_ok: bool | None = None,
) -> str:
    """Pure classification of a verification run. No Docker, fully testable.

    Returns one of: no_changes, patch_touches_harness, patch_apply_failed,
    build_failed, still_crashes, unexpected_exit, fixed_tests_failed,
    verified_correct.
    """
    if not diff.strip():
        return "no_changes"
    if touches_harness(diff):
        return "patch_touches_harness"
    if not apply_ok:
        return "patch_apply_failed"
    if not build_ok:
        return "build_failed"
    if crashed(sanitizer, run_output):
        return "still_crashes"
    if run_returncode != 0:
        return "unexpected_exit"
    # Crash is gone. Correctness gate (v1): make test must pass.
    if make_test_ok is False:
        return "fixed_tests_failed"
    return "verified_correct"


def docker_exec(container: str, command: str, *, input: str | None = None, timeout: int) -> subprocess.CompletedProcess:
    cmd = ["docker", "exec"]
    if input is not None:
        cmd.append("-i")
    cmd += [container, "bash", "-lc", command]
    return subprocess.run(cmd, input=input, text=True, capture_output=True, timeout=timeout)


SANITIZER_ENV = {"asan": "address", "msan": "memory", "ubsan": "undefined"}


def compile_env(bug: dict) -> dict:
    """OSS-Fuzz build env the ARVO `compile` wrapper requires (it runs under
    `set -u`). ARVO containers don't reliably carry these, so derive them from the
    bug metadata, falling back to OSS-Fuzz's own defaults."""
    return {
        "FUZZING_LANGUAGE": bug.get("language") or "c++",
        "SANITIZER": SANITIZER_ENV.get((bug.get("sanitizer") or "").lower(), "address"),
        "FUZZING_ENGINE": bug.get("fuzz_engine") or "libfuzzer",
        "ARCHITECTURE": "x86_64",
    }


def env_prefix(env: dict) -> str:
    """Render an env dict as a shell command prefix: `K=v K2=v2`."""
    return " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())


def apply_patch(exec_fn, project: str, diff: str):
    """Apply `diff` in the bug's source tree, tolerating an extra leading path
    component. OSS-CRS nests the repo under a project-named dir, so its diffs carry
    an extra prefix and only apply at -p2; plain diffs apply at -p1. Try -p1 first,
    fall back to -p2. `exec_fn(command, diff)` returns a CompletedProcess. Returns
    the first successful attempt, else the last one."""
    result = None
    for strip in (1, 2):
        result = exec_fn(f"git -C /src/{project} apply -p{strip} -", diff)
        if result.returncode == 0:
            return result
    return result


def save(instance: dict, verification: dict) -> dict:
    out_path = results_dir(instance["instance_id"]) / "verification.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(verification, indent=2))
    print(json.dumps(verification, indent=2))

    classification = verification["classification"]
    print(f"\n=== RESULT: {classification.upper()} ===")
    print(f"Full details saved to {out_path}")
    return verification


def verify(bug_id: int, keep: bool = False) -> dict:
    bug = load_bug(bug_id)
    instance = build_instance(bug)
    project = instance["project"]

    diff_path = results_dir(instance["instance_id"]) / "patch.diff"
    diff = diff_path.read_text() if diff_path.exists() else ""

    verification = {"instance_id": instance["instance_id"], "project": project}

    if not diff.strip():
        verification["classification"] = "no_changes"
        return save(instance, verification)

    if touches_harness(diff):
        verification["classification"] = "patch_touches_harness"
        verification["harness_paths"] = changed_paths(diff)
        return save(instance, verification)

    container = f"arvo-{instance['instance_id']}-verify"
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--name", container, instance["image_name"], "sleep", str(COMPILE_TIMEOUT + 600)],
        check=True,
        capture_output=True,
    )

    try:
        apply_result = apply_patch(
            lambda cmd, patch: docker_exec(container, cmd, input=patch, timeout=60),
            project, diff,
        )
        if apply_result.returncode != 0:
            verification["classification"] = "patch_apply_failed"
            verification["apply_output"] = apply_result.stdout + apply_result.stderr
            return save(instance, verification)

        # Cap ninja's parallelism: this host has more cores than RAM can support for a
        # full-parallel clang+ASan build, which previously crashed the WSL2 VM.
        docker_exec(container, "sed -i 's#/depot_tools/ninja -C#/depot_tools/ninja -j3 -C#g' /src/build.sh 2>/dev/null || true", timeout=30)
        build_result = docker_exec(container, f"cd /src/{project} && {env_prefix(compile_env(bug))} compile", timeout=COMPILE_TIMEOUT)
        build_output = build_result.stdout + build_result.stderr
        verification["build_output_tail"] = "\n".join(build_output.splitlines()[-50:])
        if build_result.returncode != 0:
            verification["classification"] = "build_failed"
            return save(instance, verification)

        run_result = docker_exec(container, "arvo", timeout=RUN_TIMEOUT)
        run_output = run_result.stdout + run_result.stderr
        verification["run_output_tail"] = "\n".join(run_output.splitlines()[-30:])
        verification["run_returncode"] = run_result.returncode

        sanitizer = bug["sanitizer"].lower()
        make_test_ok = None
        if not crashed(sanitizer, run_output) and run_result.returncode == 0 and project == "mruby":
            test_result = docker_exec(container, mruby_test_cmd(bug), timeout=TEST_TIMEOUT)
            make_test_ok = test_result.returncode == 0
            verification["make_test_ok"] = make_test_ok
            verification["make_test_tail"] = "\n".join(
                (test_result.stdout + test_result.stderr).splitlines()[-30:]
            )

        verification["classification"] = classify_run(
            sanitizer=sanitizer,
            diff=diff,
            apply_ok=True,
            build_ok=True,
            run_output=run_output,
            run_returncode=run_result.returncode,
            make_test_ok=make_test_ok,
        )
        return save(instance, verification)
    finally:
        if keep:
            verification["container"] = container
        else:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bug_id", type=int)
    parser.add_argument("--keep", action="store_true", help="keep the verification container around for debugging")
    args = parser.parse_args()
    verify(args.bug_id, keep=args.keep)
