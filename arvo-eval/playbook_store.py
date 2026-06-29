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
