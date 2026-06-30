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


def _triggers(playbook_text: str) -> list[str]:
    """The trigger headers a render emits: '## <trigger>  (tags)...' -> '<trigger>'."""
    out = []
    for line in playbook_text.splitlines():
        if line.startswith("## "):
            head = line[3:]
            idx = head.find("  (")          # render appends "  (tags)" after the trigger
            out.append((head[:idx] if idx != -1 else head).strip())
    return [t for t in out if t]


def maybe_compress(playbook_text: str, *, llm=call_llm) -> str:
    if len(playbook_text) <= MAX_PLAYBOOK_CHARS:
        return playbook_text
    compressed = llm(
        f"Compress this playbook to under {MAX_PLAYBOOK_CHARS} characters:\n\n{playbook_text}",
        system=SYSTEM,
    )
    # Fidelity guard: a compressor that silently drops a distinct lesson degrades
    # every future injection. If any trigger vanished, fall back to the full render
    # -- an oversized-but-complete playbook beats a lossy one.
    if any(t not in compressed for t in _triggers(playbook_text)):
        return playbook_text
    return compressed
