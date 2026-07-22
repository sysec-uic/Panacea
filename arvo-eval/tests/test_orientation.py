from pathlib import Path

from orientation import _source_frame, parse_crash_output

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


# --- Item 1: frames without a line number must not be dropped ---------------


def _lineless_top_frame_trace():
    # #0 has no ":line" (matches real traces, e.g. mrb_vm_exec /src/mruby/src/vm.c
    # in the msan fixture). It must still become the fault site.
    return (
        "==1==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000000\n"
        "    #0 0x55e851c07304 in mrb_vm_exec /src/mruby/src/vm.c\n"
        "    #1 0x55e851c08000 in mrb_run /src/mruby/src/vm.c:3369:10\n"
        "SUMMARY: AddressSanitizer: SEGV /src/mruby/src/vm.c in mrb_vm_exec\n"
    )


def test_lineless_top_frame_becomes_fault_site_with_line_none():
    o = parse_crash_output(_lineless_top_frame_trace(), "SEGV on unknown address", "mruby")
    assert o is not None
    assert o.fault_site is not None
    assert o.fault_site.func == "mrb_vm_exec"
    assert o.fault_site.path == "src/vm.c"
    assert o.fault_site.line is None


# --- Item 2: trimming must not drop SUMMARY / the source frame --------------


def test_trim_trace_preserves_summary_and_source_frame_on_long_msan_trace():
    o = parse_crash_output(_msan(), "Use-of-uninitialized-value", "mruby")
    md = render_orientation(o)
    assert "SUMMARY" in md
    assert o.source_frame is not None
    assert o.source_frame.func in md


# --- Item 3: SEGV-class crashes must not parse to crash_class=None ----------


def test_segv_crash_class_falls_back_to_normalized_crash_type():
    o = parse_crash_output(_lineless_top_frame_trace(), "SEGV on unknown address", "mruby")
    assert o is not None
    assert o.crash_class == "segv-on-unknown-address"


# --- Item 6: MSan source_frame must be exact ---------------------------------


def test_msan_source_frame_is_root_cause():
    o = parse_crash_output(_msan(), "Use-of-uninitialized-value", "mruby")
    assert o.source_frame is not None
    assert o.source_frame.func == "mrb_basic_alloc_func"
    assert o.source_frame.path == "src/allocf.c"
    assert o.source_frame.line == 30


# --- Minor: _source_frame returns None when marker has no app frame after ---


def test_source_frame_none_when_marker_present_but_no_app_frame_follows():
    text = (
        "previously allocated by\n"
        "    #0 0x1 in malloc "
        "/src/llvm-project/compiler-rt/lib/asan/asan_malloc_linux.cpp:52:3\n"
    )
    assert _source_frame(text, "/src/mruby/") is None
