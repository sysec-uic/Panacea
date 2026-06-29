from pathlib import Path

from differential_oracle import default_probes


def test_default_probes_finds_committed_scripts():
    probes = default_probes()
    names = [p.name for p in probes]
    assert "01_integer_arith.rb" in names
    assert all(p.suffix == ".rb" for p in probes)
    assert names == sorted(names)               # deterministic order
    assert all(Path(p).read_text().strip() for p in probes)  # non-empty
