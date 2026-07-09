"""Compose-file selection and local-model preflight reachability check."""
from pathlib import Path

import pytest

import arvo_oss_crs


def test_compose_file_defaults_to_local(monkeypatch):
    monkeypatch.delenv("OSS_CRS_COMPOSE_FILE", raising=False)
    assert arvo_oss_crs._compose_file() == (
        arvo_oss_crs.OSS_CRS_DIR / "example/crs-claude-code/compose-local.yaml")


def test_compose_file_env_override(monkeypatch):
    monkeypatch.setenv("OSS_CRS_COMPOSE_FILE", "/tmp/other-compose.yaml")
    assert arvo_oss_crs._compose_file() == Path("/tmp/other-compose.yaml")


def test_uses_local_model_true_for_local_compose():
    assert arvo_oss_crs._uses_local_model(
        Path("/x/example/crs-claude-code/compose-local.yaml")) is True


def test_uses_local_model_false_for_oauth_compose():
    assert arvo_oss_crs._uses_local_model(
        Path("/x/example/crs-claude-code/compose-oauth.yaml")) is False


def test_reachability_passes_when_endpoint_up():
    calls = []
    # A stub opener that "succeeds" records the call and returns without raising.
    arvo_oss_crs.check_local_model_reachable(
        "http://172.17.0.1:8080/v1/models",
        opener=lambda url, timeout: calls.append((url, timeout)))
    assert calls == [("http://172.17.0.1:8080/v1/models", 4.0)]


def test_reachability_raises_actionable_error_when_endpoint_down():
    def dead(url, timeout):
        raise OSError("Connection refused")

    with pytest.raises(RuntimeError) as exc:
        arvo_oss_crs.check_local_model_reachable(
            "http://172.17.0.1:8080/v1/models", opener=dead)
    msg = str(exc.value)
    assert "unreachable" in msg
    assert "172.17.0.1:8080:localhost:8080" in msg   # tells the user how to fix it
