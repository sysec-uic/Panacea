"""Compress the rendered playbook only when it grows past a size cap.

Compression is ephemeral (used for one injection); the structured store is the
source of truth and is never overwritten here.
"""
from llm import call_llm

MAX_PLAYBOOK_CHARS = 3000

SYSTEM = (
    "You compress a list of mruby repair heuristics into a shorter digest WITHOUT "
    "dropping any distinct lesson or merging unrelated bug classes. Preserve the "
    "trigger -> lesson -> how-to-apply structure. Output markdown only."
)


def maybe_compress(playbook_text: str, *, llm=call_llm) -> str:
    if len(playbook_text) <= MAX_PLAYBOOK_CHARS:
        return playbook_text
    return llm(
        f"Compress this playbook to under {MAX_PLAYBOOK_CHARS} characters:\n\n{playbook_text}",
        system=SYSTEM,
    )
