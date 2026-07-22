# Crash Orientation (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parse the sanitizer trace already stored in `arvo.crash_output` into an `ORIENTATION.md` and inject it into the repair agent's workspace, so it starts oriented instead of burning its wall-clock cap re-deriving the crash location.

**Architecture:** A new pure-logic module `orientation.py` (parse + render, no I/O) is unit-tested against real crash traces captured as fixtures. A thin `inject_orientation()` in `arvo_oss_crs.py` writes `ORIENTATION.md` into the agent's `target-source` dir and prepends a one-line pointer to `HEURISTICS.md`, gated by `OSS_CRS_ORIENT=1` and applied to both control and treatment passes.

**Tech Stack:** Python 3, stdlib `re`/`dataclasses`, pytest. Run tests with `PYTHONPATH=. python3 -m pytest tests -q` from `arvo-eval/`.

**Scope note:** This is Phase 1 of the spec (`docs/superpowers/specs/2026-07-20-crash-orientation-and-learned-recon-scripts-design.md`). Phase 2 (learned recon scripts) gets its own plan after Phase 1 is validated in a live run (spec §8).

---

## File Structure

- **Create** `arvo-eval/orientation.py` — pure parsing/rendering. `parse_crash_output()`, `render_orientation()`, `Orientation`/`Frame` dataclasses, `HEURISTICS_POINTER` constant. No filesystem/network.
- **Create** `arvo-eval/tests/test_orientation.py` — hermetic unit tests over fixtures.
- **Create** `arvo-eval/tests/fixtures/crash_439494108_asan.txt` — real ASan stack-use-after-return trace.
- **Create** `arvo-eval/tests/fixtures/crash_440058794_msan.txt` — real MSan use-of-uninitialized-value trace.
- **Modify** `arvo-eval/arvo_oss_crs.py` — add `inject_orientation()` and call it in `run_oss_crs()` after `inject_heuristics()` (line 456).
- **Modify** `arvo-eval/tests/test_arvo_oss_crs.py` — tests for `inject_orientation()` gating + file writing.

All work is committed on the current branch `fix/verify-pipeline-e2e`.

---

## Task 1: Capture real crash traces as test fixtures

**Files:**
- Create: `arvo-eval/tests/fixtures/crash_439494108_asan.txt`
- Create: `arvo-eval/tests/fixtures/crash_440058794_msan.txt`

- [ ] **Step 1: Create the fixtures dir and dump both real traces from the DB**

Run from `arvo-eval/`:

```bash
mkdir -p tests/fixtures
python3 -c "
import sqlite3
c = sqlite3.connect('arvo_new.db'); c.row_factory = sqlite3.Row
for bid, name in [(439494108, 'crash_439494108_asan.txt'), (440058794, 'crash_440058794_msan.txt')]:
    row = dict(c.execute('SELECT crash_output FROM arvo WHERE localId=?', (bid,)).fetchone())
    open(f'tests/fixtures/{name}', 'w').write(row['crash_output'])
    print('wrote', name, len(row['crash_output']), 'bytes')
"
```

Expected output:
```
wrote crash_439494108_asan.txt 4208 bytes
wrote crash_440058794_msan.txt 6790 bytes
```

- [ ] **Step 2: Sanity-check the fixtures contain the expected frames**

Run:
```bash
grep -m1 'limb_addmul_1' tests/fixtures/crash_439494108_asan.txt
grep -m1 'mrb_obj_hash_code' tests/fixtures/crash_440058794_msan.txt
```
Expected: one matching line from each (the fault-site frames).

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/crash_439494108_asan.txt tests/fixtures/crash_440058794_msan.txt
git commit -m "test(orientation): capture real ASan+MSan crash traces as fixtures"
```

---

## Task 2: `Orientation`/`Frame` dataclasses + frame regex + ASan parsing

**Files:**
- Create: `arvo-eval/orientation.py`
- Test: `arvo-eval/tests/test_orientation.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_orientation.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest tests/test_orientation.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'orientation'`.

- [ ] **Step 3: Write minimal implementation**

Create `orientation.py`:

```python
"""Parse an OSS-Fuzz sanitizer crash report into a compact orientation for the
repair agent. Pure logic (no I/O) so it can be unit-tested against real traces.

The crash report is a deployment-faithful signal: it is exactly what OSS-Fuzz
hands a real developer. This module only extracts what the report already states
(crash class, faulting frame, call chain, root-cause frame); it adds no knowledge
of the upstream fix.
"""
import re
from dataclasses import dataclass, field

# One stack frame line, e.g.:
#   #0 0x55e851c07304 in limb_addmul_1 /src/mruby/mrbgems/.../bigint.c:726:58
_FRAME_RE = re.compile(
    r"#\d+ 0x[0-9a-f]+ in (?P<func>\S+) (?P<path>/\S+?):(?P<line>\d+)(?::\d+)?"
)
# The crash-class line: "ERROR: AddressSanitizer: stack-use-after-return ..." or
# "WARNING: MemorySanitizer: use-of-uninitialized-value".
_CLASS_RE = re.compile(
    r"(?:ERROR|WARNING): \w*Sanitizer: (?P<cls>[a-z][a-z0-9-]+)"
)


@dataclass
class Frame:
    func: str
    path: str   # repo-relative (leading /src/<project>/ stripped)
    line: int


@dataclass
class Orientation:
    crash_class: str | None
    summary_line: str | None
    fault_site: Frame | None
    call_chain: list[Frame]
    source_frame: Frame | None
    raw_trace: str


def _app_frame(func: str, path: str, line: str, prefix: str) -> Frame | None:
    """A Frame iff the path is inside the project's own source tree."""
    if not path.startswith(prefix):
        return None
    return Frame(func=func, path=path[len(prefix):], line=int(line))


def parse_crash_output(crash_output: str, crash_type: str, project: str) -> Orientation | None:
    """Return an Orientation, or None if there is no usable crash text."""
    if not (crash_output or "").strip():
        return None
    prefix = f"/src/{project}/"

    m = _CLASS_RE.search(crash_output)
    crash_class = m.group("cls") if m else None

    call_chain: list[Frame] = []
    for fm in _FRAME_RE.finditer(crash_output):
        fr = _app_frame(fm.group("func"), fm.group("path"), fm.group("line"), prefix)
        if fr is not None:
            call_chain.append(fr)

    fault_site = call_chain[0] if call_chain else None
    return Orientation(
        crash_class=crash_class,
        summary_line=None,
        fault_site=fault_site,
        call_chain=call_chain,
        source_frame=None,
        raw_trace=crash_output,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m pytest tests/test_orientation.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add orientation.py tests/test_orientation.py
git commit -m "feat(orientation): parse sanitizer crash class + app-frame call chain"
```

---

## Task 3: Source (root-cause) frame extraction

**Files:**
- Modify: `arvo-eval/orientation.py`
- Test: `arvo-eval/tests/test_orientation.py`

The sanitizer prints a secondary frame group for the root cause — after a marker
like `located in stack of` (ASan SUAR), `freed by` (ASan UAF), or
`Uninitialized value was created by` (MSan). We capture the first app frame that
appears *after* such a marker.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_orientation.py`:

```python
def test_asan_source_frame_is_root_cause():
    o = parse_crash_output(_asan(), "Stack-use-after-return READ 4", "mruby")
    assert o.source_frame is not None
    assert o.source_frame.func == "mrb_bint_reduce"
    assert o.source_frame.path == "mrbgems/mruby-bigint/core/bigint.c"
    assert o.source_frame.line == 3673
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest tests/test_orientation.py::test_asan_source_frame_is_root_cause -q`
Expected: FAIL — `assert None is not None` (source_frame is still None).

- [ ] **Step 3: Update implementation**

In `orientation.py`, add the marker constant near the top (after `_CLASS_RE`):

```python
# Markers that introduce the sanitizer's root-cause frame group.
_SOURCE_MARKERS = (
    "located in stack of",
    "previously allocated by",
    "freed by",
    "allocated by",
    "Uninitialized value was created by",
)


def _source_frame(crash_output: str, prefix: str) -> Frame | None:
    """First app frame appearing after a root-cause marker line."""
    after_marker = False
    for line in crash_output.splitlines():
        if not after_marker:
            if any(mk in line for mk in _SOURCE_MARKERS):
                after_marker = True
            continue
        fm = _FRAME_RE.search(line)
        if fm:
            fr = _app_frame(fm.group("func"), fm.group("path"), fm.group("line"), prefix)
            if fr is not None:
                return fr
    return None
```

Then in `parse_crash_output`, replace `source_frame=None,` with a real call — change the return block to compute it first:

```python
    source_frame = _source_frame(crash_output, prefix)
    return Orientation(
        crash_class=crash_class,
        summary_line=None,
        fault_site=fault_site,
        call_chain=call_chain,
        source_frame=source_frame,
        raw_trace=crash_output,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m pytest tests/test_orientation.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add orientation.py tests/test_orientation.py
git commit -m "feat(orientation): extract sanitizer root-cause (source) frame"
```

---

## Task 4: MSan support + summary line + empty/partial handling

**Files:**
- Modify: `arvo-eval/orientation.py`
- Test: `arvo-eval/tests/test_orientation.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orientation.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest tests/test_orientation.py -q`
Expected: FAIL — `test_summary_line_captured_when_present` fails (summary_line is None); MSan/empty/partial tests should already pass from Task 2's generic logic, but summary_line is unimplemented.

- [ ] **Step 3: Update implementation**

In `orientation.py`, inside `parse_crash_output`, after computing `crash_class`, add summary capture:

```python
    summary_line = next(
        (ln.strip() for ln in crash_output.splitlines() if ln.strip().startswith("SUMMARY:")),
        None,
    )
```

And set `summary_line=summary_line,` in the returned `Orientation` (replacing `summary_line=None,`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m pytest tests/test_orientation.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add orientation.py tests/test_orientation.py
git commit -m "feat(orientation): MSan support, summary line, empty/partial handling"
```

---

## Task 5: Render `ORIENTATION.md` + pointer constant

**Files:**
- Modify: `arvo-eval/orientation.py`
- Test: `arvo-eval/tests/test_orientation.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_orientation.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest tests/test_orientation.py::test_render_contains_key_fields -q`
Expected: FAIL — `ImportError: cannot import name 'render_orientation'`.

- [ ] **Step 3: Add implementation**

Append to `orientation.py`:

```python
HEURISTICS_POINTER = (
    "Read ORIENTATION.md first -- it has the parsed crash trace (class, fault "
    "site, call chain, root-cause frame). Do not re-derive it by grepping.\n\n"
)


def _fmt_frame(fr: Frame) -> str:
    return f"{fr.func}    {fr.path}:{fr.line}"


def _trim_trace(raw: str) -> str:
    """Drop the libFuzzer preamble; keep from the first sanitizer line onward, capped."""
    lines = raw.splitlines()
    start = next(
        (i for i, ln in enumerate(lines)
         if "Sanitizer:" in ln or ln.strip().startswith(("#0", "==", "ERROR", "WARNING"))),
        0,
    )
    return "\n".join(lines[start:])[:3500]


def render_orientation(o: Orientation) -> str:
    """Render an ORIENTATION.md body for the repair agent."""
    out = ["# Crash orientation (parsed from the sanitizer report -- a real developer signal)"]
    if o.crash_class:
        out.append(f"Class:       {o.crash_class}")
    if o.fault_site:
        out.append(f"Fault site:  {_fmt_frame(o.fault_site)}")
    if o.call_chain:
        out.append("Call chain:  " + " <- ".join(f.func for f in o.call_chain))
    if o.source_frame:
        out.append("Source frame (where the bad memory came from):")
        out.append(f"             {_fmt_frame(o.source_frame)}")
    out.append(
        "\n-> Read these functions first, form a root-cause hypothesis, make your "
        "first edit, then run check-patch. Do NOT re-derive the trace by grepping "
        "the codebase.\n"
    )
    out.append("```")
    out.append(_trim_trace(o.raw_trace))
    out.append("```")
    return "\n".join(out) + "\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m pytest tests/test_orientation.py -q`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add orientation.py tests/test_orientation.py
git commit -m "feat(orientation): render ORIENTATION.md + HEURISTICS pointer"
```

---

## Task 6: `inject_orientation()` wiring in `arvo_oss_crs.py`

**Files:**
- Modify: `arvo-eval/arvo_oss_crs.py` (add import + `inject_orientation`; call in `run_oss_crs`)
- Test: `arvo-eval/tests/test_arvo_oss_crs.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_arvo_oss_crs.py`:

```python
def test_inject_orientation_writes_files_and_pointer(tmp_path, monkeypatch):
    import arvo_oss_crs
    monkeypatch.setenv("OSS_CRS_ORIENT", "1")
    monkeypatch.setattr(arvo_oss_crs, "find_target_source_dir", lambda san: tmp_path)
    (tmp_path / "HEURISTICS.md").write_text("EXISTING PLAYBOOK\n")
    bug = {
        "localId": 439494108, "project": "mruby",
        "crash_type": "Stack-use-after-return READ 4",
        "crash_output": (
            "==7==ERROR: AddressSanitizer: stack-use-after-return on address 0x1\n"
            "    #0 0x1 in limb_addmul_1 /src/mruby/mrbgems/mruby-bigint/core/bigint.c:726:58\n"
            "SUMMARY: AddressSanitizer: stack-use-after-return bigint.c:726\n"
        ),
    }
    assert arvo_oss_crs.inject_orientation("address", bug) is True
    assert "limb_addmul_1" in (tmp_path / "ORIENTATION.md").read_text()
    heur = (tmp_path / "HEURISTICS.md").read_text()
    assert heur.startswith("Read ORIENTATION.md first")
    assert "EXISTING PLAYBOOK" in heur   # pointer prepended, not clobbered


def test_inject_orientation_disabled_by_default(tmp_path, monkeypatch):
    import arvo_oss_crs
    monkeypatch.delenv("OSS_CRS_ORIENT", raising=False)
    monkeypatch.setattr(arvo_oss_crs, "find_target_source_dir", lambda san: tmp_path)
    bug = {"localId": 1, "project": "mruby", "crash_type": "x", "crash_output": "==ERROR: ..."}
    assert arvo_oss_crs.inject_orientation("address", bug) is False
    assert not (tmp_path / "ORIENTATION.md").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest tests/test_arvo_oss_crs.py -k inject_orientation -q`
Expected: FAIL — `AttributeError: module 'arvo_oss_crs' has no attribute 'inject_orientation'`.

- [ ] **Step 3: Add implementation**

In `arvo_oss_crs.py`, add the import near the other local imports at the top of the file (the module already imports from sibling modules; place with them):

```python
from orientation import parse_crash_output, render_orientation, HEURISTICS_POINTER
```

Then add this function immediately after `inject_heuristics` (after line ~255):

```python
def inject_orientation(sanitizer: str, bug: dict) -> bool:
    """Write ORIENTATION.md (parsed sanitizer trace) into the agent's source dir and
    prepend a pointer to HEURISTICS.md. Gated by OSS_CRS_ORIENT=1. Applied to both
    passes -- orientation is a harness signal, not the playbook under test -- so it
    must not skew the control/treatment comparison. Returns True if it injected."""
    if os.environ.get("OSS_CRS_ORIENT") != "1":
        return False
    o = parse_crash_output(bug.get("crash_output") or "", bug.get("crash_type") or "", bug["project"])
    if o is None:
        return False
    target_source = find_target_source_dir(sanitizer)
    if target_source is None:
        print(f"[{bug['localId']}] Warning: no target-source dir, skipping orientation")
        return False
    (target_source / "ORIENTATION.md").write_text(render_orientation(o))
    heur = target_source / "HEURISTICS.md"
    existing = heur.read_text() if heur.exists() else ""
    if HEURISTICS_POINTER not in existing:
        heur.write_text(HEURISTICS_POINTER + existing)
    print(f"[{bug['localId']}] Injected ORIENTATION.md into {target_source}")
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m pytest tests/test_arvo_oss_crs.py -k inject_orientation -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add arvo_oss_crs.py tests/test_arvo_oss_crs.py
git commit -m "feat(orientation): inject_orientation writes ORIENTATION.md + HEURISTICS pointer"
```

---

## Task 7: Call `inject_orientation` in `run_oss_crs`

**Files:**
- Modify: `arvo-eval/arvo_oss_crs.py:456`

- [ ] **Step 1: Add the call**

In `run_oss_crs`, immediately after the existing line 456:

```python
    inject_heuristics(project_dir, sanitizer, bug_id, bug["project"])
```

add:

```python
    inject_orientation(sanitizer, bug)
```

(`bug` is already in scope from `bug = load_bug(bug_id)` at line 418, and carries `crash_output`/`crash_type`/`project`/`localId`.)

- [ ] **Step 2: Run the full test suite to verify nothing regressed**

Run: `PYTHONPATH=. python3 -m pytest tests -q`
Expected: PASS — all prior tests plus the 12 new orientation tests. No failures.

- [ ] **Step 3: Commit**

```bash
git add arvo_oss_crs.py
git commit -m "feat(orientation): wire inject_orientation into run_oss_crs (both passes)"
```

---

## Task 8: Validate the parser across all 30 real traces (manual gate)

**Files:** none (a one-off verification command; not a committed test, to keep the suite hermetic).

- [ ] **Step 1: Run the parser over every mruby trace in the DB and eyeball the output**

Run from `arvo-eval/`:

```bash
PYTHONPATH=. python3 -c "
import sqlite3
from orientation import parse_crash_output
c = sqlite3.connect('arvo_new.db'); c.row_factory = sqlite3.Row
rows = c.execute(\"SELECT localId, crash_type, crash_output FROM arvo WHERE project='mruby' ORDER BY localId\").fetchall()
ok = miss = 0
for r in rows:
    o = parse_crash_output(r['crash_output'] or '', r['crash_type'] or '', 'mruby')
    if o and o.fault_site:
        ok += 1
        print(f\"{r['localId']}  {o.crash_class:32s} {o.fault_site.func}  {o.fault_site.path}:{o.fault_site.line}\")
    else:
        miss += 1
        print(f\"{r['localId']}  NO FAULT SITE  (class={o.crash_class if o else None})\")
print(f'--- {ok} parsed with a fault site, {miss} without ---')
"
```

Expected: the large majority parse with a plausible in-project fault site (mruby paths like `mrbgems/...` or `src/...`). A handful without a fault site is acceptable (they degrade to raw-trace orientation).

- [ ] **Step 2: If any bug in the recon-bound set (`439494108`, `439645304`, `440058794`) shows `NO FAULT SITE`**, inspect its `crash_output` and widen the frame/marker handling in `orientation.py`, then re-run Tasks 2-5's tests. Otherwise proceed.

- [ ] **Step 3: (No commit unless Step 2 changed code.)**

---

## Task 9: Live A/B smoke — confirm orientation reaches the agent

**Files:** none (operational verification; requires the SSH tunnel up and Docker, per `arvo-eval/README.md`).

- [ ] **Step 1: Clear the target bug's ledger entry so the loop will run it**

`439494108` may have no ledger entry (safe to run). If it does, remove its line from `results/learn/ledger.jsonl` first.

- [ ] **Step 2: Launch a single-bug run with orientation ON**

Run from `arvo-eval/` (adds `OSS_CRS_ORIENT=1` to the standard command):

```bash
systemd-inhibit --what=idle:sleep --why="orientation A/B" \
  env ARVO_DB_PATH=arvo_new.db LEARN_PASS=treatment LEARN_MAX_ATTEMPTS=1 \
      OSS_CRS_CHECK_PATCH=1 OSS_CRS_RUN_TIMEOUT=7200 LLM_BACKEND=claude_cli \
      OSS_CRS_ORIENT=1 \
  python3 learn_loop.py --bugs 439494108
```

- [ ] **Step 2b: While it runs, confirm ORIENTATION.md landed in the agent's source dir**

Once `build-target` finishes and the agent starts, run:

```bash
find "$HOME/oss-crs" -name ORIENTATION.md -newermt '-20 min' 2>/dev/null -exec sed -n '1,12p' {} \;
```

Expected: an `ORIENTATION.md` printing the parsed `limb_addmul_1` / `mrb_bint_reduce` block.

- [ ] **Step 3: After the attempt, check turns-to-first-edit vs the pre-orientation baseline**

Reuse the tool-result-timestamp method: parse `agent/claude_stdout.log`, count assistant turns before the first `Edit`/`Write`. Baseline (no orientation, 2026-07-20): first edit ~mid-run on attempt 1, none on attempt 2. Success signal: the agent reaches its first edit in materially fewer turns, and ideally runs check-patch.

- [ ] **Step 4: Record the result** in a short dated note under `docs/` (e.g. `docs/2026-07-DD-orientation-ab.md`) — turns-to-first-edit with/without `OSS_CRS_ORIENT`, and whether the bug solved. This closes the Phase 1 rollout (spec §8) and gates whether to start Phase 2.

---

## Self-Review

- **Spec coverage:** §4.1 parser → Tasks 2-4; §4.2 render → Task 5; §4.3 wiring/gating/both-passes → Tasks 6-7; §4.4 degradation (empty/partial) → Task 4; §6 measurement → Task 9; §7 hermetic tests over real traces → Tasks 1-5; §8 rollout (30-trace validation + A/B) → Tasks 8-9. Phase 2 (§5) intentionally deferred to its own plan.
- **Placeholder scan:** none — every code step shows full code; every run step shows the command and expected output.
- **Type consistency:** `Frame(func,path,line)` and `Orientation(crash_class,summary_line,fault_site,call_chain,source_frame,raw_trace)` are defined once (Task 2) and used consistently; `parse_crash_output(crash_output,crash_type,project)`, `render_orientation(o)`, `HEURISTICS_POINTER`, and `inject_orientation(sanitizer,bug)` keep the same signatures across Tasks 2-7.
