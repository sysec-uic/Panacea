# mruby Heuristic Learning Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-improving agent memory for mruby ARVO bugs: extract a heuristic from each verified-correct repair run, accumulate them into a chronologically-holdout-safe playbook, and inject the playbook into the agent's context on later bugs.

**Architecture:** A thin orchestrator (`learn_loop.py`) wraps the existing `arvo_oss_crs.py` pipeline. It walks mruby's 30 bugs in `localId` order; per bug it injects the current playbook, runs the agent, verifies (sanitizer-aware no-crash + `make test`), and — only for verified-correct runs — extracts a heuristic and adds it to a structured store. Injection renders only heuristics added strictly before the current bug, so a bug is never tested against its own lesson.

**Tech Stack:** Python 3.11+, pytest, sqlite3 (stdlib), Docker (via subprocess, existing pattern), Anthropic SDK (`claude-opus-4-8`) for extraction.

**Before starting:** This work lands on `main`. Create a feature branch first: `git checkout -b mruby-heuristic-loop`.

**Spec:** `docs/superpowers/specs/2026-06-29-mruby-heuristic-learning-loop-design.md`

---

## File Structure

All new code lives under `arvo-eval/` to sit beside the pipeline it wraps.

| File | Responsibility |
|------|----------------|
| `arvo-eval/playbook_store.py` | Structured heuristic store: load/save state JSON, add heuristic with provenance, render holdout-filtered markdown. Pure logic, no I/O beyond its files. |
| `arvo-eval/ledger.py` | Append/read per-run experiment records to `results/learn/ledger.jsonl`. |
| `arvo-eval/injector.py` | Place playbook text where the agent will read it (mechanism from Task 1). |
| `arvo-eval/llm.py` | Thin Anthropic SDK wrapper: `call_llm(prompt, system) -> str`. |
| `arvo-eval/extract_heuristic.py` | LLM call producing one structured heuristic from a verified-correct run. |
| `arvo-eval/curator.py` | Compress rendered playbook text when it exceeds a size cap (ephemeral, at injection time). |
| `arvo-eval/learn_loop.py` | Orchestrator: order bugs, inject, run, verify, extract, log. |
| `arvo-eval/verify_fix.py` | **Modify**: extract pure `classify_run()`, make crash detection sanitizer-aware, add `make test` gate. |
| `arvo-eval/playbook/mruby_playbook.md` | Human-readable full render (derived artifact). |
| `arvo-eval/playbook/playbook_state.json` | Canonical structured store. |
| `arvo-eval/tests/conftest.py` | Adds `arvo-eval/` to `sys.path` so tests import bare module names like the existing scripts. |
| `arvo-eval/tests/test_*.py` | Unit + integration tests. |
| `arvo-eval/PHASE0_NOTES.md` | Records the two spike outcomes (injection hook, `make test` command). |

**Test command convention (run from repo root, avoids `cd`):**
```bash
PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/<file>.py -v
```

---

## Phase 0 — De-risking Spikes

### Task 1: Spike — confirm the injection point

**Files:**
- Create: `arvo-eval/PHASE0_NOTES.md`

- [ ] **Step 1: Run one mruby bug through the existing pipeline with a marker file present**

Drop a uniquely-identifiable marker into the wrapped project working tree and see whether the agent reads it. Pick a cheap bug (439291659).

```bash
# Build + run a single bug, then inspect the agent's stdout log for whether
# a planted file in the project dir is visible to the agent.
echo "MARKER-PLAYBOOK-7f3a: if you can read this, mention 7f3a in your reasoning." \
  > ~/.arvo-oss-crs/439291659/project/HEURISTICS.md  # dir may not exist yet; see step 2
```

- [ ] **Step 2: Generate the project dir first, then plant the marker, then run**

Run the pipeline once to create `~/.arvo-oss-crs/439291659/project/`, then re-plant and re-run with `--skip-build`:

```bash
ARVO_DB_PATH=arvo_new.db OSS_CRS_BUG_ID=439291659 python3 arvo-eval/arvo_oss_crs.py
echo "MARKER-PLAYBOOK-7f3a ..." > ~/.arvo-oss-crs/439291659/project/HEURISTICS.md
ARVO_DB_PATH=arvo_new.db OSS_CRS_BUG_ID=439291659 python3 arvo-eval/arvo_oss_crs.py --skip-build
grep -i 7f3a arvo-eval/results/439291659/oss_crs_claude_stdout.log
```

- [ ] **Step 3: Try the in-container source-tree hook if the project-dir file is not read**

The agent edits `/src/mruby` inside the container. Test planting a `CLAUDE.md` there via the OSS-CRS workdir, and/or check whether OSS-CRS exposes a task-prompt hook. Record which of these works:
1. file in `~/.arvo-oss-crs/<id>/project/`
2. `CLAUDE.md` / `HEURISTICS.md` inside `/src/mruby`
3. prompt-level (requires editing how OSS-CRS builds the task) — fallback only

- [ ] **Step 4: Write the decision to `PHASE0_NOTES.md`**

Record: the working mechanism, the exact path/format, and the constant the injector will use. Example content:

```markdown
# Phase 0 Notes

## Injection mechanism (Task 1)
- WORKS: writing `HEURISTICS.md` into `<project_dir>` is surfaced to the agent. (or whichever won)
- Constant for injector.py: INJECT_FILENAME = "HEURISTICS.md", INJECT_TARGET = "project_dir"
- Agent confirmed reading it: stdout log line referencing marker 7f3a.
```

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/PHASE0_NOTES.md
git commit -m "spike: confirm playbook injection point for crs-claude-code"
```

### Task 2: Spike — confirm the `make test` command for mruby ARVO images

**Files:**
- Modify: `arvo-eval/PHASE0_NOTES.md`

- [ ] **Step 1: Find the test target inside a mruby ARVO container**

```bash
docker run --rm n132/arvo:439291659-vul bash -lc '
  cd /src/mruby 2>/dev/null && ls && \
  (rake -T 2>/dev/null | head) ; \
  (ls minirake build_config* Rakefile 2>/dev/null)'
```

- [ ] **Step 2: Run the suite to see the invocation and exit behavior**

mruby's standard test entry is `rake test` (or `./minirake test`). Confirm which exists and that it exits non-zero on failure:

```bash
docker run --rm n132/arvo:439291659-vul bash -lc 'cd /src/mruby && rake test; echo EXIT=$?'
```

- [ ] **Step 3: Record the confirmed command in `PHASE0_NOTES.md`**

```markdown
## make test command (Task 2)
- Confirmed: `cd /src/mruby && rake test`   (or `./minirake test`)
- Exit code on success: 0 ; on failure: non-zero (verified by forcing a failing test)
- MRUBY_TEST_CMD constant for verify_fix.py set accordingly.
```

- [ ] **Step 4: Commit**

```bash
git add arvo-eval/PHASE0_NOTES.md
git commit -m "spike: confirm mruby make-test invocation in ARVO container"
```

---

## Phase 1 — Verifier Upgrade

### Task 3: Extract a pure, sanitizer-aware `classify_run()` function

**Files:**
- Modify: `arvo-eval/verify_fix.py`
- Create: `arvo-eval/tests/conftest.py`
- Test: `arvo-eval/tests/test_verify_classify.py`

- [ ] **Step 1: Create the test conftest so tests can import bare module names**

`arvo-eval/tests/conftest.py`:
```python
import sys
from pathlib import Path

# Make arvo-eval/ importable so tests use the same bare imports as the scripts.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
```

- [ ] **Step 2: Write failing tests for `classify_run`**

`arvo-eval/tests/test_verify_classify.py`:
```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_verify_classify.py -v`
Expected: FAIL with `ImportError: cannot import name 'classify_run'`.

- [ ] **Step 4: Implement `classify_run` and the sanitizer signature table in `verify_fix.py`**

Add near the top of `arvo-eval/verify_fix.py` (after the existing constants):
```python
# Crash signatures keyed by sanitizer. A rebuilt target "still crashes" if any
# of its sanitizer's signatures appears in the rerun output.
SANITIZER_SIGNATURES = {
    "asan": ("ERROR: AddressSanitizer", "SUMMARY: AddressSanitizer"),
    "msan": ("WARNING: MemorySanitizer", "ERROR: MemorySanitizer", "SUMMARY: MemorySanitizer"),
    "ubsan": ("runtime error:", "SUMMARY: UndefinedBehaviorSanitizer"),
}


def crashed(sanitizer: str, run_output: str) -> bool:
    sigs = SANITIZER_SIGNATURES.get(sanitizer.lower(), ("ERROR: AddressSanitizer",))
    return any(sig in run_output for sig in sigs)


def classify_run(
    *,
    sanitizer: str,
    diff: str,
    apply_ok: bool = True,
    build_ok: bool = True,
    run_output: str = "",
    run_returncode: int = 0,
    make_test_ok: bool | None = None,
) -> str:
    """Pure classification of a verification run. No Docker, fully testable.

    Returns one of: no_changes, patch_apply_failed, build_failed, still_crashes,
    unexpected_exit, fixed_tests_failed, verified_correct.
    """
    if not diff.strip():
        return "no_changes"
    if not apply_ok:
        return "patch_apply_failed"
    if not build_ok:
        return "build_failed"
    if crashed(sanitizer, run_output):
        return "still_crashes"
    if run_returncode != 0:
        return "unexpected_exit"
    # Crash is gone. Correctness gate (v1): make test must pass.
    if make_test_ok is False:
        return "fixed_tests_failed"
    return "verified_correct"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_verify_classify.py -v`
Expected: PASS (7 passed).

- [ ] **Step 6: Commit**

```bash
git add arvo-eval/verify_fix.py arvo-eval/tests/conftest.py arvo-eval/tests/test_verify_classify.py
git commit -m "feat: sanitizer-aware classify_run with make-test correctness gate"
```

### Task 4: Wire `classify_run` + `make test` into the Docker `verify()`

**Files:**
- Modify: `arvo-eval/verify_fix.py:45-99`

- [ ] **Step 1: Add the test command constant**

Use the value confirmed in `PHASE0_NOTES.md` (Task 2). Add near the constants:
```python
MRUBY_TEST_CMD = "cd /src/mruby && rake test"  # confirmed in PHASE0_NOTES.md
TEST_TIMEOUT = 1800
```

- [ ] **Step 2: Replace the inline classification in `verify()` with `classify_run` and add the make-test step**

In `verify()`, after the existing build step succeeds and the `arvo` rerun completes, replace the `if "ERROR: AddressSanitizer" ...` block with:
```python
        run_result = docker_exec(container, "arvo", timeout=RUN_TIMEOUT)
        run_output = run_result.stdout + run_result.stderr
        verification["run_output_tail"] = "\n".join(run_output.splitlines()[-30:])
        verification["run_returncode"] = run_result.returncode

        sanitizer = bug["sanitizer"].lower()
        make_test_ok = None
        if not crashed(sanitizer, run_output) and run_result.returncode == 0 and project == "mruby":
            test_result = docker_exec(container, MRUBY_TEST_CMD, timeout=TEST_TIMEOUT)
            make_test_ok = test_result.returncode == 0
            verification["make_test_ok"] = make_test_ok
            verification["make_test_tail"] = "\n".join(
                (test_result.stdout + test_result.stderr).splitlines()[-30:]
            )

        verification["classification"] = classify_run(
            sanitizer=sanitizer,
            diff=diff,
            apply_ok=True,
            build_ok=True,
            run_output=run_output,
            run_returncode=run_result.returncode,
            make_test_ok=make_test_ok,
        )
        return save(instance, verification)
```

Also update the earlier `patch_apply_failed` and `build_failed` branches to keep returning their existing classifications (they already match `classify_run`'s vocabulary, so no rename needed). Confirm `no_changes` still short-circuits before the container is created.

- [ ] **Step 3: Smoke-test against a real bug already in `results/`**

Run (requires Docker; pick a bug with a known-good patch, e.g. 440058794 which has `fix.patch`):
```bash
cp arvo-eval/../bug-runs/results/440058794/fix.patch arvo-eval/results/440058794/patch.diff 2>/dev/null || true
ARVO_DB_PATH=arvo_new.db python3 arvo-eval/verify_fix.py 440058794
```
Expected: JSON output with `"classification": "verified_correct"` (or `fixed_tests_failed` if the suite flags it — either proves the new path runs).

- [ ] **Step 4: Commit**

```bash
git add arvo-eval/verify_fix.py
git commit -m "feat: run mruby make-test gate inside verify() via classify_run"
```

---

## Phase 2 — Store, Ledger, Injector

### Task 5: Playbook store with holdout-safe rendering

**Files:**
- Create: `arvo-eval/playbook_store.py`
- Test: `arvo-eval/tests/test_playbook_store.py`

- [ ] **Step 1: Write failing tests**

`arvo-eval/tests/test_playbook_store.py`:
```python
import json
from pathlib import Path

from playbook_store import (
    new_state, add_heuristic, active_heuristics, render_playbook,
    save_state, load_state,
)


def make_h(text="lesson", tags=("suar",)):
    return {"trigger": "SUAR in pool", "root_cause_lesson": text,
            "how_to_apply": "deep-copy on escape", "tags": list(tags), "confidence": "high"}


def test_add_assigns_provenance_and_bumps_version():
    s = new_state()
    assert s["version"] == 0
    s = add_heuristic(s, make_h(), source_bug=439494108, after_bug=439494108)
    assert s["version"] == 1
    h = s["heuristics"][0]
    assert h["source_bug"] == 439494108
    assert h["added_after_bug"] == 439494108
    assert h["id"] == "h-439494108"


def test_holdout_excludes_own_and_future_lessons():
    s = new_state()
    s = add_heuristic(s, make_h("early"), source_bug=439494108, after_bug=439494108)
    s = add_heuristic(s, make_h("later"), source_bug=440058794, after_bug=440058794)
    # Running 439494108 must see NEITHER (its own is not yet added; later is future).
    assert active_heuristics(s, before_bug=439494108) == []
    # Running 440058794 sees only the earlier lesson.
    got = active_heuristics(s, before_bug=440058794)
    assert [h["source_bug"] for h in got] == [439494108]


def test_render_is_markdown_with_only_active_lessons():
    s = new_state()
    s = add_heuristic(s, make_h("early"), source_bug=439494108, after_bug=439494108)
    md = render_playbook(s, before_bug=440058794)
    assert "early" in md and "SUAR in pool" in md
    md_empty = render_playbook(s, before_bug=439494108)
    assert md_empty.strip() == "" or "No heuristics" in md_empty


def test_save_and_load_roundtrip(tmp_path):
    s = new_state()
    s = add_heuristic(s, make_h(), source_bug=439494108, after_bug=439494108)
    p = tmp_path / "state.json"
    save_state(s, p)
    assert load_state(p)["heuristics"][0]["source_bug"] == 439494108
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_playbook_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'playbook_store'`.

- [ ] **Step 3: Implement `playbook_store.py`**

```python
"""Structured store of mruby repair heuristics with chronological-holdout rendering.

A heuristic carries `added_after_bug` = the localId of the bug after which it was
learned. Rendering for a given bug includes only heuristics with
`added_after_bug < before_bug`, so a bug is never shown its own (or any future)
lesson. This is the holdout guarantee from the design spec.
"""
import json
from pathlib import Path


def new_state() -> dict:
    return {"version": 0, "heuristics": []}


def add_heuristic(state: dict, heuristic: dict, *, source_bug: int, after_bug: int) -> dict:
    entry = dict(heuristic)
    entry["id"] = f"h-{source_bug}"
    entry["source_bug"] = source_bug
    entry["added_after_bug"] = after_bug
    state["heuristics"].append(entry)
    state["version"] += 1
    return state


def active_heuristics(state: dict, before_bug: int) -> list[dict]:
    return [h for h in state["heuristics"] if h["added_after_bug"] < before_bug]


def render_playbook(state: dict, before_bug: int) -> str:
    active = active_heuristics(state, before_bug)
    if not active:
        return "No heuristics yet.\n"
    lines = ["# mruby Repair Playbook", "",
             "Lessons learned from earlier fixes in this project. Apply when relevant.", ""]
    for h in active:
        tags = ", ".join(h.get("tags", []))
        lines += [
            f"## {h['trigger']}  ({tags})",
            f"- **Lesson:** {h['root_cause_lesson']}",
            f"- **How to apply:** {h['how_to_apply']}",
            "",
        ]
    return "\n".join(lines)


def save_state(state: dict, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def load_state(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return new_state()
    return json.loads(path.read_text())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_playbook_store.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/playbook_store.py arvo-eval/tests/test_playbook_store.py
git commit -m "feat: holdout-safe mruby playbook store"
```

### Task 6: Run ledger

**Files:**
- Create: `arvo-eval/ledger.py`
- Test: `arvo-eval/tests/test_ledger.py`

- [ ] **Step 1: Write failing tests**

`arvo-eval/tests/test_ledger.py`:
```python
from ledger import append_record, read_records


def test_append_and_read(tmp_path):
    p = tmp_path / "ledger.jsonl"
    append_record(p, {"bug_id": 439494108, "pass": "treatment", "classification": "verified_correct"})
    append_record(p, {"bug_id": 440058794, "pass": "treatment", "classification": "still_crashes"})
    recs = read_records(p)
    assert len(recs) == 2
    assert recs[0]["bug_id"] == 439494108
    assert recs[1]["classification"] == "still_crashes"


def test_read_missing_file_returns_empty(tmp_path):
    assert read_records(tmp_path / "nope.jsonl") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_ledger.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ledger'`.

- [ ] **Step 3: Implement `ledger.py`**

```python
"""Append-only JSONL ledger of per-run experiment records."""
import json
from pathlib import Path


def append_record(path: Path, record: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def read_records(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_ledger.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/ledger.py arvo-eval/tests/test_ledger.py
git commit -m "feat: jsonl run ledger"
```

### Task 7: Injector

**Files:**
- Create: `arvo-eval/injector.py`
- Test: `arvo-eval/tests/test_injector.py`

Uses the mechanism confirmed in Task 1. The default below assumes "write a file into the per-bug project dir"; if Task 1 chose a different target, adjust `INJECT_FILENAME`/target accordingly and keep the same `inject()` signature.

- [ ] **Step 1: Write failing tests**

`arvo-eval/tests/test_injector.py`:
```python
from pathlib import Path
from injector import inject, INJECT_FILENAME


def test_inject_writes_playbook_into_project_dir(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    inject("# Playbook\n- lesson one\n", project_dir)
    written = (project_dir / INJECT_FILENAME).read_text()
    assert "lesson one" in written


def test_inject_noop_on_empty_text(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    inject("", project_dir)
    assert not (project_dir / INJECT_FILENAME).exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_injector.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'injector'`.

- [ ] **Step 3: Implement `injector.py`**

```python
"""Place the playbook where the crs-claude-code agent will read it.

Mechanism confirmed in PHASE0_NOTES.md (Task 1). Default: a file in the per-bug
OSS-Fuzz project dir that the agent surfaces. The `inject()` signature is stable
so the mechanism can change without touching learn_loop.
"""
from pathlib import Path

INJECT_FILENAME = "HEURISTICS.md"


def inject(playbook_text: str, project_dir: Path) -> None:
    if not playbook_text.strip():
        return
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / INJECT_FILENAME).write_text(playbook_text)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_injector.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/injector.py arvo-eval/tests/test_injector.py
git commit -m "feat: playbook injector"
```

---

## Phase 3 — LLM Extraction & Compression

### Task 8: Anthropic SDK wrapper + dependency

**Files:**
- Create: `arvo-eval/llm.py`
- Modify: `arvo-eval/requirements.txt`
- Test: `arvo-eval/tests/test_llm.py`

- [ ] **Step 1: Add the dependency**

Append to `arvo-eval/requirements.txt`:
```
anthropic>=0.40
```
Then install: `python3 -m pip install 'anthropic>=0.40'`

- [ ] **Step 2: Write a failing test (client is injectable, so no network needed)**

`arvo-eval/tests/test_llm.py`:
```python
from llm import call_llm


class FakeMessages:
    def create(self, **kwargs):
        class R:
            content = [type("Block", (), {"text": "hello from stub"})()]
        return R()


class FakeClient:
    messages = FakeMessages()


def test_call_llm_returns_text_from_client():
    out = call_llm("say hi", system="be terse", client=FakeClient())
    assert out == "hello from stub"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_llm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'llm'`.

- [ ] **Step 4: Implement `llm.py`**

```python
"""Thin Anthropic wrapper. `client` is injectable for tests."""
import os

MODEL = "claude-opus-4-8"
MAX_TOKENS = 1024


def _default_client():
    import anthropic
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def call_llm(prompt: str, *, system: str = "", client=None, max_tokens: int = MAX_TOKENS) -> str:
    client = client or _default_client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in resp.content).strip()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_llm.py -v`
Expected: PASS (1 passed).

- [ ] **Step 6: Commit**

```bash
git add arvo-eval/llm.py arvo-eval/requirements.txt arvo-eval/tests/test_llm.py
git commit -m "feat: injectable anthropic llm wrapper"
```

### Task 9: Heuristic extractor

**Files:**
- Create: `arvo-eval/extract_heuristic.py`
- Test: `arvo-eval/tests/test_extract_heuristic.py`

- [ ] **Step 1: Write failing tests with a stub LLM**

`arvo-eval/tests/test_extract_heuristic.py`:
```python
import json
from extract_heuristic import extract_heuristic

VALID = json.dumps({
    "trigger": "Stack-use-after-return in bigint pool path",
    "root_cause_lesson": "mpz values built in a stack pool escape via bint_new heap path",
    "how_to_apply": "pool-aware mpz_move / deep-copy before the value escapes the frame",
    "tags": ["suar", "bigint-pool"],
    "confidence": "high",
})


def test_extract_parses_structured_heuristic():
    h = extract_heuristic(
        bug={"localId": 439494108, "crash_type": "Stack-use-after-return READ 4",
             "sanitizer": "asan", "fuzz_target": "mruby_fuzzer", "crash_output": "..."},
        diff="--- a/x\n+++ b/x\n",
        trajectory_summary="agent traced the escape",
        verdict="verified_correct",
        llm=lambda prompt, system="": VALID,
    )
    assert h["trigger"].startswith("Stack-use-after-return")
    assert "suar" in h["tags"]


def test_extract_tolerates_fenced_json():
    h = extract_heuristic(
        bug={"localId": 1, "crash_type": "x", "sanitizer": "asan",
             "fuzz_target": "f", "crash_output": ""},
        diff="d", trajectory_summary="", verdict="verified_correct",
        llm=lambda prompt, system="": f"```json\n{VALID}\n```",
    )
    assert h["confidence"] == "high"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_extract_heuristic.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'extract_heuristic'`.

- [ ] **Step 3: Implement `extract_heuristic.py`**

```python
"""Turn one verified-correct repair into a structured, reusable heuristic."""
import json

from llm import call_llm

SYSTEM = (
    "You distill C/C++ vulnerability fixes into terse, reusable repair heuristics "
    "for the mruby interpreter. Output ONLY a JSON object with keys: trigger, "
    "root_cause_lesson, how_to_apply, tags (array of short slugs), confidence "
    "(high|medium|low). Be specific to the bug class, not generic advice. Keep each "
    "string under 240 characters."
)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


def build_prompt(bug: dict, diff: str, trajectory_summary: str, verdict: str) -> str:
    return f"""Bug {bug['localId']} ({bug['crash_type']}, {bug['sanitizer']}, target {bug['fuzz_target']}).
Verdict: {verdict}.

Crash output:
{bug.get('crash_output', '')[:3000]}

Accepted fix diff:
{diff[:6000]}

Agent reasoning summary:
{trajectory_summary[:2000]}

Produce the heuristic JSON now."""


def extract_heuristic(*, bug: dict, diff: str, trajectory_summary: str, verdict: str, llm=call_llm) -> dict:
    raw = llm(build_prompt(bug, diff, trajectory_summary, verdict), system=SYSTEM)
    data = json.loads(_strip_fences(raw))
    # Normalize required keys so the store never receives a malformed entry.
    return {
        "trigger": data["trigger"],
        "root_cause_lesson": data["root_cause_lesson"],
        "how_to_apply": data["how_to_apply"],
        "tags": list(data.get("tags", [])),
        "confidence": data.get("confidence", "medium"),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_extract_heuristic.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/extract_heuristic.py arvo-eval/tests/test_extract_heuristic.py
git commit -m "feat: llm heuristic extractor"
```

### Task 10: Cap-triggered curator (ephemeral compression at injection time)

**Files:**
- Create: `arvo-eval/curator.py`
- Test: `arvo-eval/tests/test_curator.py`

**Design note:** Per the spec's "self-compressing" intent, compression runs at *injection time* on the already-holdout-filtered rendered text, and is never written back to the store. This keeps provenance/holdout exact while bounding context size. It only fires when the render exceeds `MAX_PLAYBOOK_CHARS`.

- [ ] **Step 1: Write failing tests with a stub LLM**

`arvo-eval/tests/test_curator.py`:
```python
from curator import maybe_compress, MAX_PLAYBOOK_CHARS


def test_under_cap_is_returned_unchanged():
    text = "short playbook"
    assert maybe_compress(text, llm=lambda p, system="": "SHOULD NOT BE CALLED") == text


def test_over_cap_is_compressed_via_llm():
    big = "x" * (MAX_PLAYBOOK_CHARS + 10)
    out = maybe_compress(big, llm=lambda p, system="": "compressed digest")
    assert out == "compressed digest"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_curator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'curator'`.

- [ ] **Step 3: Implement `curator.py`**

```python
"""Compress the rendered playbook only when it grows past a size cap.

Compression is ephemeral (used for one injection); the structured store is the
source of truth and is never overwritten here.
"""
from llm import call_llm

MAX_PLAYBOOK_CHARS = 3000

SYSTEM = (
    "You compress a list of mruby repair heuristics into a shorter digest WITHOUT "
    "dropping any distinct lesson or merging unrelated bug classes. Preserve the "
    "trigger -> lesson -> how-to-apply structure. Output markdown only."
)


def maybe_compress(playbook_text: str, *, llm=call_llm) -> str:
    if len(playbook_text) <= MAX_PLAYBOOK_CHARS:
        return playbook_text
    return llm(
        f"Compress this playbook to under {MAX_PLAYBOOK_CHARS} characters:\n\n{playbook_text}",
        system=SYSTEM,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_curator.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/curator.py arvo-eval/tests/test_curator.py
git commit -m "feat: cap-triggered ephemeral playbook compression"
```

---

## Phase 4 — Orchestrator

### Task 11: mruby bug ordering helper

**Files:**
- Create: `arvo-eval/mruby_bugs.py`
- Test: `arvo-eval/tests/test_mruby_bugs.py`

- [ ] **Step 1: Write a failing test against the real `arvo_new.db`**

`arvo-eval/tests/test_mruby_bugs.py`:
```python
from pathlib import Path
import pytest
from mruby_bugs import mruby_bug_ids

DB = Path(__file__).resolve().parents[1] / "arvo_new.db"


@pytest.mark.skipif(not DB.exists(), reason="arvo_new.db not present")
def test_returns_sorted_mruby_ids():
    ids = mruby_bug_ids(DB)
    assert len(ids) == 30
    assert ids == sorted(ids)          # chronological by localId
    assert ids[0] == 439237851         # earliest mruby bug
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_mruby_bugs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mruby_bugs'`.

Note: `arvo_new.db` is large and not committed (see arvo-expanded README). The test self-skips when absent; download it from the GitHub release first to exercise it locally.

- [ ] **Step 3: Implement `mruby_bugs.py`**

```python
"""List mruby bug localIds in chronological (localId) order from the ARVO db."""
import sqlite3
from pathlib import Path


def mruby_bug_ids(db_path: Path) -> list[int]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT localId FROM arvo WHERE project = 'mruby' ORDER BY localId"
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]
```

- [ ] **Step 4: Run test to verify it passes (or skips if db absent)**

Run: `PYTHONPATH=arvo-eval ARVO_DB_PATH=arvo-eval/arvo_new.db python3 -m pytest arvo-eval/tests/test_mruby_bugs.py -v`
Expected: PASS, or SKIPPED if `arvo_new.db` is not downloaded.

- [ ] **Step 5: Commit**

```bash
git add arvo-eval/mruby_bugs.py arvo-eval/tests/test_mruby_bugs.py
git commit -m "feat: chronological mruby bug id helper"
```

### Task 12: `learn_loop.py` orchestrator + dry-run integration test

**Files:**
- Create: `arvo-eval/learn_loop.py`
- Test: `arvo-eval/tests/test_learn_loop_dryrun.py`

The loop is structured so the three side-effecting collaborators — the agent runner, the verifier, and the extractor LLM — are injectable. The dry-run test swaps in stubs and asserts the end-to-end wiring **and the holdout invariant** without Docker or network.

- [ ] **Step 1: Write the failing integration test**

`arvo-eval/tests/test_learn_loop_dryrun.py`:
```python
from pathlib import Path
from learn_loop import run_pass


def stub_agent(bug_id, project_dir, skip_build):
    # Pretend the agent produced a patch; echo the injected playbook back so the
    # test can assert holdout (a bug must never see its own lesson).
    injected = ""
    hfile = Path(project_dir) / "HEURISTICS.md"
    if hfile.exists():
        injected = hfile.read_text()
    return {"diff": f"--- a/x\n+++ b/x\n# fix {bug_id}\n", "injected_seen": injected,
            "trajectory_summary": f"fixed {bug_id}"}


def stub_verify(bug_id, diff):
    return {"classification": "verified_correct", "make_test_ok": True}


def stub_extract(bug, diff, trajectory_summary, verdict):
    return {"trigger": f"pattern from {bug['localId']}", "root_cause_lesson": f"lesson {bug['localId']}",
            "how_to_apply": "apply", "tags": ["t"], "confidence": "high"}


def test_dryrun_treatment_pass_is_holdout_safe(tmp_path):
    bugs = [{"localId": 100, "crash_type": "c", "sanitizer": "asan", "fuzz_target": "f", "crash_output": ""},
            {"localId": 200, "crash_type": "c", "sanitizer": "asan", "fuzz_target": "f", "crash_output": ""}]
    result = run_pass(
        bugs=bugs, pass_name="treatment", inject_enabled=True,
        state_path=tmp_path / "state.json", ledger_path=tmp_path / "ledger.jsonl",
        project_dir_for=lambda bid: tmp_path / f"proj-{bid}",
        agent=stub_agent, verify=stub_verify, extract=stub_extract,
    )
    # Bug 100 ran with an empty playbook (nothing learned yet).
    assert "lesson 100" not in result[0]["injected_seen"]
    # Bug 200 saw bug 100's lesson but NOT its own.
    assert "lesson 100" in result[1]["injected_seen"]
    assert "lesson 200" not in result[1]["injected_seen"]


def test_dryrun_control_pass_injects_nothing(tmp_path):
    bugs = [{"localId": 100, "crash_type": "c", "sanitizer": "asan", "fuzz_target": "f", "crash_output": ""},
            {"localId": 200, "crash_type": "c", "sanitizer": "asan", "fuzz_target": "f", "crash_output": ""}]
    result = run_pass(
        bugs=bugs, pass_name="control", inject_enabled=False,
        state_path=tmp_path / "state.json", ledger_path=tmp_path / "ledger.jsonl",
        project_dir_for=lambda bid: tmp_path / f"proj-{bid}",
        agent=stub_agent, verify=stub_verify, extract=stub_extract,
    )
    assert result[1]["injected_seen"] == ""   # never injected, even though store grew
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_learn_loop_dryrun.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'learn_loop'`.

- [ ] **Step 3: Implement `learn_loop.py`**

```python
"""Chronological self-improving repair loop for mruby ARVO bugs.

Per bug (in localId order): render the holdout-filtered playbook, optionally
inject it, run the agent, verify, log, and — only on verified_correct — extract a
heuristic and add it to the store AFTER the bug has been evaluated.
"""
import os
import sys
from pathlib import Path

from playbook_store import load_state, save_state, add_heuristic, render_playbook
from injector import inject
from curator import maybe_compress
from ledger import append_record
from mruby_bugs import mruby_bug_ids


def _default_agent(bug_id, project_dir, skip_build):
    """Real agent: drive OSS-CRS, then return the chosen patch + trajectory tail."""
    from arvo_oss_crs import run_oss_crs
    summary = run_oss_crs(bug_id, skip_build=skip_build)
    results_dir = Path(__file__).parent / "results" / str(bug_id)
    patch = results_dir / "oss_crs_patch_0.diff"
    diff = patch.read_text() if patch.exists() else ""
    log = results_dir / "oss_crs_claude_stdout.log"
    trajectory = "\n".join(log.read_text().splitlines()[-80:]) if log.exists() else ""
    # verify_fix reads results/<id>/patch.diff; bridge the OSS-CRS naming.
    if diff:
        (results_dir / "patch.diff").write_text(diff)
    return {"diff": diff, "trajectory_summary": trajectory, "summary": summary}


def _default_verify(bug_id, diff):
    from verify_fix import verify
    return verify(bug_id)


def _default_extract(bug, diff, trajectory_summary, verdict):
    from extract_heuristic import extract_heuristic
    return extract_heuristic(bug=bug, diff=diff, trajectory_summary=trajectory_summary, verdict=verdict)


def run_pass(*, bugs, pass_name, inject_enabled, state_path, ledger_path,
             project_dir_for, agent=_default_agent, verify=_default_verify,
             extract=_default_extract, skip_build=False):
    """Run one full pass over `bugs` (already in chronological order)."""
    state = load_state(state_path)
    records = []
    for bug in bugs:
        bug_id = bug["localId"]
        project_dir = Path(project_dir_for(bug_id))
        project_dir.mkdir(parents=True, exist_ok=True)

        # Holdout-filtered render: only lessons from strictly-earlier bugs.
        playbook = render_playbook(state, before_bug=bug_id)
        if inject_enabled:
            inject(maybe_compress(playbook), project_dir)

        run = agent(bug_id, project_dir, skip_build)
        diff = run.get("diff", "")
        verdict = verify(bug_id, diff)["classification"] if diff.strip() else "no_changes"

        record = {"bug_id": bug_id, "pass": pass_name, "classification": verdict,
                  "playbook_version": state["version"]}
        append_record(ledger_path, record)

        if verdict == "verified_correct":
            heuristic = extract(bug, diff, run.get("trajectory_summary", ""), verdict)
            state = add_heuristic(state, heuristic, source_bug=bug_id, after_bug=bug_id)
            save_state(state, state_path)

        records.append({**record, **{k: run[k] for k in ("injected_seen",) if k in run}})
    return records


def main():
    db = Path(os.environ.get("ARVO_DB_PATH", Path(__file__).parent / "arvo_new.db"))
    bugs_ids = mruby_bug_ids(db)
    from build_instance import load_bug
    bugs = [load_bug(b) for b in bugs_ids]

    base = Path(__file__).parent
    pb_dir = base / "playbook"
    learn_dir = base / "results" / "learn"
    project_dir_for = lambda bid: Path.home() / ".arvo-oss-crs" / str(bid) / "project"

    pass_name = os.environ.get("LEARN_PASS", "treatment")
    inject_enabled = pass_name == "treatment"
    run_pass(
        bugs=bugs, pass_name=pass_name, inject_enabled=inject_enabled,
        state_path=pb_dir / f"playbook_state_{pass_name}.json",
        ledger_path=learn_dir / "ledger.jsonl",
        project_dir_for=project_dir_for,
        skip_build="--skip-build" in sys.argv,
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests/test_learn_loop_dryrun.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the full unit suite**

Run: `PYTHONPATH=arvo-eval python3 -m pytest arvo-eval/tests -v`
Expected: all green (skips allowed for the db-dependent test).

- [ ] **Step 6: Commit**

```bash
git add arvo-eval/learn_loop.py arvo-eval/tests/test_learn_loop_dryrun.py
git commit -m "feat: chronological holdout learn loop orchestrator"
```

---

## Phase 5 — Experiment Runbook

### Task 13: Document and run the control/treatment experiment

**Files:**
- Modify: `arvo-eval/README.md`

- [ ] **Step 1: Add a runbook section to `arvo-eval/README.md`**

Append:
````markdown
## Heuristic Learning Loop (mruby)

Self-improving playbook over mruby's 30 bugs in chronological (`localId`) order.
Requires `arvo_new.db` (download from the GitHub release), `CLAUDE_CODE_OAUTH_TOKEN`
(agent) and `ANTHROPIC_API_KEY` (heuristic extraction).

Run the control pass (no playbook injected), then the treatment pass (injected):
```bash
ARVO_DB_PATH=arvo_new.db LEARN_PASS=control   python3 learn_loop.py
ARVO_DB_PATH=arvo_new.db LEARN_PASS=treatment python3 learn_loop.py
```
Results accumulate in `results/learn/ledger.jsonl`. Compare `verified_correct`
rate on the later ~two-thirds of bugs between passes. N=30 is a pilot — read the
delta as directional signal, not statistical proof.
````

- [ ] **Step 2: Execute the control pass** (real Docker + agent; long-running)

```bash
ARVO_DB_PATH=arvo_new.db LEARN_PASS=control python3 arvo-eval/learn_loop.py
```
Expected: a `control` record per bug appended to `results/learn/ledger.jsonl`.

- [ ] **Step 3: Execute the treatment pass**

```bash
ARVO_DB_PATH=arvo_new.db LEARN_PASS=treatment python3 arvo-eval/learn_loop.py
```
Expected: `treatment` records; `playbook/playbook_state_treatment.json` grows as bugs are fixed.

- [ ] **Step 4: Compute and record the comparison**

```bash
PYTHONPATH=arvo-eval python3 -c "
from ledger import read_records
r = read_records('arvo-eval/results/learn/ledger.jsonl')
for p in ('control','treatment'):
    rows = [x for x in r if x['pass']==p]
    tail = rows[len(rows)//3:]   # later two-thirds
    ok = sum(1 for x in tail if x['classification']=='verified_correct')
    print(p, 'tail verified_correct:', ok, '/', len(tail))
"
```

- [ ] **Step 5: Commit the runbook and results**

```bash
git add arvo-eval/README.md arvo-eval/results/learn/ledger.jsonl arvo-eval/playbook/
git commit -m "docs: heuristic learning loop runbook + initial experiment results"
```

---

## Self-Review Notes

- **Spec coverage:** §3 flow → Task 12; §4.1 orchestrator → Task 12; §4.2 store → Task 5; §4.3 injector → Tasks 1,7; §4.4 verifier (sanitizer-aware + make test) → Tasks 3,4; §4.5 extractor → Task 9; §4.6 curator → Task 10; §4.7 ledger → Task 6; §6 control/treatment → Tasks 12,13; §8 phases → Tasks 1–13; §9 testing incl. holdout invariant → Tasks 5,12.
- **Holdout invariant** is enforced structurally in `playbook_store.active_heuristics` (Task 5) and asserted end-to-end in the dry-run (Task 12).
- **Deferred per spec:** differential-vs-`-fix` correctness oracle (v2); negative/failure heuristics; multi-project portability.
