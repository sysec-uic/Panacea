"""Thin Anthropic wrapper. `client` is injectable for tests."""
import os

MODEL = "claude-opus-4-8"
MAX_TOKENS = 1024


def _default_client():
    import anthropic
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def call_llm(prompt: str, *, system: str = "", client=None, max_tokens: int = MAX_TOKENS) -> str:
    client = client or _default_client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in resp.content).strip()
