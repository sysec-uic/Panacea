import sqlite3

from arvo_bugs import scoped_bug_ids


def _db(tmp_path, rows):
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE arvo (localId INTEGER, project TEXT)")
    con.executemany("INSERT INTO arvo VALUES (?, ?)", rows)
    con.commit()
    con.close()
    return p


def test_global_localid_order_across_projects(tmp_path):
    db = _db(tmp_path, [(3, "php"), (1, "mruby"), (2, "vlc")])
    assert scoped_bug_ids(db) == [1, 2, 3]


def test_project_scoping_filters_and_keeps_order(tmp_path):
    db = _db(tmp_path, [(3, "php"), (1, "mruby"), (2, "vlc"), (4, "mruby")])
    assert scoped_bug_ids(db, projects=["mruby", "vlc"]) == [1, 2, 4]


def test_none_projects_returns_all(tmp_path):
    db = _db(tmp_path, [(2, "php"), (1, "mruby")])
    assert scoped_bug_ids(db, projects=None) == [1, 2]
