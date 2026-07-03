import json
import pytest
from llm import (call_llm, with_retries, _client_args, OAUTH_BETA,
                 _select_backend, ClaudeCLIClient, have_credentials)


class Boom(Exception):
    """Stand-in for anthropic.APIStatusError: carries a status_code."""
    def __init__(self, status_code=429):
        super().__init__("boom")
        self.status_code = status_code


def test_with_retries_retries_then_succeeds():
    calls = {"n": 0}
    slept = []

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Boom(429)
        return "ok"

    out = with_retries(fn, sleep=slept.append, backoff=lambda exc, attempt: attempt)
    assert out == "ok"
    assert calls["n"] == 3
    assert slept == [0, 1]  # backed off before the 2nd and 3rd tries


def test_with_retries_gives_up_after_max_retries():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise Boom(429)

    with pytest.raises(Boom):
        with_retries(fn, max_retries=2, sleep=lambda s: None, backoff=lambda exc, a: 0)
    assert calls["n"] == 3  # initial attempt + 2 retries


def test_with_retries_does_not_retry_non_retriable():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise Boom(400)  # client error, not transient

    with pytest.raises(Boom):
        with_retries(fn, sleep=lambda s: None)
    assert calls["n"] == 1  # no retries


def test_call_llm_retries_on_rate_limit():
    state = {"n": 0}

    class FlakyMessages:
        def create(self, **kw):
            state["n"] += 1
            if state["n"] == 1:
                raise Boom(429)
            return type("R", (), {"content": [type("B", (), {"text": "recovered"})()]})()

    class FlakyClient:
        messages = FlakyMessages()

    out = call_llm("hi", client=FlakyClient(), sleep=lambda s: None)
    assert out == "recovered"
    assert state["n"] == 2


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


# --- backend selection & Claude Code CLI adapter -----------------------------

def _clear_backend(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.delenv("LLM_BACKEND", raising=False)


def test_backend_explicit_env_wins(monkeypatch):
    _clear_backend(monkeypatch)
    monkeypatch.setenv("LLM_BACKEND", "api")
    monkeypatch.setattr("llm.shutil.which", lambda _: "/usr/bin/claude")
    assert _select_backend() == "api"


def test_backend_prefers_api_when_key_present(monkeypatch):
    _clear_backend(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr("llm.shutil.which", lambda _: "/usr/bin/claude")
    assert _select_backend() == "api"


def test_backend_falls_back_to_cli_on_subscription(monkeypatch):
    # No API key, no custom endpoint, but the claude CLI is installed.
    _clear_backend(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-xyz")
    monkeypatch.setattr("llm.shutil.which", lambda _: "/usr/bin/claude")
    assert _select_backend() == "claude_cli"
    assert have_credentials() is True


def test_backend_stays_api_when_cli_absent(monkeypatch):
    _clear_backend(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-xyz")
    monkeypatch.setattr("llm.shutil.which", lambda _: None)
    assert _select_backend() == "api"


def test_cli_client_parses_result(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = json.dumps({"is_error": False, "result": '{"root":"uaf"}'})
        stderr = ""

    seen = {}

    def fake_run(cmd, input, capture_output, text, timeout):
        seen["cmd"] = cmd
        seen["input"] = input
        return _Proc()

    monkeypatch.setattr("llm.subprocess.run", fake_run)
    client = ClaudeCLIClient()
    resp = client.messages.create(model="claude-opus-4-8", max_tokens=1024,
                                  system="be terse", messages=[{"role": "user", "content": "hi"}])
    assert "".join(b.text for b in resp.content) == '{"root":"uaf"}'
    assert seen["input"] == "hi"                       # prompt goes via stdin
    assert "--system-prompt" in seen["cmd"] and "be terse" in seen["cmd"]
    assert "--model" in seen["cmd"] and "claude-opus-4-8" in seen["cmd"]


def test_cli_client_raises_on_error_payload(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = json.dumps({"is_error": True, "result": "rate limited"})
        stderr = ""

    monkeypatch.setattr("llm.subprocess.run", lambda *a, **k: _Proc())
    with pytest.raises(RuntimeError, match="claude CLI reported an error"):
        ClaudeCLIClient().messages.create(model="m", max_tokens=1, system="",
                                          messages=[{"role": "user", "content": "x"}])


def test_cli_client_raises_on_nonzero_exit(monkeypatch):
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "not logged in"

    monkeypatch.setattr("llm.subprocess.run", lambda *a, **k: _Proc())
    with pytest.raises(RuntimeError, match="claude CLI exited 1"):
        ClaudeCLIClient().messages.create(model="m", max_tokens=1, system="",
                                          messages=[{"role": "user", "content": "x"}])
