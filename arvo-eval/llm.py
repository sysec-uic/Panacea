"""Thin Anthropic-style wrapper. `client` is injectable for tests.

Credentials — the API key is PREFERRED; the OAuth token is the FALLBACK. You may
safely export BOTH at once (e.g. an API key to bill this extractor to pay-per-use
plus the repair agent's OAuth subscription token): this module picks the API key
and sends only ONE credential per request, so there is no dual-auth rejection.
  ANTHROPIC_API_KEY      - standard API key (x-api-key). Used first when present.
  CLAUDE_CODE_OAUTH_TOKEN- the Claude Pro/Max OAuth token the repair agent uses;
                           sent as a Bearer token + the oauth beta header, exactly
                           how Claude Code authenticates to /v1/messages. Used only
                           when no API key is set.
  ANTHROPIC_AUTH_TOKEN   - generic bearer-token env var (e.g. from `ant auth`)

Note: a subscription OAuth token is provisioned for Claude Code, not raw API
access, so using it here is rate-limited far more aggressively than an API key —
prefer ANTHROPIC_API_KEY for unattended multi-hour runs.

Backend is also env-configurable for a future local model:
  LLM_MODEL    - model id           (default: claude-opus-4-8)
  LLM_BASE_URL - override endpoint   (e.g. a local Anthropic-compatible server)

A local server that speaks the OpenAI API instead needs a small adapter client
with the same `.messages.create(...)` shape — pass it via the `client=` arg.
See the NETWORK LLM CALL SITE marker in call_llm() for the exact seam to swap.
"""
import os
import sys
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


def _rl_summary(exc) -> str:
    """Human-readable dump of the rate-limit headers on a 429 so we can tell a
    transient per-minute throttle (recoverable) from a hard daily/org cap (not)."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or {}
    interesting = ("retry-after", "anthropic-ratelimit-unified-reset",
                   "anthropic-ratelimit-unified-status",
                   "anthropic-ratelimit-requests-remaining",
                   "anthropic-ratelimit-tokens-remaining")
    parts = [f"{k}={headers.get(k)}" for k in interesting if headers.get(k) is not None]
    return ", ".join(parts) or "(no rate-limit headers on response)"


def _oauth_only() -> bool:
    """Using a subscription OAuth token and NO API key -- the combination that gets
    throttled hard on raw /v1/messages regardless of interactive Claude Code headroom."""
    return not os.environ.get("ANTHROPIC_API_KEY") and bool(
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


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
            if getattr(exc, "status_code", None) == 429:
                wait = backoff(exc, attempt)
                print(f"[llm] 429 rate-limited (attempt {attempt + 1}/{max_retries + 1}); "
                      f"retrying in {wait:.0f}s. {_rl_summary(exc)}", file=sys.stderr)
                sleep(wait)
            else:
                sleep(backoff(exc, attempt))
            attempt += 1


def have_credentials() -> bool:
    """True if any usable Anthropic credential is in the environment.

    Mirrors the detection in `_client_args` so callers (e.g. demos) can choose
    real-vs-stub the SAME way the client is built -- an OAuth token counts, not
    just an API key -- without constructing a client or catching its RuntimeError.
    """
    return bool(os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


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
        # ============================ NETWORK LLM CALL SITE ============================
        # This is the ONE place this module hits a model over the network. To run the
        # heuristic loop against a LOCAL LLM, either:
        #   * point LLM_BASE_URL at a local Anthropic-compatible server (no code change), or
        #   * pass a `client=` object exposing this same `.messages.create(...)` shape
        #     (e.g. a thin adapter over a local OpenAI-style server).
        # Keep the request/response contract below stable and swaps stay drop-in.
        # ==============================================================================
        resp = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in resp.content).strip()

    try:
        return with_retries(_once, sleep=sleep)
    except Exception as exc:
        # A 429 that survives every retry on an OAuth-only credential is almost never
        # a transient blip: subscription tokens are throttled hard on raw /v1/messages.
        # Turn the opaque anthropic.RateLimitError into an actionable instruction.
        if getattr(exc, "status_code", None) == 429 and _oauth_only():
            raise RuntimeError(
                "Anthropic returned 429 for the heuristic extractor after exhausting "
                f"retries ({_rl_summary(exc)}). You're authenticating with a Claude "
                "subscription OAuth token (CLAUDE_CODE_OAUTH_TOKEN) and no API key; "
                "subscription tokens are rate-limited hard on raw /v1/messages calls, "
                "independent of your interactive Claude Code headroom. To fix: set "
                "ANTHROPIC_API_KEY (billed to the API, keeps the OAuth token for the "
                "repair agent), or point LLM_BASE_URL at a local Anthropic-compatible "
                "model to run the extractor locally."
            ) from exc
        raise
