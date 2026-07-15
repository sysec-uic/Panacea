"""Coarse crash-class taxonomy: the matched/mismatched key for transfer eligibility.

Raw ARVO `crash_type` is noisy (`UNKNOWN READ`, `Heap-buffer-overflow READ 1` vs
`READ 8`). Collapse it to a small, frozen family so cross-project matching is
deterministic and reviewable. See the cross-project-transfer experiment design.
"""

# Ordered: first matching prefix wins. Specific prefixes precede generic ones.
_PREFIXES = [
    ("use-of-uninitialized-value", "uninit"),
    ("heap-buffer-overflow", "heap-oob"),
    ("stack-buffer-overflow", "stack-oob"),
    ("stack-use-after-return", "stack-oob"),
    ("heap-use-after-free", "uaf"),
    ("use-after-free", "uaf"),
    ("null-dereference", "null-deref"),
    ("segv on unknown address", "null-deref"),
]


def crash_class(crash_type: str | None) -> str:
    t = (crash_type or "").strip().lower()
    for prefix, family in _PREFIXES:
        if t.startswith(prefix):
            return family
    return "other"
