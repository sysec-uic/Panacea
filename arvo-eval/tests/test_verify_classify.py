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
