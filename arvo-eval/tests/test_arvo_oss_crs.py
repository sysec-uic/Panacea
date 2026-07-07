"""Compose-file selection: local default, OSS_CRS_COMPOSE_FILE override."""
from pathlib import Path

import arvo_oss_crs


def test_compose_file_defaults_to_local(monkeypatch):
    monkeypatch.delenv("OSS_CRS_COMPOSE_FILE", raising=False)
    assert arvo_oss_crs._compose_file() == (
        arvo_oss_crs.OSS_CRS_DIR / "example/crs-claude-code/compose-local.yaml")


def test_compose_file_env_override(monkeypatch):
    monkeypatch.setenv("OSS_CRS_COMPOSE_FILE", "/tmp/other-compose.yaml")
    assert arvo_oss_crs._compose_file() == Path("/tmp/other-compose.yaml")
