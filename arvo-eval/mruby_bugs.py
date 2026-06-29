"""List mruby bug localIds in chronological (localId) order from the ARVO db."""
import sqlite3
from pathlib import Path


def mruby_bug_ids(db_path: Path) -> list[int]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT localId FROM arvo WHERE project = 'mruby' ORDER BY localId"
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]
