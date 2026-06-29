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


from differential_oracle import decide_label


def test_decide_label_no_fix_takes_precedence():
    assert decide_label(fix_image_available=False, errored=True,
                         divergences=[{"probe": "poc"}]) == "no_fix_available"


def test_decide_label_error():
    assert decide_label(fix_image_available=True, errored=True,
                        divergences=[]) == "oracle_error"


def test_decide_label_divergent():
    assert decide_label(fix_image_available=True, errored=False,
                        divergences=[{"probe": "poc"}]) == "divergent"


def test_decide_label_confirmed():
    assert decide_label(fix_image_available=True, errored=False,
                        divergences=[]) == "oracle_confirmed"


import differential_oracle as do_mod
from differential_oracle import grade, OracleError


class FakeOps:
    """Scripted ops: agent/fix probe outputs are dicts keyed by probe label.
    Probe label is 'poc' for the PoC probe and the script text otherwise."""
    def __init__(self, *, fix_available=True, agent=None, fix=None,
                 build_raises=False):
        self._fix_available = fix_available
        self._agent = agent or {}
        self._fix = fix or {}
        self._build_raises = build_raises
        self.cleaned = []

    def fix_image_available(self, local_id):
        return self._fix_available

    def build_agent(self, bug, diff):
        if self._build_raises:
            raise OracleError("build failed")
        return "agent-c"

    def start_fix(self, local_id):
        return "fix-c"

    def run_poc(self, container):
        side = self._agent if container == "agent-c" else self._fix
        return side.get("poc", (0, ""))

    def run_script(self, container, script_text):
        side = self._agent if container == "agent-c" else self._fix
        return side.get(script_text, (0, ""))

    def cleanup(self, container):
        self.cleaned.append(container)


BUG = {"localId": 439494108}


def test_grade_confirmed_when_all_probes_agree():
    ops = FakeOps(agent={"poc": (0, "ok\n"), "S": (0, "v=1\n")},
                  fix={"poc": (0, "ok\n"), "S": (0, "v=1\n")})
    res = grade(BUG, "diff", probes=[], poc_only=False,
                script_texts=["S"], ops=ops)
    assert res["label"] == "oracle_confirmed"
    assert res["fix_image_available"] is True
    assert res["divergences"] == []
    assert "agent-c" in ops.cleaned and "fix-c" in ops.cleaned


def test_grade_divergent_on_script_output():
    ops = FakeOps(agent={"poc": (0, "ok\n"), "S": (0, "v=9\n")},
                  fix={"poc": (0, "ok\n"), "S": (0, "v=1\n")})
    res = grade(BUG, "diff", probes=[], script_texts=["S"], ops=ops)
    assert res["label"] == "divergent"
    assert [d["probe"] for d in res["divergences"]] == ["S"]


def test_grade_no_fix_available_short_circuits():
    ops = FakeOps(fix_available=False)
    res = grade(BUG, "diff", probes=[], script_texts=["S"], ops=ops)
    assert res["label"] == "no_fix_available"
    assert res["fix_image_available"] is False
    assert ops.cleaned == []                 # nothing built


def test_grade_oracle_error_on_build_failure():
    ops = FakeOps(build_raises=True)
    res = grade(BUG, "diff", probes=[], script_texts=["S"], ops=ops)
    assert res["label"] == "oracle_error"
    assert "fix-c" not in ops.cleaned        # fix container never started
