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
access, so using it against /v1/messages is rate-limited (429) far more
aggressively than an API key — see the claude_cli backend below, which is the
way to run this on a bare subscription.

Backend selection (LLM_BACKEND, else auto):
  api         - the raw Anthropic API via the anthropic SDK (needs an API key,
                OAuth token, or LLM_BASE_URL). Default when an API key or a
                custom LLM_BASE_URL is set.
  openai      - an OpenAI-compatible server (llama.cpp behind the SSH tunnel, or
                the OSS-CRS LiteLLM proxy) via the openai SDK. Auto-selected when
                OPENAI_BASE_URL is set (and no Anthropic API key / endpoint); set
                LLM_BACKEND=openai to force it. This is the same local-model
                setup the repair agent uses via litellm-config-local.yaml.
  claude_cli  - drive the Claude Code CLI (`claude -p`) instead of the API, so a
                Pro/Max SUBSCRIPTION runs the extractor with NO API key, on the
                same sanctioned path as the OSS-CRS repair agent. Auto-selected
                when no API key and no LLM_BASE_URL are set and `claude` is on PATH.

  LLM_MODEL       - model id          (default: claude-opus-4-8; llama.cpp ignores
                    it, and the LiteLLM configs alias the Claude ids to the local
                    model, so the default works everywhere)
  LLM_BASE_URL    - Anthropic-compatible endpoint override (api backend)
  OPENAI_BASE_URL - OpenAI-compatible endpoint (openai backend;
                    default http://localhost:8080/v1 — the llama.cpp SSH tunnel)
  OPENAI_API_KEY  - key for the openai backend (default sk-local-dummy; local
                    llama.cpp ignores it, matching litellm-config-local.yaml)

Any other server shape needs a small adapter client with the same
`.messages.create(...)` contract — pass it via the `client=` arg (ClaudeCLIClient
and OpenAIClient are exactly such adapters). See the NETWORK LLM CALL SITE
marker in call_llm() for the exact seam to swap.
"""
import json
import os
import shutil
import subprocess
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
    """True if we can make a real model call some way -- so callers (e.g. demos) can
    choose real-vs-stub the SAME way `_default_client` picks a backend, without
    constructing a client or catching its RuntimeError.

    Counts an API key, an OAuth token, the Claude Code CLI backend (its own login),
    OR a local OpenAI-compatible server (needs no real key), matching `_select_backend`.
    """
    if _select_backend() in ("claude_cli", "openai"):
        return True
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


class _CLIBlock:
    """Mimics an anthropic content block: exposes `.text`."""
    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text


class _CLIResponse:
    """Mimics an anthropic Message: exposes `.content` -> [block with .text]."""

    def __init__(self, text):
        self.content = [_CLIBlock(text)]


class ClaudeCLIClient:
    """Runs the extractor through the Claude Code CLI (`claude -p`) instead of the raw
    API, so a Pro/Max SUBSCRIPTION can drive it with no API key -- the same sanctioned
    path the OSS-CRS repair agent uses, and not rate-limited the way subscription OAuth
    tokens are on /v1/messages.

    Exposes only the sliver of the anthropic client that `call_llm` touches:
    `client.messages.create(...)` returning an object with `.content[i].text`.
    """

    def __init__(self, *, timeout=600, cli="claude"):
        self.timeout = timeout
        self.cli = cli
        self.messages = self          # so `client.messages.create(...)` resolves here

    def create(self, *, model, max_tokens, system, messages):
        # Our usage is always a single user turn; join any user content defensively.
        prompt = "\n\n".join(m["content"] for m in messages if m.get("role") == "user")
        # --system-prompt REPLACES Claude Code's default agent prompt so the extractor
        # follows exactly our instructions (clean JSON, no tool use). Prompt via stdin
        # to sidestep argv length/escaping limits on large diffs.
        cmd = [self.cli, "-p", "--output-format", "json", "--model", model]
        if system:
            cmd += ["--system-prompt", system]
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=self.timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI exited {proc.returncode}: "
                               f"{(proc.stderr or proc.stdout).strip()[:500]}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"claude CLI returned non-JSON: {proc.stdout[:500]}") from exc
        if data.get("is_error"):
            raise RuntimeError(f"claude CLI reported an error: "
                               f"{data.get('result') or data.get('api_error_status')}")
        return _CLIResponse(data.get("result", ""))


DEFAULT_OPENAI_BASE_URL = "http://localhost:8080/v1"   # llama.cpp SSH tunnel
DEFAULT_OPENAI_API_KEY = "sk-local-dummy"               # local servers ignore it


def _openai_args() -> dict:
    """base_url/api_key for the OpenAI-compatible backend. Defaults target the
    llama.cpp SSH tunnel on the host — the same server litellm-config-local.yaml
    routes the repair agent to (from containers it's 172.17.0.1 instead)."""
    return {
        "base_url": os.environ.get("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL),
        "api_key": os.environ.get("OPENAI_API_KEY", DEFAULT_OPENAI_API_KEY),
    }


class OpenAIClient:
    """Adapter putting an OpenAI-compatible /v1/chat/completions server behind the
    anthropic `.messages.create(...)` shape call_llm expects. Covers llama.cpp
    (direct or via the SSH tunnel) and the OSS-CRS LiteLLM proxy alike.

    The wrapped SDK client is injectable for tests.
    """

    def __init__(self, *, client=None):
        if client is None:
            import openai
            client = openai.OpenAI(**_openai_args())
        self._client = client
        self.messages = self          # so `client.messages.create(...)` resolves here

    def create(self, *, model, max_tokens, system, messages):
        chat_messages = ([{"role": "system", "content": system}] if system else []) + messages
        resp = self._client.chat.completions.create(
            model=model, max_tokens=max_tokens, messages=chat_messages)
        return _CLIResponse(resp.choices[0].message.content or "")


def _select_backend() -> str:
    """'api', 'openai', or 'claude_cli'. Explicit LLM_BACKEND wins. An Anthropic API
    key or endpoint keeps the raw-API path. Otherwise prefer a configured local
    OpenAI-compatible server (OPENAI_BASE_URL), then the Claude Code CLI: the raw-API
    path can't work on a bare subscription (OAuth tokens 429 on /v1/messages), but the
    CLI authenticates via its own login and runs on the subscription like the agent."""
    explicit = os.environ.get("LLM_BACKEND")
    if explicit:
        return explicit
    if (not os.environ.get("ANTHROPIC_API_KEY")
            and not os.environ.get("LLM_BASE_URL")):
        if os.environ.get("OPENAI_BASE_URL"):
            return "openai"
        if shutil.which("claude"):
            return "claude_cli"
    return "api"


def _default_client():
    backend = _select_backend()
    if backend == "claude_cli":
        return ClaudeCLIClient()
    if backend == "openai":
        return OpenAIClient()
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
