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
    """A Frame iff the path is inside the project's own source tree (excluding
    the OSS-Fuzz fuzzing harness itself, which lives under the project prefix
    but isn't app code)."""
    if not path.startswith(prefix):
        return None
    rel = path[len(prefix):]
    if rel.startswith("oss-fuzz/"):
        return None
    return Frame(func=func, path=rel, line=int(line))


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
