import sqlite3
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


@pytest.mark.skipif(not DB.exists(), reason="arvo_new.db not present")
def test_processing_order_is_strictly_increasing_and_unique():
    # The holdout filter keys on `added_after_bug < before_bug` and the loop
    # processes in this order. Strict monotonicity + uniqueness is what makes
    # that strict-less-than correspond to "every earlier bug, no ties, no self".
    ids = mruby_bug_ids(DB)
    assert len(ids) == len(set(ids))
    assert all(a < b for a, b in zip(ids, ids[1:]))


@pytest.mark.skipif(not DB.exists(), reason="arvo_new.db not present")
def test_localid_is_the_oss_fuzz_issue_id():
    # localId order == disclosure order holds *by construction* only because
    # localId IS the OSS-Fuzz issue id (sequentially assigned). Assert that link
    # rather than a calendar date the db does not carry.
    con = sqlite3.connect(str(DB))
    try:
        rows = con.execute(
            "SELECT localId, report FROM arvo WHERE project = 'mruby'"
        ).fetchall()
    finally:
        con.close()
    for local_id, report in rows:
        assert report.rstrip("/").rsplit("/", 1)[-1] == str(local_id)
