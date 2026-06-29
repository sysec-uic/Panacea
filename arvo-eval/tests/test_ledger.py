from ledger import append_record, read_records


def test_append_and_read(tmp_path):
    p = tmp_path / "ledger.jsonl"
    append_record(p, {"bug_id": 439494108, "pass": "treatment", "classification": "verified_correct"})
    append_record(p, {"bug_id": 440058794, "pass": "treatment", "classification": "still_crashes"})
    recs = read_records(p)
    assert len(recs) == 2
    assert recs[0]["bug_id"] == 439494108
    assert recs[1]["classification"] == "still_crashes"


def test_read_missing_file_returns_empty(tmp_path):
    assert read_records(tmp_path / "nope.jsonl") == []
