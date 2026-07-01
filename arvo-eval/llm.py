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
import time

MODEL = os.environ.get("LLM_MODEL", "claude-opus-4-8")
MAX_TOKENS = 1024
OAUTH_BETA = "oauth-2025-04-20"

# 429s and transient 5xx are worth retrying; a subscription credential shared with
# the repair agent gets rate-limited by back-to-back Opus bursts, and one blip must
# not kill a multi-hour run.
RETRIABLE_STATUS = {429, 500, 502, 503, 529}
MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "6"))


def _is_retriable(exc) -> bool:
    """Detect by status_code so this covers anthropic.APIStatusError subclasses
    (RateLimitError=429, InternalServerError=500, ...) and any error exposing one."""
    return getattr(exc, "status_code", None) in RETRIABLE_STATUS


def _backoff_seconds(exc, attempt: int) -> float:
    """Honor a Retry-After header when the server sends one, else exponential
    backoff (1s, 2s, 4s, ... capped at 60s)."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers is not None:
        try:
            return float(headers.get("retry-after"))
        except (TypeError, ValueError):
            pass
    return min(60.0, 2.0 ** attempt)


def with_retries(fn, *, max_retries=MAX_RETRIES, is_retriable=_is_retriable,
                 backoff=_backoff_seconds, sleep=time.sleep):
    """Call `fn()`, retrying transient failures with backoff. Re-raises the last
    error once retries are exhausted or the error is not retriable."""
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if attempt >= max_retries or not is_retriable(exc):
                raise
            sleep(backoff(exc, attempt))
            attempt += 1


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


def call_llm(prompt: str, *, system: str = "", client=None, max_tokens: int = MAX_TOKENS,
             sleep=time.sleep) -> str:
    client = client or _default_client()

    def _once():
        resp = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in resp.content).strip()

    return with_retries(_once, sleep=sleep)
