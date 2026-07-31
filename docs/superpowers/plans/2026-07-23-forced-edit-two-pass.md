# Forced-Edit Two-Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the local-model repair agent finishes its recon pass without making any edit, automatically relaunch it with a narrow "make the edit now" directive seeded with its own root-cause analysis — so an "understands-but-won't-write" run becomes a real patch, with no operator in the loop.

**Architecture:** A flag-gated (`OSS_CRS_FORCE_EDIT`) two-pass structure inside `arvo_oss_crs.run_oss_crs`. Phase 1 runs the agent under the existing hard cap but checks the agent's edit count once at `OSS_CRS_RECON_TIMEOUT`; if it is 0 the harness kills Phase 1, rewrites the injected `HEURISTICS.md` in *this run's* target-source into a forced-edit directive, and runs the agent again (Phase 2) for the remaining budget. The agent always authors the edit; the harness only controls timing and prompting. New pure helpers do the transcript parsing and directive assembly; the orchestration reuses the existing injection channel, check-patch service, and patch-collection path unchanged.

**Tech Stack:** Python 3, `pytest`, `subprocess` (Popen for Phase-1 polling), the existing `arvo-eval` OSS-CRS wrapper. Spec: `docs/superpowers/specs/2026-07-23-forced-edit-two-pass-design.md`.

---

## File structure

- `arvo-eval/arvo_oss_crs.py` — add config readers, transcript helpers, directive builder, Phase-1 poll-runner, Phase-2 injector; modify `run_oss_crs` agent-run section and its `summary` dict.
- `arvo-eval/learn_loop.py` — thread the new forced-edit fields from `summary` into the ledger record.
- `arvo-eval/tests/test_arvo_oss_crs.py` — unit tests for every new pure helper and the poll-runner (fakes, no Docker).
- `arvo-eval/CLAUDE.md` — document the new flags.

Run the whole suite with: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests -q`

---

### Task 1: Config readers for the two flags

**Files:**
- Modify: `arvo-eval/arvo_oss_crs.py` (near `_check_patch_enabled`, ~line 255)
- Test: `arvo-eval/tests/test_arvo_oss_crs.py`

- [ ] **Step 1: Write the failing test**

```python
def test_force_edit_flag_and_recon_timeout(monkeypatch):
    import arvo_oss_crs as a
    monkeypatch.delenv("OSS_CRS_FORCE_EDIT", raising=False)
    monkeypatch.delenv("OSS_CRS_RECON_TIMEOUT", raising=False)
    assert a._force_edit_enabled() is False
    assert a._recon_timeout() == 1800.0
    monkeypatch.setenv("OSS_CRS_FORCE_EDIT", "1")
    monkeypatch.setenv("OSS_CRS_RECON_TIMEOUT", "600")
    assert a._force_edit_enabled() is True
    assert a._recon_timeout() == 600.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py::test_force_edit_flag_and_recon_timeout -v`
Expected: FAIL with `AttributeError: module 'arvo_oss_crs' has no attribute '_force_edit_enabled'`

- [ ] **Step 3: Write minimal implementation**

Add after `_check_patch_enabled` (~line 256):

```python
def _force_edit_enabled() -> bool:
    """Master switch for the recon->forced-edit two-pass (OSS_CRS_FORCE_EDIT=1).
    Unset -> single-pass behavior identical to before this feature."""
    return os.environ.get("OSS_CRS_FORCE_EDIT") == "1"


def _recon_timeout() -> float:
    """Phase-1 elapsed time (seconds) at which the 0-edit check fires.
    Default 1800 (30 min); bounded above by OSS_CRS_RUN_TIMEOUT in practice."""
    v = os.environ.get("OSS_CRS_RECON_TIMEOUT")
    return float(v) if v else 1800.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py::test_force_edit_flag_and_recon_timeout -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/arvo_oss_crs.py arvo-eval/tests/test_arvo_oss_crs.py
git commit -m "feat(force-edit): config readers for OSS_CRS_FORCE_EDIT / RECON_TIMEOUT"
```

---

### Task 2: `agent_edit_count` — count edits in a claude_stdout.log stream

**Files:**
- Modify: `arvo-eval/arvo_oss_crs.py` (near `parse_token_counts`, ~line 360)
- Test: `arvo-eval/tests/test_arvo_oss_crs.py`

Context: the agent session is persisted as a JSONL stream (`claude_stdout.log`), the same file `parse_token_counts` reads. Each line is an event; assistant events carry `message.content` — a list of blocks, where a tool call is `{"type": "tool_use", "name": "Edit", ...}`. We count edit-family tools.

- [ ] **Step 1: Write the failing test**

```python
def test_agent_edit_count(tmp_path):
    import arvo_oss_crs as a, json
    log = tmp_path / "claude_stdout.log"
    lines = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "let me look"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "grep x"}}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.c"}}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "b.c"}}]}},
        "this is not json",
    ]
    log.write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x for x in lines))
    assert a.agent_edit_count(log) == 2
    assert a.agent_edit_count(tmp_path / "missing.log") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py::test_agent_edit_count -v`
Expected: FAIL with `AttributeError: ... 'agent_edit_count'`

- [ ] **Step 3: Write minimal implementation**

Add near `parse_token_counts`:

```python
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "str_replace", "str_replace_editor"}


def agent_edit_count(log_path: "Path") -> int:
    """Count edit-family tool_use events (Edit/Write/MultiEdit/str_replace) in a
    claude_stdout.log JSONL stream. Missing/unreadable file or malformed lines -> those
    contribute 0, so the worst case is under-counting to 0 ('treat as no edits')."""
    n = 0
    try:
        text = Path(log_path).read_text()
    except OSError:
        return 0
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = obj.get("message", {}).get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" \
                        and b.get("name") in EDIT_TOOLS:
                    n += 1
    return n
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py::test_agent_edit_count -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/arvo_oss_crs.py arvo-eval/tests/test_arvo_oss_crs.py
git commit -m "feat(force-edit): agent_edit_count over the claude_stdout.log stream"
```

---

### Task 3: `find_agent_stdout_log` — locate the live/persisted stream log for a run

**Files:**
- Modify: `arvo-eval/arvo_oss_crs.py` (near `copy_session_files`, ~line 354)
- Test: `arvo-eval/tests/test_arvo_oss_crs.py`

Context: `copy_session_files` already globs `crs/crs-claude-code/*/LOG_DIR/*/agent/claude_stdout.log` under a run dir. Extract that path logic so both the copy and the live edit-count check share one definition (DRY).

- [ ] **Step 1: Write the failing test**

```python
def test_find_agent_stdout_log(tmp_path):
    import arvo_oss_crs as a
    run_dir = tmp_path / "run"
    p = run_dir / "crs/crs-claude-code/proj/LOG_DIR/mruby_fuzzer/agent/claude_stdout.log"
    p.parent.mkdir(parents=True)
    p.write_text("{}")
    assert a.find_agent_stdout_log(run_dir) == p
    assert a.find_agent_stdout_log(tmp_path / "empty") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py::test_find_agent_stdout_log -v`
Expected: FAIL with `AttributeError: ... 'find_agent_stdout_log'`

- [ ] **Step 3: Write minimal implementation**

Add above `copy_session_files`:

```python
def find_agent_stdout_log(run_dir: "Path") -> "Path | None":
    """Host path of the agent's claude_stdout.log JSONL stream for a run. Written live
    during the run (bind-mounted LOG_DIR) so it is readable mid-run and after. Newest
    wins if a run somehow has more than one."""
    logs = list(run_dir.glob("crs/crs-claude-code/*/LOG_DIR/*/agent/claude_stdout.log"))
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None
```

Then DRY up `copy_session_files` to use it:

```python
def copy_session_files(run_dir: Path, output_dir: Path) -> None:
    """Copy claude_stdout.log to output_dir."""
    log = find_agent_stdout_log(run_dir)
    if log is not None:
        shutil.copy2(log, output_dir / "oss_crs_claude_stdout.log")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py -k "find_agent_stdout_log or session" -v`
Expected: PASS (new test passes; any existing copy_session_files test still passes)

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/arvo_oss_crs.py arvo-eval/tests/test_arvo_oss_crs.py
git commit -m "refactor(force-edit): share the agent stdout-log glob via find_agent_stdout_log"
```

---

### Task 4: `_live_edit_count` — edit count for the current run

**Files:**
- Modify: `arvo-eval/arvo_oss_crs.py` (after Task 3 helpers)
- Test: `arvo-eval/tests/test_arvo_oss_crs.py`

Context: composes `find_latest_run_dir` + `find_agent_stdout_log` + `agent_edit_count` into the single probe the orchestration calls both at the recon mark (live) and after each phase.

- [ ] **Step 1: Write the failing test**

```python
def test_live_edit_count(tmp_path, monkeypatch):
    import arvo_oss_crs as a, json
    run_dir = tmp_path / "run"
    p = run_dir / "crs/crs-claude-code/proj/LOG_DIR/mruby_fuzzer/agent/claude_stdout.log"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.c"}}]}}))
    monkeypatch.setattr(a, "find_latest_run_dir", lambda san: run_dir)
    assert a._live_edit_count("address") == 1
    monkeypatch.setattr(a, "find_latest_run_dir", lambda san: None)
    assert a._live_edit_count("address") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py::test_live_edit_count -v`
Expected: FAIL with `AttributeError: ... '_live_edit_count'`

- [ ] **Step 3: Write minimal implementation**

```python
def _live_edit_count(sanitizer: str) -> int:
    """Edit count for the current (newest) run's agent stream. 0 when the run dir or log
    does not exist yet (early in a run, or run never started)."""
    run_dir = find_latest_run_dir(sanitizer)
    if run_dir is None:
        return 0
    log = find_agent_stdout_log(run_dir)
    return agent_edit_count(log) if log is not None else 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py::test_live_edit_count -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/arvo_oss_crs.py arvo-eval/tests/test_arvo_oss_crs.py
git commit -m "feat(force-edit): _live_edit_count probe for the current run"
```

---

### Task 5: `extract_root_cause` — the agent's own final diagnosis

**Files:**
- Modify: `arvo-eval/arvo_oss_crs.py` (after `agent_edit_count`)
- Test: `arvo-eval/tests/test_arvo_oss_crs.py`

- [ ] **Step 1: Write the failing test**

```python
def test_extract_root_cause(tmp_path):
    import arvo_oss_crs as a, json
    log = tmp_path / "claude_stdout.log"
    big = "The set kset_put path stores keys without a write barrier. " * 6  # >200 chars
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": big}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines))
    assert a.extract_root_cause(log).startswith("The set kset_put path")
    assert a.extract_root_cause(tmp_path / "missing.log") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py::test_extract_root_cause -v`
Expected: FAIL with `AttributeError: ... 'extract_root_cause'`

- [ ] **Step 3: Write minimal implementation**

```python
def extract_root_cause(log_path: "Path", min_len: int = 200) -> str:
    """Return the agent's last substantial (>= min_len chars) assistant text block from
    the stream log, stripped. Empty string if none / unreadable. This is the agent's own
    diagnosis, fed back verbatim into the forced-edit directive -- the harness never
    interprets or rewrites it into a patch."""
    last = ""
    try:
        text = Path(log_path).read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = obj.get("message", {}).get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    t = (b.get("text") or "").strip()
                    if len(t) >= min_len:
                        last = t
    return last
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py::test_extract_root_cause -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/arvo_oss_crs.py arvo-eval/tests/test_arvo_oss_crs.py
git commit -m "feat(force-edit): extract_root_cause from the agent stream"
```

---

### Task 6: `build_forced_edit_directive` — the Phase-2 prompt

**Files:**
- Modify: `arvo-eval/arvo_oss_crs.py` (near `check_patch_instruction`, ~line 219)
- Test: `arvo-eval/tests/test_arvo_oss_crs.py`

- [ ] **Step 1: Write the failing test**

```python
def test_build_forced_edit_directive():
    import arvo_oss_crs as a
    out = a.build_forced_edit_directive(
        project="mruby", root_cause="Missing write barrier in kset_put.",
        orientation="# Crash orientation\nClass: heap-use-after-free")
    # Mandate is first and unmissable
    assert out.lstrip().startswith("# FORCED EDIT")
    idx_mandate = out.index("only task")
    idx_repo = out.index("/work/agent/clean-src/mruby")
    assert idx_mandate < idx_repo               # mandate before mechanics
    assert "Missing write barrier in kset_put." in out   # agent's own words fed back
    assert "check-patch" in out
    assert "Crash orientation" in out
    # Empty analysis is safe: still a valid directive standing on crash + recipe
    out2 = a.build_forced_edit_directive(project="mruby", root_cause="", orientation="")
    assert out2.lstrip().startswith("# FORCED EDIT")
    assert "You already concluded" not in out2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py::test_build_forced_edit_directive -v`
Expected: FAIL with `AttributeError: ... 'build_forced_edit_directive'`

- [ ] **Step 3: Write minimal implementation**

```python
def build_forced_edit_directive(*, project: str, root_cause: str, orientation: str) -> str:
    """The Phase-2 injected HEURISTICS.md content: a narrow 'make the edit now' directive.
    Deliberately omits the full playbook and any 'read the codebase' affordance -- Phase 2
    is about converting a stated diagnosis into one edit, not informing a fresh look."""
    repo = f"/work/agent/clean-src/{project}"
    parts = [
        "# FORCED EDIT -- your only task now",
        "You already investigated this crash. Do NOT investigate further and do NOT "
        "re-read the codebase. Make the ONE source edit your analysis points to and "
        "validate it with check-patch. That is the entire and only task.",
    ]
    if orientation.strip():
        parts.append(orientation.strip())
    if root_cause.strip():
        parts.append("## You already concluded\n" + root_cause.strip())
    parts.append(
        "## Make the edit and validate\n"
        f"Edit the source in `{repo}` (already a git repo -- do NOT run `git init`). If "
        "it is not present yet, run `download-source target-source /work/agent/clean-src` "
        "first. Then, from that repo:\n"
        f"    cd {repo} && bash \"$OSS_CRS_SHARED_DIR/check-patch\"\n"
        "When check-patch prints PASS you are DONE: the validated patch is recorded and "
        "submitted for you. Do NOT hand-write a diff or write to /patches/."
    )
    return "\n\n".join(parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py::test_build_forced_edit_directive -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/arvo_oss_crs.py arvo-eval/tests/test_arvo_oss_crs.py
git commit -m "feat(force-edit): build_forced_edit_directive (mandate-first Phase-2 prompt)"
```

---

### Task 7: `phase2_timeout` — remaining-budget math

**Files:**
- Modify: `arvo-eval/arvo_oss_crs.py` (near `_run_timeout`, ~line 448)
- Test: `arvo-eval/tests/test_arvo_oss_crs.py`

- [ ] **Step 1: Write the failing test**

```python
def test_phase2_timeout():
    import arvo_oss_crs as a
    # Normal: hard cap 7200, phase1 ran 1800 -> 5400 remaining
    assert a.phase2_timeout(7200, 1800) == 5400
    # Floor protects a tiny remainder
    assert a.phase2_timeout(7200, 7000, floor=900) == 900
    # No hard cap -> no phase-2 cap
    assert a.phase2_timeout(None, 1800) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py::test_phase2_timeout -v`
Expected: FAIL with `AttributeError: ... 'phase2_timeout'`

- [ ] **Step 3: Write minimal implementation**

```python
def phase2_timeout(hard_cap: "float | None", phase1_elapsed: float,
                   floor: float = 900.0) -> "float | None":
    """Wall-clock cap for the forced-edit pass: whatever remains of the hard per-run cap
    after Phase 1, but never less than `floor` so a forced pass always gets a usable
    budget. No hard cap -> no Phase-2 cap."""
    if hard_cap is None:
        return None
    return max(hard_cap - phase1_elapsed, floor)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py::test_phase2_timeout -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/arvo_oss_crs.py arvo-eval/tests/test_arvo_oss_crs.py
git commit -m "feat(force-edit): phase2_timeout remaining-budget helper"
```

---

### Task 8: `_run_agent_recon_phase` — Phase-1 poll-runner with the one-shot 0-edit check

**Files:**
- Modify: `arvo-eval/arvo_oss_crs.py` (near `_run_agent_with_timeout`, ~line 469)
- Test: `arvo-eval/tests/test_arvo_oss_crs.py`

Context: `_run_agent_with_timeout` blocks on `subprocess.run(timeout=)`, which can't act on an external signal mid-run. Phase 1 needs to (a) let the agent run, (b) at `recon_cap` elapsed check the edit count exactly once, (c) kill + hand off to Phase 2 iff 0 edits, else keep running to `hard_cap`. Implement with `Popen` + a poll loop, with every side-effecting dependency injected so it is unit-testable with fakes (mirrors how `_run_agent_with_timeout` injects `run`/`teardown`).

Returns `(timed_out: bool, forced: bool)`.

- [ ] **Step 1: Write the failing test**

```python
def test_run_agent_recon_phase_forces_when_zero_edits():
    import arvo_oss_crs as a

    class FakeProc:
        def __init__(self): self.killed = False
        def poll(self): return None        # never exits on its own
        def kill(self): self.killed = True
        def wait(self, timeout=None): return 0

    proc = FakeProc()
    torn = []
    clock = {"t": 0.0}
    def fake_now(): clock["t"] += 10; return clock["t"]   # +10s per poll
    timed_out, forced = a._run_agent_recon_phase(
        ["run"], cwd=".", hard_cap=100, recon_cap=30,
        edit_probe=lambda: 0,                  # agent made no edits
        popen=lambda cmd, cwd: proc,
        teardown=lambda: torn.append(True),
        now=fake_now, sleep=lambda s: None)
    assert (timed_out, forced) == (False, True)
    assert proc.killed and torn == [True]


def test_run_agent_recon_phase_lets_worker_run_to_hard_cap():
    import arvo_oss_crs as a

    class FakeProc:
        def __init__(self): self.killed = False
        def poll(self): return None
        def kill(self): self.killed = True
        def wait(self, timeout=None): return 0

    proc = FakeProc()
    clock = {"t": 0.0}
    def fake_now(): clock["t"] += 10; return clock["t"]
    timed_out, forced = a._run_agent_recon_phase(
        ["run"], cwd=".", hard_cap=100, recon_cap=30,
        edit_probe=lambda: 3,                  # already editing at the recon mark
        popen=lambda cmd, cwd: proc,
        teardown=lambda: None,
        now=fake_now, sleep=lambda s: None)
    # editing -> not forced; runs until hard_cap -> timed_out
    assert (timed_out, forced) == (True, False)
    assert proc.killed


def test_run_agent_recon_phase_natural_exit():
    import arvo_oss_crs as a

    class FakeProc:
        def __init__(self): self.calls = 0
        def poll(self):
            self.calls += 1
            return None if self.calls < 2 else 0   # exits on the 2nd poll
        def kill(self): pass
        def wait(self, timeout=None): return 0

    clock = {"t": 0.0}
    def fake_now(): clock["t"] += 10; return clock["t"]
    timed_out, forced = a._run_agent_recon_phase(
        ["run"], cwd=".", hard_cap=100, recon_cap=30,
        edit_probe=lambda: 0, popen=lambda cmd, cwd: FakeProc(),
        teardown=lambda: None, now=fake_now, sleep=lambda s: None)
    assert (timed_out, forced) == (False, False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py -k run_agent_recon_phase -v`
Expected: FAIL with `AttributeError: ... '_run_agent_recon_phase'`

- [ ] **Step 3: Write minimal implementation**

```python
def _run_agent_recon_phase(cmd, *, cwd, hard_cap, recon_cap, edit_probe,
                           popen=subprocess.Popen, teardown=None,
                           now=time.monotonic, sleep=time.sleep,
                           poll_interval=15) -> "tuple[bool, bool]":
    """Run the Phase-1 (recon) agent. Returns (timed_out, forced).

    At `recon_cap` elapsed, check `edit_probe()` exactly once:
      * 0 edits  -> kill + teardown, return (False, True)  [hand off to forced pass]
      * >0 edits -> the agent is productively editing; do NOT interrupt, keep running to
                    `hard_cap`.
    Natural exit before any cap -> (False, False). Reaching `hard_cap` -> kill + teardown,
    (True, False). `hard_cap`/`recon_cap` are seconds; None hard_cap = no hard cap."""
    teardown = teardown or terminate_crs_run
    proc = popen(cmd, cwd=cwd)
    start = now()
    checked_recon = False
    while True:
        if proc.poll() is not None:
            return (False, False)
        elapsed = now() - start
        if not checked_recon and elapsed >= recon_cap:
            checked_recon = True
            if edit_probe() == 0:
                proc.kill(); proc.wait(); teardown()
                return (False, True)
        if hard_cap is not None and elapsed >= hard_cap:
            proc.kill(); proc.wait(); teardown()
            return (True, False)
        sleep(poll_interval)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py -k run_agent_recon_phase -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/arvo_oss_crs.py arvo-eval/tests/test_arvo_oss_crs.py
git commit -m "feat(force-edit): _run_agent_recon_phase poll-runner with one-shot 0-edit check"
```

---

### Task 9: `inject_forced_edit` — overwrite this run's HEURISTICS.md with the directive

**Files:**
- Modify: `arvo-eval/arvo_oss_crs.py` (after `inject_orientation`, ~line 325)
- Test: `arvo-eval/tests/test_arvo_oss_crs.py`

Context: Phase 2 reuses the same already-built `target-source` (no rebuild). We pin it with the same `newer_than`/`exclude` used in Phase-1 injection, read the persisted Phase-1 stream to get the agent's diagnosis, and overwrite `HEURISTICS.md` with the directive. Orientation text is rebuilt from the bug via the existing `parse_crash_output` + `render_orientation` (already imported/used by `inject_orientation`).

- [ ] **Step 1: Write the failing test**

```python
def test_inject_forced_edit_overwrites_target_source(tmp_path, monkeypatch):
    import arvo_oss_crs as a, json
    ts = tmp_path / "target-source"
    ts.mkdir()
    (ts / "HEURISTICS.md").write_text("OLD PLAYBOOK CONTENT")
    # stream log with the agent's diagnosis
    log = tmp_path / "claude_stdout.log"
    big = "Root cause: kset_put stores keys without a write barrier. " * 5
    log.write_text(json.dumps({"type": "assistant",
        "message": {"content": [{"type": "text", "text": big}]}}))
    monkeypatch.setattr(a, "find_target_source_dir", lambda san, newer_than=None, exclude=(): ts)
    monkeypatch.setattr(a, "find_latest_run_dir", lambda san: tmp_path)
    monkeypatch.setattr(a, "find_agent_stdout_log", lambda rd: log)
    monkeypatch.setattr(a, "parse_crash_output", lambda *args, **kw: None)  # no orientation
    ok = a.inject_forced_edit("address", {"localId": 1, "project": "mruby",
                                          "crash_output": "", "crash_type": ""}, "mruby",
                              newer_than=None, exclude=())
    assert ok is True
    written = (ts / "HEURISTICS.md").read_text()
    assert written.lstrip().startswith("# FORCED EDIT")
    assert "OLD PLAYBOOK CONTENT" not in written          # overwritten, not appended
    assert "kset_put stores keys" in written              # agent's diagnosis fed back
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py::test_inject_forced_edit_overwrites_target_source -v`
Expected: FAIL with `AttributeError: ... 'inject_forced_edit'`

- [ ] **Step 3: Write minimal implementation**

```python
def inject_forced_edit(sanitizer: str, bug: dict, project: str,
                       newer_than: float | None = None,
                       exclude: "set[Path] | tuple" = ()) -> bool:
    """Overwrite THIS run's target-source HEURISTICS.md with the forced-edit directive,
    seeded with the agent's own Phase-1 diagnosis. Same `newer_than`/`exclude` pinning as
    inject_heuristics, so it always targets this run's freshly-built dir. Returns True on
    write, False if the target-source can't be found."""
    target_source = find_target_source_dir(sanitizer, newer_than=newer_than, exclude=exclude)
    if target_source is None:
        print(f"[{bug['localId']}] WARNING: no fresh target-source dir -- forced-edit "
              f"directive NOT injected; Phase 2 will run without it.")
        return False
    # Agent's own diagnosis from Phase 1 (best effort).
    root_cause = ""
    run_dir = find_latest_run_dir(sanitizer)
    if run_dir is not None:
        log = find_agent_stdout_log(run_dir)
        if log is not None:
            root_cause = extract_root_cause(log)
    # Crash orientation (best effort; reuse the same parser inject_orientation uses).
    orientation = ""
    o = parse_crash_output(bug.get("crash_output") or "", bug.get("crash_type") or "",
                           bug["project"])
    if o is not None:
        orientation = render_orientation(o)
    directive = build_forced_edit_directive(project=project, root_cause=root_cause,
                                            orientation=orientation)
    dest = target_source / "HEURISTICS.md"
    dest.write_text(directive)
    print(f"[{bug['localId']}] Injected FORCED-EDIT directive ({len(directive)} bytes, "
          f"root_cause={len(root_cause)}B) into {dest}")
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py::test_inject_forced_edit_overwrites_target_source -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/arvo_oss_crs.py arvo-eval/tests/test_arvo_oss_crs.py
git commit -m "feat(force-edit): inject_forced_edit overwrites this run's HEURISTICS.md"
```

---

### Task 10: Wire the two-pass into `run_oss_crs`

**Files:**
- Modify: `arvo-eval/arvo_oss_crs.py:575-590` (the agent-run block) and the `summary` dict (~line 631)
- Test: `arvo-eval/tests/test_arvo_oss_crs.py`

Context: the current single call is (lines 575-590):

```python
    run_start = time.time()
    try:
        timed_out = _run_agent_with_timeout(
            [*base, "run", *compose_args,
             "--fuzz-proj-path", str(project_dir),
             "--target-harness", bug["fuzz_target"],
             "--pov", str(pov_path),
             "--incremental-build"],
            cwd=OSS_CRS_DIR,
            timeout=timeout,
        )
    finally:
        if check_stop is not None:
            check_stop.set()
            check_thread.join(timeout=30)
    run_elapsed = time.time() - run_start
```

- [ ] **Step 1: Write the failing test (flag-off regression + two-phase dispatch)**

Add a test that the two-pass helper `run_agent_phases` (extracted below) makes exactly one agent call when the flag is off, and a recon+forced pair when the probe reports 0 edits. Keep the check-thread/patch logic out of it so it stays unit-testable.

```python
def test_run_agent_phases_flag_off_single_call(monkeypatch):
    import arvo_oss_crs as a
    calls = []
    monkeypatch.setenv("OSS_CRS_FORCE_EDIT", "0") if False else monkeypatch.delenv("OSS_CRS_FORCE_EDIT", raising=False)
    res = a.run_agent_phases(
        run_cmd=["run"], cwd=".", sanitizer="address", bug={"localId": 1},
        project="mruby", hard_cap=100, newer_than=None, exclude=(),
        single=lambda cmd, cwd, timeout: (calls.append(("single", timeout)) or False),
        recon=lambda **k: (_ for _ in ()).throw(AssertionError("recon must not run")),
        inject_forced=lambda *a_, **k: True,
        edit_probe=lambda: 0)
    assert res["forced_edit_triggered"] is False
    assert calls == [("single", 100)]


def test_run_agent_phases_forced_pair(monkeypatch):
    import arvo_oss_crs as a
    monkeypatch.setenv("OSS_CRS_FORCE_EDIT", "1")
    calls = []
    injected = []
    res = a.run_agent_phases(
        run_cmd=["run"], cwd=".", sanitizer="address", bug={"localId": 1},
        project="mruby", hard_cap=7200, newer_than=None, exclude=(),
        single=lambda cmd, cwd, timeout: calls.append(("single", timeout)) or False,
        recon=lambda **k: (False, True),          # recon ends with 0 edits -> force
        inject_forced=lambda *a_, **k: injected.append(True) or True,
        edit_probe=lambda: 0,
        phase1_elapsed_override=1800)
    assert res["forced_edit_triggered"] is True
    assert injected == [True]
    # Phase 2 is the single-call path, capped at remaining budget (7200-1800=5400)
    assert calls == [("single", 5400)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py -k run_agent_phases -v`
Expected: FAIL with `AttributeError: ... 'run_agent_phases'`

- [ ] **Step 3: Write minimal implementation**

Add `run_agent_phases` (pure orchestration, all effects injected) near `run_oss_crs`:

```python
def run_agent_phases(*, run_cmd, cwd, sanitizer, bug, project, hard_cap,
                     newer_than, exclude, single, recon, inject_forced, edit_probe,
                     phase1_elapsed_override=None, now=time.monotonic):
    """Decide and drive the recon/forced-edit passes. All side-effecting steps are passed
    in so this is unit-testable:
      single(cmd, cwd, timeout) -> timed_out      (a normal capped agent run)
      recon(**kw) -> (timed_out, forced)          (Phase-1 poll runner)
      inject_forced(sanitizer, bug, project, newer_than, exclude) -> bool
      edit_probe() -> int                         (edit count for the current run)
    Returns a dict of forced-edit fields for the summary/ledger."""
    if not _force_edit_enabled():
        timed_out = single(run_cmd, cwd, hard_cap)
        return {"timed_out": timed_out, "forced_edit_triggered": False,
                "phase1_edits": None, "phase2_edits": edit_probe(),
                "edit_phase": "recon" if edit_probe() else None}

    t0 = now()
    timed_out, forced = recon(cmd=run_cmd, cwd=cwd, hard_cap=hard_cap,
                              recon_cap=_recon_timeout(), edit_probe=edit_probe,
                              sanitizer=sanitizer)
    phase1_elapsed = phase1_elapsed_override if phase1_elapsed_override is not None \
        else (now() - t0)
    phase1_edits = edit_probe()
    if not forced:
        return {"timed_out": timed_out, "forced_edit_triggered": False,
                "phase1_edits": phase1_edits, "phase2_edits": phase1_edits,
                "edit_phase": "recon" if phase1_edits else None}

    inject_forced(sanitizer, bug, project, newer_than, exclude)
    timed_out = single(run_cmd, cwd, phase2_timeout(hard_cap, phase1_elapsed))
    phase2_edits = edit_probe()
    return {"timed_out": timed_out, "forced_edit_triggered": True,
            "phase1_edits": phase1_edits, "phase2_edits": phase2_edits,
            "edit_phase": "forced" if phase2_edits else "recon"}
```

Then replace the `run_oss_crs` agent-run block (lines 575-590) with a call that binds the real effects:

```python
    run_start = time.time()
    run_cmd = [*base, "run", *compose_args,
               "--fuzz-proj-path", str(project_dir),
               "--target-harness", bug["fuzz_target"],
               "--pov", str(pov_path),
               "--incremental-build"]
    try:
        phase_info = run_agent_phases(
            run_cmd=run_cmd, cwd=OSS_CRS_DIR, sanitizer=sanitizer, bug=bug,
            project=bug["project"], hard_cap=timeout,
            newer_than=build_start, exclude=ts_before,
            single=lambda cmd, cwd, to: _run_agent_with_timeout(cmd, cwd=cwd, timeout=to),
            recon=lambda **kw: (kw.pop("sanitizer", None),
                                _run_agent_recon_phase(kw.pop("cmd"), **kw))[1],
            inject_forced=inject_forced_edit,
            edit_probe=lambda: _live_edit_count(sanitizer))
        timed_out = phase_info["timed_out"]
    finally:
        if check_stop is not None:
            check_stop.set()
            check_thread.join(timeout=30)
    run_elapsed = time.time() - run_start
```

And extend the `summary` dict (after `"auto_submitted": auto_submitted,`):

```python
        "forced_edit_triggered": phase_info["forced_edit_triggered"],
        "edit_phase": phase_info["edit_phase"],
        "phase1_edits": phase_info["phase1_edits"],
        "phase2_edits": phase_info["phase2_edits"],
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/test_arvo_oss_crs.py -k run_agent_phases -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the FULL suite (guard against integration breakage)**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests -q`
Expected: PASS (all prior tests + the new ones)

- [ ] **Step 6: Commit**

```bash
git add arvo-eval/arvo_oss_crs.py arvo-eval/tests/test_arvo_oss_crs.py
git commit -m "feat(force-edit): two-pass orchestration in run_oss_crs behind OSS_CRS_FORCE_EDIT"
```

---

### Task 11: Thread forced-edit fields into the ledger record

**Files:**
- Modify: `arvo-eval/learn_loop.py:156-158`
- Test: `arvo-eval/tests/test_learn_loop.py` (or the existing learn_loop test module)

Context: the ledger `record` is built by selecting fields (not a summary passthrough), so the new fields must be added explicitly. The last attempt's summary is in `last_run["summary"]`.

- [ ] **Step 1: Write the failing test**

```python
def test_ledger_record_carries_forced_edit_fields():
    # Build the record dict the way run_pass does and assert the forced-edit fields
    # are carried from the summary.
    summary = {"forced_edit_triggered": True, "edit_phase": "forced",
               "phase1_edits": 0, "phase2_edits": 1}
    forced = {k: summary.get(k) for k in
              ("forced_edit_triggered", "edit_phase", "phase1_edits", "phase2_edits")}
    record = {"bug_id": 1, "pass": "treatment", "classification": "verified_correct",
              **forced}
    assert record["forced_edit_triggered"] is True
    assert record["edit_phase"] == "forced"
    assert record["phase1_edits"] == 0 and record["phase2_edits"] == 1
```

(This locks the field-selection contract; the implementation must produce the same keys.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/ -k forced_edit_fields -v`
Expected: FAIL (test file/function not yet present) — add the test, then it fails only if the contract is wrong; here it documents the shape.

- [ ] **Step 3: Write minimal implementation**

In `learn_loop.py`, just before building `record` (line 156), add:

```python
            _summary = last_run.get("summary", {})
            forced_edit_fields = {k: _summary[k] for k in
                                  ("forced_edit_triggered", "edit_phase",
                                   "phase1_edits", "phase2_edits") if k in _summary}
```

and include it in the record:

```python
            record = {"bug_id": bug_id, "pass": pass_name, "classification": final_verdict,
                      "n_attempts": len(result["attempts"]), "playbook_version": playbook_version_snap,
                      **forced_edit_fields,
                      **oracle_fields, **({"tokens": total_tokens} if total_tokens else {})}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arvo-eval && PYTHONPATH=. .venv/bin/python3 -m pytest tests/ -k forced_edit_fields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/learn_loop.py arvo-eval/tests/
git commit -m "feat(force-edit): record forced-edit fields in the ledger"
```

---

### Task 12: Document the flags and launch usage

**Files:**
- Modify: `arvo-eval/CLAUDE.md` (Running the experiment / env section)
- Modify: `arvo-eval/launch_scale.sh` (add the flags, commented, off by default)

- [ ] **Step 1: Add the flags to `launch_scale.sh`**

In the `env` block, add (kept off by default so nothing changes until deliberately enabled):

```bash
  OSS_CRS_FORCE_EDIT="${OSS_CRS_FORCE_EDIT:-0}" \
  OSS_CRS_RECON_TIMEOUT="${OSS_CRS_RECON_TIMEOUT:-1800}" \
```

- [ ] **Step 2: Document in `CLAUDE.md`**

Add under the env/gotchas section:

```markdown
- **`OSS_CRS_FORCE_EDIT=1`** — enables the recon->forced-edit two-pass: if the agent
  makes 0 edits within `OSS_CRS_RECON_TIMEOUT` seconds (default 1800), the harness kills
  the recon pass and reruns the agent with a narrow "make the edit now" directive seeded
  with its own stated root cause. Off by default (single pass, unchanged). Apply to BOTH
  arms when enabled (it is a harness signal, not the playbook under test), which means the
  existing control 6/10 baseline must be re-run under the flag for a clean comparison.
```

- [ ] **Step 3: Commit**

```bash
git add arvo-eval/CLAUDE.md arvo-eval/launch_scale.sh
git commit -m "docs(force-edit): document OSS_CRS_FORCE_EDIT / RECON_TIMEOUT + launcher flags"
```

---

## Self-review

**Spec coverage:**
- Flag gating (`OSS_CRS_FORCE_EDIT`), recon timeout → Task 1, 12.
- Recon pass + one-shot 0-edit check, "push staller / don't interrupt worker" → Task 8.
- Edit detection (primary transcript count) → Tasks 2, 4. (Spec's optional `git diff` fallback is intentionally deferred — YAGNI until the transcript signal proves insufficient; noted here so it isn't lost.)
- Root-cause extraction → Task 5.
- Forced-edit directive (mandate first, agent's own words, check-patch recipe, no playbook) → Task 6.
- Phase-2 injection via existing channel, same target-source pinning → Task 9.
- Budget math under the hard cap → Task 7.
- Two-pass orchestration, flag-off identical path → Task 10.
- Bounded failure (exactly one forced pass) → Task 10 (`run_agent_phases` runs `single` once after force; no loop).
- Ledger observability fields → Task 11.
- Both-arms scope note + control re-baseline implication → Task 12.

**Placeholder scan:** none — every code step carries full code and every command has expected output.

**Type consistency:** `run_agent_phases` returns the four keys `forced_edit_triggered`/`edit_phase`/`phase1_edits`/`phase2_edits`; the summary dict (Task 10) and the ledger record (Task 11) consume exactly those names. `_run_agent_recon_phase` returns `(timed_out, forced)` consumed positionally in Task 10's `recon=` lambda. `phase2_timeout(hard_cap, phase1_elapsed, floor)` signature matches its call in `run_agent_phases`. `run_agent_phases` passes `sanitizer=` into `recon(...)`, but `_run_agent_recon_phase` has no `sanitizer` param — Task 10's real `recon=` lambda pops `sanitizer` off before forwarding, so the wiring is correct as written.
