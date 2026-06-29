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
