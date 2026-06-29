# Differential `-fix` Oracle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a post-hoc grader that compares the agent's accepted patch against the canonical `n132/arvo:{id}-fix` image and uses the verdict to veto wrong heuristics and promote confirmed ones — without ever exposing `-fix` to the agent.

**Architecture:** A new `differential_oracle.py` module with three pure functions (`normalize`, `outputs_diverge`, `decide_label`) and one Docker-driven orchestrator (`grade`) that talks to containers through an injectable `ops` object so the orchestration is unit-testable without Docker. `learn_loop.py` calls `grade` after `repair_with_retries` returns and before `add_heuristic`, applying veto-and-promote. A committed mruby probe battery plus the crash PoC are the comparison inputs.

**Tech Stack:** Python 3 (stdlib + `subprocess`/Docker), pytest, the existing `arvo-eval` modules (`verify_fix.docker_exec`, `build_instance.load_bug`, `playbook_store`, `ledger`).

---

## Scope & Deviations from the Spec

- **Spec §4.3 / Phase 4 (build reuse):** `repair_with_retries` discards the verification dict, so the agent-patched container is not available downstream. Threading a live container through the holdout retry loop is risky for a one-compile saving on a 30-bug pilot. **Decision:** `grade` builds its own agent container by default; the `patched_container` parameter exists as a hook for a future optimization but is not wired through `repair_loop` in this plan. This is the only intentional deviation.
- Everything else implements the spec as written.

## File Structure

- **Create** `arvo-eval/differential_oracle.py` — `normalize`, `outputs_diverge`, `decide_label` (pure); `OracleError`, `DockerOps`, `default_probes`, `grade` (orchestration).
- **Create** `arvo-eval/differential/mruby_probes/*.rb` — committed deterministic probe scripts.
- **Modify** `arvo-eval/playbook_store.py` — surface an `oracle=confirmed` trust marker in `render_playbook`.
- **Modify** `arvo-eval/learn_loop.py` — `_default_grade`, veto-and-promote at the solved block, ledger fields, `grade` collaborator param.
- **Create** `arvo-eval/tests/test_differential_oracle.py` — unit tests for the pure functions and `grade` (via a `FakeOps`).
- **Modify** `arvo-eval/tests/test_playbook_store.py` — trust-marker rendering test.
- **Modify** `arvo-eval/tests/test_learn_loop_dryrun.py` — veto/promote/no-fix behavior with a stubbed grader.
- **Modify** `arvo-eval/tests/test_repair_loop.py` — wall test: grader unreachable from the repair/feedback path.

Run all tests with: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests -q`

---

### Task 1: Probe battery + loader

**Files:**
- Create: `arvo-eval/differential/mruby_probes/01_integer_arith.rb`
- Create: `arvo-eval/differential/mruby_probes/02_string_ops.rb`
- Create: `arvo-eval/differential/mruby_probes/03_array_hash.rb`
- Create: `arvo-eval/differential_oracle.py`
- Test: `arvo-eval/tests/test_differential_oracle.py`

These three probes use only core mruby (no optional gems) so they run on any container's `bin/mruby`. Bignum/set probes that exercise the bug-cluster gems are added during the Phase 0 spike (Task 10) only after confirming the container's `bin/mruby` includes those gems.

- [ ] **Step 1: Write the three probe scripts**

`01_integer_arith.rb`:
```ruby
# Deterministic core-integer arithmetic. No optional gems.
puts (123456 * 789 + 42).to_s
puts (1000000007 % 97).to_s
puts (-25 / 4).to_s
puts (1 << 30).to_s
```

`02_string_ops.rb`:
```ruby
# Deterministic string operations.
s = "panacea" * 3
puts s.length
puts s.upcase
puts s.reverse
puts ("a".."f").to_a.join(",")
```

`03_array_hash.rb`:
```ruby
# Deterministic array/hash operations.
a = (1..10).to_a.map { |x| x * x }
puts a.inject(0) { |acc, x| acc + x }
h = {}
a.each_with_index { |v, i| h[i] = v }
puts h.keys.sort.join(",")
puts h.values.sort.reverse.join(",")
```

- [ ] **Step 2: Write the failing test for the loader**

Create `arvo-eval/tests/test_differential_oracle.py`:
```python
from pathlib import Path

from differential_oracle import default_probes


def test_default_probes_finds_committed_scripts():
    probes = default_probes()
    names = [p.name for p in probes]
    assert "01_integer_arith.rb" in names
    assert all(p.suffix == ".rb" for p in probes)
    assert names == sorted(names)               # deterministic order
    assert all(Path(p).read_text().strip() for p in probes)  # non-empty
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests/test_differential_oracle.py::test_default_probes_finds_committed_scripts -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'differential_oracle'`.

- [ ] **Step 4: Create the module with the loader**

Create `arvo-eval/differential_oracle.py`:
```python
"""Differential -fix oracle: grade a learned lesson by comparing the agent's
accepted patch against the canonical n132/arvo:{id}-fix image.

Runs ONLY in the learning path (learn_loop), after the agent has produced its
accepted patch. Its output feeds the ledger and the add/suppress/confidence
decision -- never agent feedback. The deployment-faithful -fix wall is preserved.
"""
from pathlib import Path

PROBE_DIR = Path(__file__).parent / "differential" / "mruby_probes"


def default_probes() -> list[Path]:
    """Committed mruby probe scripts, in deterministic (sorted) order."""
    return sorted(PROBE_DIR.glob("*.rb"))
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests/test_differential_oracle.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add arvo-eval/differential/mruby_probes arvo-eval/differential_oracle.py arvo-eval/tests/test_differential_oracle.py
git commit -m "feat(oracle): committed mruby probe battery + loader"
```

---

### Task 2: `normalize` — strip non-deterministic output noise

**Files:**
- Modify: `arvo-eval/differential_oracle.py`
- Test: `arvo-eval/tests/test_differential_oracle.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_differential_oracle.py`:
```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests/test_differential_oracle.py::test_normalize_strips_addresses_pids_and_sanitizer_banner -v`
Expected: FAIL with `ImportError: cannot import name 'normalize'`.

- [ ] **Step 3: Implement `normalize`**

Add to `differential_oracle.py` (after the imports, add `import re`):
```python
import re

_NOISE_PATTERNS = [
    re.compile(r"==\d+==.*$", re.MULTILINE),          # sanitizer ==PID== banner lines
    re.compile(r"^SUMMARY:.*$", re.MULTILINE),        # sanitizer summary line
    re.compile(r"0x[0-9a-fA-F]+"),                    # hex addresses
    re.compile(r"\b(?:AddressSanitizer|MemorySanitizer|UndefinedBehaviorSanitizer)\b"),
    re.compile(r"(?:pid|tid)[=: ]*\d+", re.IGNORECASE),
    re.compile(r"\bin \d+(?:\.\d+)? ?(?:ms|s)\b"),    # timing fragments
]


def normalize(text: str) -> str:
    """Strip non-deterministic / sanitizer noise so only semantic output remains."""
    out = text
    for pat in _NOISE_PATTERNS:
        out = pat.sub("", out)
    # Collapse trailing whitespace and drop now-empty lines.
    lines = [ln.rstrip() for ln in out.splitlines()]
    return "\n".join(ln for ln in lines if ln.strip())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests/test_differential_oracle.py -k normalize -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/differential_oracle.py arvo-eval/tests/test_differential_oracle.py
git commit -m "feat(oracle): output normalization for differential comparison"
```

---

### Task 3: `outputs_diverge` — compare one probe across builds

**Files:**
- Modify: `arvo-eval/differential_oracle.py`
- Test: `arvo-eval/tests/test_differential_oracle.py`

A probe result is a `(exit_code, combined_output)` tuple. Divergence is exit-code mismatch first, then normalized-stdout mismatch.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_differential_oracle.py`:
```python
from differential_oracle import outputs_diverge


def test_outputs_diverge_exit_mismatch():
    assert outputs_diverge((0, "x"), (1, "x")) == "exit"


def test_outputs_diverge_stdout_mismatch():
    assert outputs_diverge((0, "value=7\n"), (0, "value=8\n")) == "stdout"


def test_outputs_diverge_agree_modulo_noise():
    a = (0, "==1==WARNING: MemorySanitizer: q\nvalue=7\n")
    b = (0, "==2==WARNING: MemorySanitizer: q\nvalue=7\n")
    assert outputs_diverge(a, b) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests/test_differential_oracle.py::test_outputs_diverge_exit_mismatch -v`
Expected: FAIL with `ImportError: cannot import name 'outputs_diverge'`.

- [ ] **Step 3: Implement `outputs_diverge`**

Add to `differential_oracle.py`:
```python
def outputs_diverge(agent: tuple[int, str], fix: tuple[int, str]) -> str | None:
    """Return 'exit', 'stdout', or None for one probe's agent-vs-fix comparison."""
    agent_exit, agent_out = agent
    fix_exit, fix_out = fix
    if agent_exit != fix_exit:
        return "exit"
    if normalize(agent_out) != normalize(fix_out):
        return "stdout"
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests/test_differential_oracle.py -k diverge -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/differential_oracle.py arvo-eval/tests/test_differential_oracle.py
git commit -m "feat(oracle): per-probe divergence comparison"
```

---

### Task 4: `decide_label` — map results to a label

**Files:**
- Modify: `arvo-eval/differential_oracle.py`
- Test: `arvo-eval/tests/test_differential_oracle.py`

Precedence: no fix image → `no_fix_available`; grader errored → `oracle_error`; any divergence → `divergent`; else `oracle_confirmed`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_differential_oracle.py`:
```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests/test_differential_oracle.py -k decide_label -v`
Expected: FAIL with `ImportError: cannot import name 'decide_label'`.

- [ ] **Step 3: Implement `decide_label`**

Add to `differential_oracle.py`:
```python
def decide_label(*, fix_image_available: bool, errored: bool, divergences: list) -> str:
    if not fix_image_available:
        return "no_fix_available"
    if errored:
        return "oracle_error"
    if divergences:
        return "divergent"
    return "oracle_confirmed"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests/test_differential_oracle.py -k decide_label -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/differential_oracle.py arvo-eval/tests/test_differential_oracle.py
git commit -m "feat(oracle): label decision logic"
```

---

### Task 5: `grade` orchestration (DockerOps injected as `ops`)

**Files:**
- Modify: `arvo-eval/differential_oracle.py`
- Test: `arvo-eval/tests/test_differential_oracle.py`

`grade` walks the probe battery through an injectable `ops` object so orchestration is testable without Docker. The real `DockerOps` is added in Task 6.

`ops` interface (duck-typed; `FakeOps` in the test mirrors it):
```
fix_image_available(local_id: int) -> bool
build_agent(bug: dict, diff: str) -> str          # container name; raises OracleError on build/apply fail
start_fix(local_id: int) -> str                   # container name
run_poc(container: str) -> tuple[int, str]
run_script(container: str, script_text: str) -> tuple[int, str]
cleanup(container: str) -> None
```

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_differential_oracle.py`:
```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests/test_differential_oracle.py -k grade -v`
Expected: FAIL with `ImportError: cannot import name 'OracleError'`.

- [ ] **Step 3: Implement `OracleError` and `grade`**

Add to `differential_oracle.py` (place `OracleError` near the top, after imports):
```python
class OracleError(Exception):
    """Raised when the grader cannot complete (build/run failure). Mapped to
    the 'oracle_error' label so a flaky grader never costs a real lesson."""


def grade(bug, agent_diff, *, probes=None, script_texts=None,
          patched_container=None, poc_only=False, ops=None) -> dict:
    """Compare the agent-patched build against n132/arvo:{id}-fix.

    Returns {label, fix_image_available, divergences}. The agent never sees this.
    `script_texts` overrides reading `probes` from disk (used by tests); in
    production `probes` defaults to `default_probes()` and their text is read here.
    """
    if ops is None:
        ops = DockerOps()
    local_id = bug["localId"]

    if not ops.fix_image_available(local_id):
        return {"label": "no_fix_available", "fix_image_available": False, "divergences": []}

    if script_texts is None:
        probe_paths = default_probes() if probes is None else list(probes)
        script_texts = [p.read_text() for p in probe_paths]
        script_labels = [p.name for p in probe_paths]
    else:
        script_labels = list(script_texts)

    own_agent = patched_container is None
    agent_c = patched_container
    fix_c = None
    try:
        if own_agent:
            agent_c = ops.build_agent(bug, agent_diff)   # may raise OracleError
        fix_c = ops.start_fix(local_id)

        divergences = []
        a, f = ops.run_poc(agent_c), ops.run_poc(fix_c)
        kind = outputs_diverge(a, f)
        if kind:
            divergences.append({"probe": "poc", "kind": kind})

        if not poc_only:
            for label, text in zip(script_labels, script_texts):
                a, f = ops.run_script(agent_c, text), ops.run_script(fix_c, text)
                kind = outputs_diverge(a, f)
                if kind:
                    divergences.append({"probe": label, "kind": kind})

        label = decide_label(fix_image_available=True, errored=False, divergences=divergences)
        return {"label": label, "fix_image_available": True, "divergences": divergences}
    except (OracleError, subprocess.SubprocessError) as exc:
        return {"label": "oracle_error", "fix_image_available": True,
                "divergences": [], "error": str(exc)}
    finally:
        if own_agent and agent_c:
            ops.cleanup(agent_c)
        if fix_c:
            ops.cleanup(fix_c)
```

Add `import subprocess` to the module imports.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests/test_differential_oracle.py -k grade -v`
Expected: 4 PASS.

- [ ] **Step 5: Run the whole oracle test file**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests/test_differential_oracle.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add arvo-eval/differential_oracle.py arvo-eval/tests/test_differential_oracle.py
git commit -m "feat(oracle): grade orchestration with injectable ops"
```

---

### Task 6: `DockerOps` — real container implementation

**Files:**
- Modify: `arvo-eval/differential_oracle.py`

No unit test (Docker-dependent; exercised by the Task 10 integration check). It reuses `verify_fix.docker_exec` and mirrors `verify_fix.verify`'s build steps exactly so behavior matches the gate that produced the verdict.

- [ ] **Step 1: Implement `DockerOps`**

Add to `differential_oracle.py`:
```python
from build_instance import build_instance  # noqa: E402
from verify_fix import docker_exec, COMPILE_TIMEOUT, RUN_TIMEOUT  # noqa: E402

PROBE_RUN_TIMEOUT = 60


class DockerOps:
    """Real Docker-backed ops. Builds the agent-patched container the same way
    verify_fix does, and runs the prebuilt -fix image directly."""

    def fix_image_available(self, local_id: int) -> bool:
        image = f"n132/arvo:{local_id}-fix"
        if subprocess.run(["docker", "image", "inspect", image],
                          capture_output=True).returncode == 0:
            return True
        return subprocess.run(["docker", "pull", image],
                              capture_output=True).returncode == 0

    def build_agent(self, bug: dict, diff: str) -> str:
        instance = build_instance(bug)
        project = instance["project"]
        container = f"arvo-{instance['instance_id']}-oracle-agent"
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        subprocess.run(
            ["docker", "run", "-d", "--name", container, instance["image_name"],
             "sleep", str(COMPILE_TIMEOUT + 600)], check=True, capture_output=True)
        apply_res = docker_exec(container, f"git -C /src/{project} apply -",
                                input=diff, timeout=60)
        if apply_res.returncode != 0:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
            raise OracleError(f"agent patch did not apply: {apply_res.stderr[:500]}")
        docker_exec(container,
                    "sed -i 's#/depot_tools/ninja -C#/depot_tools/ninja -j3 -C#g' "
                    "/src/build.sh 2>/dev/null || true", timeout=30)
        build_res = docker_exec(container, f"cd /src/{project} && compile",
                                timeout=COMPILE_TIMEOUT)
        if build_res.returncode != 0:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
            raise OracleError("agent build failed under oracle")
        return container

    def start_fix(self, local_id: int) -> str:
        container = f"arvo-{local_id}-oracle-fix"
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        subprocess.run(
            ["docker", "run", "-d", "--name", container, f"n132/arvo:{local_id}-fix",
             "sleep", str(COMPILE_TIMEOUT + 600)], check=True, capture_output=True)
        return container

    def run_poc(self, container: str) -> tuple[int, str]:
        r = docker_exec(container, "arvo", timeout=RUN_TIMEOUT)
        return r.returncode, r.stdout + r.stderr

    def run_script(self, container: str, script_text: str) -> tuple[int, str]:
        r = docker_exec(container, "cd /src/mruby && bin/mruby /dev/stdin",
                        input=script_text, timeout=PROBE_RUN_TIMEOUT)
        return r.returncode, r.stdout + r.stderr

    def cleanup(self, container: str) -> None:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
```

- [ ] **Step 2: Verify the module imports cleanly and unit tests still pass**

Run: `cd arvo-eval && PYTHONPATH=. python3 -c "import differential_oracle" && PYTHONPATH=. python3 -m pytest tests/test_differential_oracle.py -q`
Expected: import OK, all PASS (DockerOps is not exercised by the unit tests).

- [ ] **Step 3: Commit**

```bash
git add arvo-eval/differential_oracle.py
git commit -m "feat(oracle): real DockerOps (agent build + -fix run + probes)"
```

---

### Task 7: Surface the `oracle=confirmed` trust marker in the playbook

**Files:**
- Modify: `arvo-eval/playbook_store.py:36-47`
- Test: `arvo-eval/tests/test_playbook_store.py`

`render_playbook` already copies arbitrary heuristic keys (so an `oracle` field stored by `add_heuristic` rides along automatically). This task only makes the renderer *show* a trust marker for confirmed lessons.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_playbook_store.py`:
```python
from playbook_store import new_state, add_heuristic, render_playbook


def test_render_marks_oracle_confirmed_heuristic():
    state = new_state()
    h = {"trigger": "bigint pool escape", "root_cause_lesson": "L",
         "how_to_apply": "A", "tags": ["suar"], "confidence": "high",
         "oracle": "confirmed"}
    add_heuristic(state, h, source_bug=439494108, after_bug=439494108)
    out = render_playbook(state, before_bug=999999999)
    assert "✓ fix-confirmed" in out


def test_render_omits_marker_when_not_confirmed():
    state = new_state()
    h = {"trigger": "x", "root_cause_lesson": "L", "how_to_apply": "A",
         "tags": [], "confidence": "medium", "oracle": "tests_only"}
    add_heuristic(state, h, source_bug=1, after_bug=1)
    out = render_playbook(state, before_bug=999999999)
    assert "fix-confirmed" not in out
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests/test_playbook_store.py -k oracle_confirmed -v`
Expected: FAIL — marker `✓ fix-confirmed` not present.

- [ ] **Step 3: Add the marker to the heading in `render_playbook`**

In `playbook_store.py`, replace the heading line (currently `lines.append(f"## {h['trigger']}  ({tags})")`) with:
```python
            marker = "  ✓ fix-confirmed" if h.get("oracle") == "confirmed" else ""
            lines.append(f"## {h['trigger']}  ({tags}){marker}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests/test_playbook_store.py -q`
Expected: all PASS (existing tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/playbook_store.py arvo-eval/tests/test_playbook_store.py
git commit -m "feat(oracle): show fix-confirmed trust marker in rendered playbook"
```

---

### Task 8: Wire veto-and-promote into `learn_loop`

**Files:**
- Modify: `arvo-eval/learn_loop.py` (imports, `run` signature, the `if solved:` block, ledger record)
- Test: `arvo-eval/tests/test_learn_loop_dryrun.py`

The grader is injected as a collaborator (like `verify`/`extract`/`contrastive`) so the dry-run test can stub it. The ledger record moves to *after* grading so it can carry the oracle fields.

- [ ] **Step 1: Read the existing dry-run test to match its stubbing style**

Run: `cd arvo-eval && sed -n '1,80p' tests/test_learn_loop_dryrun.py`
Note how `run(...)` is called and which collaborators are stubbed; the new test mirrors it and adds a `grade=` stub.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_learn_loop_dryrun.py` (adapt the stub fixtures to match those already in the file — reuse its existing `agent`/`verify`/`extract`/`contrastive`/`project_dir_for` stubs; only `grade` is new):
```python
def _grade_stub(label):
    def _g(bug, diff):
        n = 1 if label == "divergent" else 0
        avail = label not in ("no_fix_available",)
        return {"label": label, "fix_image_available": avail,
                "divergences": [{"probe": "poc", "kind": "stdout"}] * n}
    return _g


def test_confirmed_lesson_is_added_high_confidence(dryrun_kwargs):
    # dryrun_kwargs: a helper/fixture in this file that returns the collaborator
    # stubs + paths for a single solved bug. Add grade= to it.
    from learn_loop import run
    kw = dryrun_kwargs(solved=True)
    run(**{**kw, "grade": _grade_stub("oracle_confirmed")})
    from playbook_store import load_state
    state = load_state(kw["state_path"])
    assert len(state["heuristics"]) == 1
    assert state["heuristics"][0]["oracle"] == "confirmed"
    assert state["heuristics"][0]["confidence"] == "high"


def test_divergent_lesson_is_suppressed(dryrun_kwargs):
    from learn_loop import run
    kw = dryrun_kwargs(solved=True)
    run(**{**kw, "grade": _grade_stub("divergent")})
    from playbook_store import load_state
    state = load_state(kw["state_path"])
    assert state["heuristics"] == []           # vetoed: nothing learned
    from ledger import read_records
    rec = read_records(kw["ledger_path"])[-1]
    assert rec["oracle_label"] == "divergent"
    assert rec["n_divergences"] == 1


def test_no_fix_learns_as_tests_only(dryrun_kwargs):
    from learn_loop import run
    kw = dryrun_kwargs(solved=True)
    run(**{**kw, "grade": _grade_stub("no_fix_available")})
    from playbook_store import load_state
    state = load_state(kw["state_path"])
    assert len(state["heuristics"]) == 1
    assert state["heuristics"][0]["oracle"] == "tests_only"
    from ledger import read_records
    rec = read_records(kw["ledger_path"])[-1]
    assert rec["fix_image_available"] is False
```

> If `test_learn_loop_dryrun.py` does not already expose a `dryrun_kwargs` helper, add one that builds the existing stub collaborators (the same ones the current dry-run test constructs inline) plus tmp `state_path`/`ledger_path`, defaulting `grade` to `_grade_stub("no_fix_available")`. Keep the existing passing test working.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests/test_learn_loop_dryrun.py -k "confirmed or divergent or tests_only" -v`
Expected: FAIL — `run()` has no `grade` parameter / oracle fields absent.

- [ ] **Step 4: Add `_default_grade` and the `grade` param**

In `learn_loop.py`, add near the other `_default_*` helpers:
```python
def _default_grade(bug, diff):
    from differential_oracle import grade
    return grade(bug, diff)
```
Add `grade=_default_grade` to the `run(...)` signature (alongside `verify=`, `extract=`, `contrastive=`).

- [ ] **Step 5: Restructure the solved block to grade, then veto/promote, then append**

Replace the current block (today lines ~91–105: the unconditional `append_record` followed by `if solved:`) with:
```python
        oracle_fields = {}
        if solved:
            accepted = result["accepted"]
            pair = result["contrastive_pair"]
            if pair:                      # failed-then-succeeded: contrastive lesson
                rejected, _ = pair
                lesson = contrastive(bug, rejected["diff"], accepted["diff"], rejected["verdict"])
            else:                         # solved first try: plain success lesson
                lesson = extract(bug, accepted["diff"], accepted.get("trajectory_summary", ""),
                                 "verified_correct")

            verdict = grade(bug, accepted["diff"])
            oracle_fields = {"oracle_label": verdict["label"],
                             "fix_image_available": verdict["fix_image_available"],
                             "n_divergences": len(verdict["divergences"])}

            if verdict["label"] == "oracle_confirmed":
                lesson["oracle"] = "confirmed"
                lesson["confidence"] = "high"
                state = add_heuristic(state, lesson, source_bug=bug_id, after_bug=bug_id)
                save_state(state, state_path)
            elif verdict["label"] == "divergent":
                pass                      # VETO: patch diverges from canonical fix; learn nothing
            else:                         # no_fix_available | oracle_error
                lesson["oracle"] = "tests_only"
                state = add_heuristic(state, lesson, source_bug=bug_id, after_bug=bug_id)
                save_state(state, state_path)

        record = {"bug_id": bug_id, "pass": pass_name, "classification": final_verdict,
                  "n_attempts": len(result["attempts"]), "playbook_version": state["version"],
                  **oracle_fields}
        append_record(ledger_path, record)
```

> Note: this moves the existing `append_record` to *after* grading and merges `oracle_fields`. Make sure the old `append_record(ledger_path, record)` that ran before the `if solved:` block is removed (there must be exactly one `append_record` per bug).

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests/test_learn_loop_dryrun.py -v`
Expected: all PASS (new + existing).

- [ ] **Step 7: Commit**

```bash
git add arvo-eval/learn_loop.py arvo-eval/tests/test_learn_loop_dryrun.py
git commit -m "feat(oracle): veto-and-promote in learn_loop + oracle ledger fields"
```

---

### Task 9: Wall test — grader unreachable from the agent path

**Files:**
- Modify: `arvo-eval/tests/test_repair_loop.py`

Reinforce the deployment-faithful invariant: the repair loop and its feedback never reference `-fix` or the grader.

- [ ] **Step 1: Write the test**

Add to `tests/test_repair_loop.py`:
```python
import inspect
import repair_loop


def test_repair_loop_never_imports_or_mentions_the_oracle():
    src = inspect.getsource(repair_loop)
    assert "differential_oracle" not in src
    assert "-fix" not in src
    assert "grade(" not in src
```

- [ ] **Step 2: Run the test to verify it passes**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests/test_repair_loop.py -v`
Expected: all PASS (the wall already holds; this locks it in).

- [ ] **Step 3: Commit**

```bash
git add arvo-eval/tests/test_repair_loop.py
git commit -m "test(oracle): assert repair loop stays blind to the -fix oracle"
```

---

### Task 10: Phase 0 integration check + probe vetting (opt-in, Docker)

**Files:**
- Possibly add: `arvo-eval/differential/mruby_probes/04_*.rb` (bignum/set probes) if the container's `bin/mruby` includes those gems.

This task is run manually on a host with Docker and the ARVO images; it validates the real `DockerOps` and vets probe determinism. It is not part of the unit suite.

- [ ] **Step 1: Confirm a `-fix` image exists and probes are deterministic on it**

Run (replace `BUG` with a known mruby bug that has a `-fix` image, e.g. `439494108`):
```bash
cd arvo-eval && PYTHONPATH=. python3 -c "
from differential_oracle import DockerOps, default_probes
ops = DockerOps(); BUG=439494108
assert ops.fix_image_available(BUG), 'no -fix image'
c = ops.start_fix(BUG)
try:
    for p in default_probes():
        a = ops.run_script(c, p.read_text())
        b = ops.run_script(c, p.read_text())   # same build twice -> must match
        assert a == b, f'non-deterministic probe {p.name}: {a} vs {b}'
        print('OK', p.name, a[0])
    print('PoC', ops.run_poc(c)[0])
finally:
    ops.cleanup(c)
"
```
Expected: each probe prints `OK <name> 0`; no assertion fires. A probe that differs fix-vs-fix is non-deterministic — fix or drop it.

- [ ] **Step 2: Grade one bug end-to-end and read the label**

Run:
```bash
cd arvo-eval && PYTHONPATH=. python3 -c "
from build_instance import load_bug
from differential_oracle import grade
bug = load_bug(439494108)
diff = open('results/439494108/oss_crs_patch_0.diff').read()
print(grade(bug, diff))
"
```
Expected: a dict with `label` in {`oracle_confirmed`, `divergent`}; inspect `divergences` if divergent.

- [ ] **Step 3 (optional): add gem-exercising probes if available**

If `bin/mruby` in the container evaluates bignum/set code (the bug-cluster gems), add `04_bignum.rb` / `05_set.rb` exercising them, re-run Step 1 to confirm determinism, and commit. If those gems are absent from the default `bin/mruby`, skip — the PoC probe already covers the faulting path.

- [ ] **Step 4: Commit any vetted new probes**

```bash
git add arvo-eval/differential/mruby_probes
git commit -m "test(oracle): vetted gem-exercising probes (Phase 0)"
```

---

## Final verification

- [ ] **Run the full unit suite**

Run: `cd arvo-eval && PYTHONPATH=. python3 -m pytest tests -q`
Expected: all PASS, no regressions in existing tests.

- [ ] **Update the README learning-loop section**

Add a short paragraph under the Heuristic Learning Loop section of `arvo-eval/README.md` describing the differential `-fix` oracle: it grades each solved bug against `n132/arvo:{id}-fix` (PoC + committed probes), suppresses `divergent` lessons, promotes `oracle_confirmed` ones to high confidence, learns `tests_only` when no `-fix` exists, and records `oracle_label`/`fix_image_available`/`n_divergences` in the ledger. Note the §8 limitation (oracle inherits a buggy upstream fix, e.g. 444773339). Commit.
```bash
git add arvo-eval/README.md
git commit -m "docs(oracle): document the differential -fix oracle in the README"
```
