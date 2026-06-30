from curator import maybe_compress, MAX_PLAYBOOK_CHARS


def _over_cap_playbook(*triggers):
    """A rendered-style playbook with the given trigger headers, padded over cap."""
    body = "\n".join(f"## {t}  (suar)\n- **Lesson:** L\n- **How to apply:** A\n" for t in triggers)
    return body + "\n" + "x" * (MAX_PLAYBOOK_CHARS + 10)


def test_under_cap_is_returned_unchanged():
    text = "short playbook"
    assert maybe_compress(text, llm=lambda p, system="": "SHOULD NOT BE CALLED") == text


def test_over_cap_is_compressed_via_llm():
    big = "x" * (MAX_PLAYBOOK_CHARS + 10)
    out = maybe_compress(big, llm=lambda p, system="": "compressed digest")
    assert out == "compressed digest"


def test_compressor_dropping_a_trigger_falls_back_to_full_render():
    text = _over_cap_playbook("SUAR in pool", "khash rebuild GC-UAF")
    # Compressor keeps only the first lesson -- the second trigger vanishes.
    out = maybe_compress(text, llm=lambda p, system="": "## SUAR in pool\n- L\n- A\n")
    assert out == text  # fidelity guard: full render preferred over a lossy digest


def test_compressor_preserving_all_triggers_returns_compressed():
    text = _over_cap_playbook("SUAR in pool", "khash rebuild GC-UAF")
    digest = "## SUAR in pool: L\n## khash rebuild GC-UAF: L\n"
    out = maybe_compress(text, llm=lambda p, system="": digest)
    assert out == digest
