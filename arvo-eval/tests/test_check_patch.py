"""The in-turn self-check engine: run_check (container-agnostic, exec_fn injected so
it drives a warm reusable container) and check_feedback (agent-facing verdict text)."""
import verify_fix
from verify_fix import run_check, check_feedback


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_exec(rules):
    """rules: list of (substring, _FakeProc). First matching substring wins; the
    command is recorded so tests can assert what ran (and what DIDN'T)."""
    calls = []

    def exec_fn(command, input=None, timeout=None):
        calls.append(command)
        for sub, proc in rules:
            if sub in command:
                return proc
        return _FakeProc(0)

    exec_fn.calls = calls
    return exec_fn


BUG = {"localId": 449429295, "sanitizer": "asan", "project": "mruby"}
DIFF = "--- a/mrbgems/mruby-bigint/core/bigint.c\n+++ b/mrbgems/mruby-bigint/core/bigint.c\n@@ -1 +1 @@\n-x\n+y\n"


def test_run_check_verified_correct_when_crash_gone_and_tests_pass():
    ex = make_exec([("apply -p1", _FakeProc(0)), ("build.sh", _FakeProc(0)),
                    ("compile", _FakeProc(0)), ("arvo", _FakeProc(0, "clean run")),
                    ("rake test", _FakeProc(0))])
    v = run_check(BUG, DIFF, ex, project="mruby")
    assert v["classification"] == "verified_correct"
    assert v["make_test_ok"] is True


def test_run_check_still_crashes_stops_before_tests():
    ex = make_exec([("apply -p1", _FakeProc(0)), ("compile", _FakeProc(0)),
                    ("arvo", _FakeProc(1, "==1==ERROR: AddressSanitizer: heap-use-after-free"))])
    v = run_check(BUG, DIFF, ex, project="mruby")
    assert v["classification"] == "still_crashes"
    assert not any("rake test" in c for c in ex.calls)   # no point testing a still-crashing build


def test_run_check_fixed_tests_failed():
    ex = make_exec([("apply -p1", _FakeProc(0)), ("compile", _FakeProc(0)),
                    ("arvo", _FakeProc(0, "clean")), ("rake test", _FakeProc(1, "1 failure"))])
    v = run_check(BUG, DIFF, ex, project="mruby")
    assert v["classification"] == "fixed_tests_failed"


def test_run_check_build_failed_stops_before_run():
    ex = make_exec([("apply -p1", _FakeProc(0)), ("compile", _FakeProc(1, "error: undefined"))])
    v = run_check(BUG, DIFF, ex, project="mruby")
    assert v["classification"] == "build_failed"
    assert not any(c == "arvo" for c in ex.calls)


def test_run_check_patch_apply_failed():
    ex = make_exec([("apply", _FakeProc(1, "does not apply"))])
    v = run_check(BUG, DIFF, ex, project="mruby")
    assert v["classification"] == "patch_apply_failed"


def test_run_check_harness_patch_short_circuits_without_touching_container():
    harness = "--- a/mruby/oss-fuzz/proto_to_ruby.cpp\n+++ b/mruby/oss-fuzz/proto_to_ruby.cpp\n"
    ex = make_exec([])
    v = run_check(BUG, harness, ex, project="mruby")
    assert v["classification"] == "patch_touches_harness"
    assert ex.calls == []   # never compiles/runs a harness rewrite


def test_run_check_empty_diff_is_no_changes():
    ex = make_exec([])
    assert run_check(BUG, "", ex, project="mruby")["classification"] == "no_changes"
    assert ex.calls == []


# check_feedback: the agent must hear a clear PASS as well as actionable failures.
def test_feedback_pass_tells_agent_to_submit():
    fb = check_feedback({"classification": "verified_correct"})
    assert "PASS" in fb and "submit" in fb.lower()


def test_feedback_still_crashes_includes_trace_tail():
    fb = check_feedback({"classification": "still_crashes", "run_output_tail": "AddressSanitizer: heap-use-after-free"})
    assert "still crashes" in fb.lower()
    assert "heap-use-after-free" in fb


def test_feedback_build_failed_includes_build_tail():
    fb = check_feedback({"classification": "build_failed", "build_output_tail": "error: expected ';'"})
    assert "compile" in fb.lower() and "expected ';'" in fb


def test_feedback_tests_failed_flags_wrong_behavior():
    fb = check_feedback({"classification": "fixed_tests_failed", "make_test_tail": "assert_equal failed"})
    assert "rake test" in fb and "assert_equal failed" in fb


def test_feedback_harness_redirects_to_project_source():
    fb = check_feedback({"classification": "patch_touches_harness"})
    assert "harness" in fb.lower() and "source" in fb.lower()


def test_feedback_never_leaks_the_fix_image():
    for cls in ("verified_correct", "still_crashes", "build_failed", "fixed_tests_failed",
                "patch_touches_harness", "patch_apply_failed", "no_changes", "unexpected_exit"):
        fb = check_feedback({"classification": cls})
        assert "-fix" not in fb and "ground-truth" not in fb.lower()
