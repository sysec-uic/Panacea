from pathlib import Path

from orientation import parse_crash_output

FIX = Path(__file__).parent / "fixtures"


def _asan():
    return (FIX / "crash_439494108_asan.txt").read_text()


def test_asan_crash_class():
    o = parse_crash_output(_asan(), "Stack-use-after-return READ 4", "mruby")
    assert o is not None
    assert o.crash_class == "stack-use-after-return"


def test_asan_fault_site_is_top_app_frame():
    o = parse_crash_output(_asan(), "Stack-use-after-return READ 4", "mruby")
    assert o.fault_site is not None
    assert o.fault_site.func == "limb_addmul_1"
    assert o.fault_site.path == "mrbgems/mruby-bigint/core/bigint.c"
    assert o.fault_site.line == 726


def test_asan_call_chain_is_app_frames_in_order():
    o = parse_crash_output(_asan(), "Stack-use-after-return READ 4", "mruby")
    funcs = [f.func for f in o.call_chain]
    # top app frames, in order, excluding libFuzzer/llvm runtime
    assert funcs[:4] == ["limb_addmul_1", "mpz_mul_basic", "mpz_mul", "bint_mul"]
    assert "LLVMFuzzerTestOneInput" not in funcs
    assert all("llvm-project" not in f.path for f in o.call_chain)


def test_asan_source_frame_is_root_cause():
    o = parse_crash_output(_asan(), "Stack-use-after-return READ 4", "mruby")
    assert o.source_frame is not None
    assert o.source_frame.func == "mrb_bint_reduce"
    assert o.source_frame.path == "mrbgems/mruby-bigint/core/bigint.c"
    assert o.source_frame.line == 3673


def _msan():
    return (FIX / "crash_440058794_msan.txt").read_text()


def test_msan_crash_class_and_fault_site():
    o = parse_crash_output(_msan(), "Use-of-uninitialized-value", "mruby")
    assert o.crash_class == "use-of-uninitialized-value"
    assert o.fault_site.func == "mrb_obj_hash_code"
    assert o.fault_site.path == "src/hash.c"
    assert o.fault_site.line == 332


def test_summary_line_captured_when_present():
    o = parse_crash_output(_asan(), "Stack-use-after-return READ 4", "mruby")
    assert o.summary_line is not None
    assert o.summary_line.startswith("SUMMARY:")


def test_empty_crash_output_returns_none():
    assert parse_crash_output("", "whatever", "mruby") is None
    assert parse_crash_output("   \n  ", "whatever", "mruby") is None


def test_nonempty_but_unparseable_returns_partial():
    o = parse_crash_output("garbage with no frames at all", "x", "mruby")
    assert o is not None
    assert o.fault_site is None
    assert o.call_chain == []
    assert o.raw_trace == "garbage with no frames at all"


from orientation import render_orientation, HEURISTICS_POINTER


def test_render_contains_key_fields():
    o = parse_crash_output(_asan(), "Stack-use-after-return READ 4", "mruby")
    md = render_orientation(o)
    assert "stack-use-after-return" in md
    assert "limb_addmul_1" in md
    assert "mrbgems/mruby-bigint/core/bigint.c:726" in md
    assert "mrb_bint_reduce" in md          # source frame
    assert "check-patch" in md              # directive to iterate
    assert "ORIENTATION.md" in HEURISTICS_POINTER


def test_render_partial_orientation_still_useful():
    o = parse_crash_output("some sanitizer text, no app frames", "x", "mruby")
    md = render_orientation(o)
    assert isinstance(md, str) and md.strip()   # never empty for a non-None Orientation
