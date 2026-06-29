import json
from pathlib import Path

from playbook_store import (
    new_state, add_heuristic, active_heuristics, render_playbook,
    save_state, load_state,
)


def make_h(text="lesson", tags=("suar",)):
    return {"trigger": "SUAR in pool", "root_cause_lesson": text,
            "how_to_apply": "deep-copy on escape", "tags": list(tags), "confidence": "high"}


def make_contrastive():
    return {"trigger": "OOB read over bytecode", "wrong_approach": "guarded the reader",
            "correct_approach": "fixed the writer", "lesson": "corrupt bytecode, not a missing check",
            "how_to_apply": "trace the emission", "tags": ["asan"], "confidence": "high",
            "kind": "contrastive"}


def test_render_handles_contrastive_lessons():
    s = new_state()
    s = add_heuristic(s, make_contrastive(), source_bug=449429295, after_bug=449429295)
    md = render_playbook(s, before_bug=999999999)
    assert "**Don't:** guarded the reader" in md
    assert "**Do:** fixed the writer" in md
    assert "corrupt bytecode, not a missing check" in md


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
    assert active_heuristics(s, before_bug=439494108) == []
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
