from curator import maybe_compress, MAX_PLAYBOOK_CHARS


def test_under_cap_is_returned_unchanged():
    text = "short playbook"
    assert maybe_compress(text, llm=lambda p, system="": "SHOULD NOT BE CALLED") == text


def test_over_cap_is_compressed_via_llm():
    big = "x" * (MAX_PLAYBOOK_CHARS + 10)
    out = maybe_compress(big, llm=lambda p, system="": "compressed digest")
    assert out == "compressed digest"
