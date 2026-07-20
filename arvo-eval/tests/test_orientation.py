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
