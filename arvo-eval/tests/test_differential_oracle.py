from pathlib import Path

from differential_oracle import default_probes


def test_default_probes_finds_committed_scripts():
    probes = default_probes()
    names = [p.name for p in probes]
    assert "01_integer_arith.rb" in names
    assert all(p.suffix == ".rb" for p in probes)
    assert names == sorted(names)               # deterministic order
    assert all(Path(p).read_text().strip() for p in probes)  # non-empty


from differential_oracle import normalize


def test_normalize_strips_addresses_pids_and_sanitizer_banner():
    raw = (
        "==12345==ERROR: AddressSanitizer: heap-buffer-overflow\n"
        "    #0 0x7f3a1b2c3d4e in foo /src/mruby/x.c:10\n"
        "SUMMARY: AddressSanitizer: heap-buffer-overflow\n"
        "result=42\n"
    )
    out = normalize(raw)
    assert "0x7f3a1b2c3d4e" not in out
    assert "12345" not in out
    assert "AddressSanitizer" not in out
    assert "SUMMARY:" not in out
    assert "result=42" in out


def test_normalize_equal_modulo_noise():
    a = "==1==WARNING: MemorySanitizer: foo\nvalue=7\n"
    b = "==999==WARNING: MemorySanitizer: foo\nvalue=7\n"
    assert normalize(a) == normalize(b)


def test_normalize_keeps_semantic_difference():
    assert normalize("value=7\n") != normalize("value=8\n")


from differential_oracle import outputs_diverge


def test_outputs_diverge_exit_mismatch():
    assert outputs_diverge((0, "x"), (1, "x")) == "exit"


def test_outputs_diverge_stdout_mismatch():
    assert outputs_diverge((0, "value=7\n"), (0, "value=8\n")) == "stdout"


def test_outputs_diverge_agree_modulo_noise():
    a = (0, "==1==WARNING: MemorySanitizer: q\nvalue=7\n")
    b = (0, "==2==WARNING: MemorySanitizer: q\nvalue=7\n")
    assert outputs_diverge(a, b) is None
