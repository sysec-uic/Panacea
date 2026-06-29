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
