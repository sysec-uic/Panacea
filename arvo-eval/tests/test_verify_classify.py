import json

import verify_fix
from verify_fix import classify_run

ASAN_CRASH = "==123==ERROR: AddressSanitizer: heap-use-after-free on address 0x..."
MSAN_CRASH = "==123==WARNING: MemorySanitizer: use-of-uninitialized-value"


def test_no_changes_when_empty_diff():
    assert classify_run(sanitizer="asan", diff="") == "no_changes"


def test_apply_failed():
    assert classify_run(sanitizer="asan", diff="x", apply_ok=False) == "patch_apply_failed"


def test_build_failed():
    assert classify_run(sanitizer="asan", diff="x", apply_ok=True, build_ok=False) == "build_failed"


def test_asan_still_crashes():
    assert classify_run(
        sanitizer="asan", diff="x", apply_ok=True, build_ok=True,
        run_output=ASAN_CRASH, run_returncode=1,
    ) == "still_crashes"


def test_msan_still_crashes_is_detected():
    # Regression: old code only grepped AddressSanitizer and missed MSan bugs.
    assert classify_run(
        sanitizer="msan", diff="x", apply_ok=True, build_ok=True,
        run_output=MSAN_CRASH, run_returncode=1,
    ) == "still_crashes"


def test_fixed_but_make_test_failed_is_not_correct():
    assert classify_run(
        sanitizer="asan", diff="x", apply_ok=True, build_ok=True,
        run_output="ok", run_returncode=0, make_test_ok=False,
    ) == "fixed_tests_failed"


def test_verified_correct():
    assert classify_run(
        sanitizer="asan", diff="x", apply_ok=True, build_ok=True,
        run_output="ok", run_returncode=0, make_test_ok=True,
    ) == "verified_correct"


def test_results_dir_respects_learn_pass(tmp_path, monkeypatch):
    # Under LEARN_PASS, verify must read/write the same namespaced dir the agent
    # writes to (results/<pass>/<bug_id>/), or it never finds the patch.
    monkeypatch.setattr(verify_fix, "RESULTS_DIR", tmp_path)
    monkeypatch.setenv("LEARN_PASS", "treatment")
    assert verify_fix.results_dir("439237851") == tmp_path / "treatment" / "439237851"
    monkeypatch.delenv("LEARN_PASS", raising=False)
    assert verify_fix.results_dir("439237851") == tmp_path / "439237851"


class _FakeProc:
    def __init__(self, returncode):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def test_apply_patch_uses_p1_first_and_stops_on_success():
    calls = []

    def fake_exec(cmd, diff):
        calls.append(cmd)
        return _FakeProc(0)

    result = verify_fix.apply_patch(fake_exec, "mruby", "DIFF")
    assert result.returncode == 0
    assert calls == ["git -C /src/mruby apply -p1 -"]


def test_apply_patch_retries_p2_when_p1_fails():
    # OSS-CRS nests the repo under mruby/, so its diffs carry an extra path
    # component and only apply at -p2. verify must fall back rather than reject.
    calls = []

    def fake_exec(cmd, diff):
        calls.append(cmd)
        return _FakeProc(0 if "-p2" in cmd else 1)

    result = verify_fix.apply_patch(fake_exec, "mruby", "DIFF")
    assert result.returncode == 0
    assert calls == ["git -C /src/mruby apply -p1 -", "git -C /src/mruby apply -p2 -"]


def test_apply_patch_returns_last_failure_when_all_strips_fail():
    def fake_exec(cmd, diff):
        return _FakeProc(1)

    result = verify_fix.apply_patch(fake_exec, "mruby", "DIFF")
    assert result.returncode == 1


def test_compile_env_maps_sanitizer_and_language():
    env = verify_fix.compile_env(
        {"language": "c++", "sanitizer": "asan", "fuzz_engine": "libfuzzer"}
    )
    assert env["FUZZING_LANGUAGE"] == "c++"
    assert env["SANITIZER"] == "address"
    assert env["FUZZING_ENGINE"] == "libfuzzer"
    assert env["ARCHITECTURE"] == "x86_64"


def test_compile_env_maps_msan_and_ubsan():
    assert verify_fix.compile_env({"sanitizer": "msan"})["SANITIZER"] == "memory"
    assert verify_fix.compile_env({"sanitizer": "ubsan"})["SANITIZER"] == "undefined"


def test_compile_env_defaults_when_metadata_missing():
    env = verify_fix.compile_env({})
    assert env["FUZZING_LANGUAGE"] == "c++"
    assert env["SANITIZER"] == "address"
    assert env["FUZZING_ENGINE"] == "libfuzzer"


def test_env_prefix_renders_shell_assignments():
    prefix = verify_fix.env_prefix({"A": "1", "B": "c++"})
    assert prefix == "A=1 B=c++"


def test_save_creates_namespaced_dir(tmp_path, monkeypatch):
    # Regression: save() used to write results/<bug_id>/verification.json with no
    # mkdir, crashing under the namespaced layout (FileNotFoundError).
    monkeypatch.setattr(verify_fix, "RESULTS_DIR", tmp_path)
    monkeypatch.setenv("LEARN_PASS", "treatment")
    verify_fix.save({"instance_id": "439237851"}, {"classification": "no_changes"})
    written = tmp_path / "treatment" / "439237851" / "verification.json"
    assert written.exists()
    assert json.loads(written.read_text())["classification"] == "no_changes"
