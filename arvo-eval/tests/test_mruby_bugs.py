from pathlib import Path
import pytest
from mruby_bugs import mruby_bug_ids

DB = Path(__file__).resolve().parents[1] / "arvo_new.db"


@pytest.mark.skipif(not DB.exists(), reason="arvo_new.db not present")
def test_returns_sorted_mruby_ids():
    ids = mruby_bug_ids(DB)
    assert len(ids) == 30
    assert ids == sorted(ids)          # chronological by localId
    assert ids[0] == 439237851         # earliest mruby bug
