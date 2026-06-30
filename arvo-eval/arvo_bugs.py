"""Project-scoped, global-`localId`-ordered bug loader for the transfer experiment.

Generalizes `mruby_bugs.mruby_bug_ids`: global chronological order is `localId` order
across *all* projects (localId is the OSS-Fuzz issue id), optionally narrowed to a
project subset so a single run stays within one machine's image-pull/compute budget.
"""
import sqlite3


def scoped_bug_ids(db_path, projects=None) -> list[int]:
    con = sqlite3.connect(str(db_path))
    try:
        if projects:
            placeholders = ",".join("?" * len(projects))
            rows = con.execute(
                f"SELECT localId FROM arvo WHERE project IN ({placeholders}) ORDER BY localId",
                tuple(projects),
            ).fetchall()
        else:
            rows = con.execute("SELECT localId FROM arvo ORDER BY localId").fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]
