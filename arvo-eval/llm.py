"""Thin Anthropic-style wrapper. `client` is injectable for tests.

Backend is env-configurable so the same code can target the hosted model now and a
local model later:
  LLM_MODEL     - model id            (default: claude-opus-4-8)
  LLM_BASE_URL  - override endpoint    (e.g. a local Anthropic-compatible server)
  ANTHROPIC_API_KEY - key (use any placeholder for keyless local servers)

A local server that speaks the OpenAI API instead of Anthropic's needs a small
adapter client with the same `.messages.create(...)` shape — drop it in via the
`client=` arg; nothing else changes.
"""
import os

MODEL = os.environ.get("LLM_MODEL", "claude-opus-4-8")
MAX_TOKENS = 1024


def _default_client():
    import anthropic
    kwargs = {"api_key": os.environ.get("ANTHROPIC_API_KEY", "")}
    base_url = os.environ.get("LLM_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    return anthropic.Anthropic(**kwargs)


def call_llm(prompt: str, *, system: str = "", client=None, max_tokens: int = MAX_TOKENS) -> str:
    client = client or _default_client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in resp.content).strip()
