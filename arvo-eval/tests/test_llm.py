import pytest
from llm import call_llm, _client_args, OAUTH_BETA


class FakeMessages:
    def create(self, **kwargs):
        class R:
            content = [type("Block", (), {"text": "hello from stub"})()]
        return R()


class FakeClient:
    messages = FakeMessages()


def test_call_llm_returns_text_from_client():
    out = call_llm("say hi", system="be terse", client=FakeClient())
    assert out == "hello from stub"


def _clear(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def test_oauth_token_used_when_no_api_key(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-xyz")
    args = _client_args()
    assert args["auth_token"] == "sk-ant-oat01-xyz"
    assert args["default_headers"]["anthropic-beta"] == OAUTH_BETA
    assert "api_key" not in args  # never send both — the API rejects dual auth


def test_api_key_preferred_over_oauth(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-xyz")
    args = _client_args()
    assert args["api_key"] == "sk-test"
    assert "auth_token" not in args


def test_raises_without_any_credential(monkeypatch):
    _clear(monkeypatch)
    with pytest.raises(RuntimeError):
        _client_args()
