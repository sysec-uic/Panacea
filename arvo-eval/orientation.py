"""Parse an OSS-Fuzz sanitizer crash report into a compact orientation for the
repair agent. Pure logic (no I/O) so it can be unit-tested against real traces.

The crash report is a deployment-faithful signal: it is exactly what OSS-Fuzz
hands a real developer. This module only extracts what the report already states
(crash class, faulting frame, call chain, root-cause frame); it adds no knowledge
of the upstream fix.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# One stack frame line, e.g.:
#   #0 0x55e851c07304 in limb_addmul_1 /src/mruby/mrbgems/.../bigint.c:726:58
# The ":line[:col]" suffix is optional -- some frames (e.g. mrb_vm_exec in
# src/vm.c, seen in real MSan traces) are emitted without a line number at all.
_FRAME_RE = re.compile(
    r"#\d+ 0x[0-9a-f]+ in (?P<func>\S+) (?P<path>/\S+?)"
    r"(?::(?P<line>\d+)(?::\d+)?)?(?=\s|$)"
)
# The crash-class line: "ERROR: AddressSanitizer: stack-use-after-return ..." or
# "WARNING: MemorySanitizer: use-of-uninitialized-value".
_CLASS_RE = re.compile(
    r"(?:ERROR|WARNING): \w*Sanitizer: (?P<cls>[a-z][a-z0-9-]+)"
)

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


@dataclass
class Frame:
    func: str
    path: str   # repo-relative (leading /src/<project>/ stripped)
    line: int | None   # None when the sanitizer omitted a line number


@dataclass
class Orientation:
    crash_class: str | None
    summary_line: str | None
    fault_site: Frame | None
    call_chain: list[Frame]
    source_frame: Frame | None
    raw_trace: str


def _app_frame(func: str, path: str, line: str | None, prefix: str) -> Frame | None:
    """A Frame iff the path is inside the project's own source tree (excluding
    the OSS-Fuzz fuzzing harness itself, which lives under the project prefix
    but isn't app code). `line` may be None -- some sanitizer frames (e.g.
    mrb_vm_exec in src/vm.c) are emitted without one; keep the frame anyway."""
    if not path.startswith(prefix):
        return None
    rel = path[len(prefix):]
    if rel.startswith("oss-fuzz/"):
        return None
    return Frame(func=func, path=rel, line=int(line) if line is not None else None)


def _normalized_crash_type(crash_type: str) -> str | None:
    """Fallback crash_class when the trace text doesn't carry a lowercase class
    token (e.g. SEGV traces: 'ERROR: AddressSanitizer: SEGV on unknown address').
    The caller's own classification (crash_type) is deployment-faithful too --
    use it rather than surfacing crash_class=None."""
    norm = re.sub(r"\s+", "-", (crash_type or "").strip().lower())
    return norm or None


def parse_crash_output(crash_output: str, crash_type: str, project: str) -> Orientation | None:
    """Return an Orientation, or None if there is no usable crash text."""
    if not (crash_output or "").strip():
        return None
    prefix = f"/src/{project}/"

    m = _CLASS_RE.search(crash_output)
    crash_class = m.group("cls") if m else _normalized_crash_type(crash_type)

    summary_line = next(
        (ln.strip() for ln in crash_output.splitlines() if ln.strip().startswith("SUMMARY:")),
        None,
    )

    call_chain: list[Frame] = []
    for fm in _FRAME_RE.finditer(crash_output):
        fr = _app_frame(fm.group("func"), fm.group("path"), fm.group("line"), prefix)
        if fr is not None:
            call_chain.append(fr)

    fault_site = call_chain[0] if call_chain else None
    source_frame = _source_frame(crash_output, prefix)
    return Orientation(
        crash_class=crash_class,
        summary_line=summary_line,
        fault_site=fault_site,
        call_chain=call_chain,
        source_frame=source_frame,
        raw_trace=crash_output,
    )


HEURISTICS_POINTER = (
    "Read ORIENTATION.md first -- it has the parsed crash trace (class, fault "
    "site, call chain, root-cause frame). Do not re-derive it by grepping.\n\n"
)


def _fmt_frame(fr: Frame) -> str:
    if fr.line is None:
        return f"{fr.func}    {fr.path}"
    return f"{fr.func}    {fr.path}:{fr.line}"


# Cap on the pasted raw-trace section. This isn't about display -- it's a
# budget for the repair agent's context tokens (raw traces can run to many KB
# once you count duplicate ASan/MSan frame groups), so we keep it well under
# what a full trace can cost while still surfacing the parts an agent needs.
_TRACE_CAP = 3500
_TRUNCATION_MARKER = "\n... [trace truncated] ...\n"


def _trim_trace(raw: str, summary_line: str | None, source_frame: Frame | None) -> str:
    """Drop the libFuzzer preamble; keep from the first sanitizer line onward,
    capped at _TRACE_CAP chars. A plain head-slice can cut off the SUMMARY line
    and the resolved source-frame line, which live near the *end* of long
    traces (e.g. the MSan fixture) -- so if truncation would drop them, append
    them after a truncation marker instead of silently losing them."""
    lines = raw.splitlines()
    start = next(
        (i for i, ln in enumerate(lines)
         if "Sanitizer:" in ln or ln.strip().startswith(("#0", "==", "ERROR", "WARNING"))),
        0,
    )
    trimmed = "\n".join(lines[start:])
    if len(trimmed) <= _TRACE_CAP:
        return trimmed

    head = trimmed[:_TRACE_CAP]
    tail_parts = []
    if summary_line and summary_line not in head:
        tail_parts.append(summary_line)
    if source_frame is not None:
        src_line = _fmt_frame(source_frame)
        if src_line not in head:
            tail_parts.append(f"Source frame: {src_line}")
    if not tail_parts:
        return head
    return head + _TRUNCATION_MARKER + "\n".join(tail_parts)


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
    out.append(_trim_trace(o.raw_trace, o.summary_line, o.source_frame))
    out.append("```")
    return "\n".join(out) + "\n"
