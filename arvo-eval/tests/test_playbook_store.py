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
