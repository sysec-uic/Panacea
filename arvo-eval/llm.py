"""Thin Anthropic-style wrapper. `client` is injectable for tests.

Credentials (first match wins) — a Claude OAuth token works, so no separate API
key is required:
  ANTHROPIC_API_KEY      - standard API key (x-api-key), if you have one
  CLAUDE_CODE_OAUTH_TOKEN- the same Claude Pro/Max OAuth token the repair agent
                           uses; sent as a Bearer token + the oauth beta header,
                           exactly how Claude Code authenticates to /v1/messages
  ANTHROPIC_AUTH_TOKEN   - generic bearer-token env var (e.g. from `ant auth`)

Do NOT set ANTHROPIC_API_KEY *and* an OAuth token at once — the API rejects dual
auth. This module sends only one.

Backend is also env-configurable for a future local model:
  LLM_MODEL    - model id           (default: claude-opus-4-8)
  LLM_BASE_URL - override endpoint   (e.g. a local Anthropic-compatible server)

A local server that speaks the OpenAI API instead needs a small adapter client
with the same `.messages.create(...)` shape — pass it via the `client=` arg.
"""
import os

MODEL = os.environ.get("LLM_MODEL", "claude-opus-4-8")
MAX_TOKENS = 1024
OAUTH_BETA = "oauth-2025-04-20"


def _client_args() -> dict:
    """Build kwargs for anthropic.Anthropic(...) from available credentials.

    Pure (reads env only) so it's testable without constructing a real client.
    """
    args: dict = {}
    base_url = os.environ.get("LLM_BASE_URL")
    if base_url:
        args["base_url"] = base_url

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if api_key:
        args["api_key"] = api_key
    elif oauth:
        # Bearer auth + the oauth beta header — required for /v1/messages.
        args["auth_token"] = oauth
        args["default_headers"] = {"anthropic-beta": OAUTH_BETA}
    else:
        raise RuntimeError(
            "No Anthropic credentials found. Set ANTHROPIC_API_KEY, or "
            "CLAUDE_CODE_OAUTH_TOKEN to reuse the repair agent's Claude OAuth token."
        )
    return args


def _default_client():
    import anthropic
    return anthropic.Anthropic(**_client_args())


def call_llm(prompt: str, *, system: str = "", client=None, max_tokens: int = MAX_TOKENS) -> str:
    client = client or _default_client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in resp.content).strip()
