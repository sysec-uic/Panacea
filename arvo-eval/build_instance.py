"""Convert ARVO bug records into mini-SWE-agent instance dicts.

Reads the bug localIds to process from bug_ids.txt, fetches each record
from arvo.db, and converts it into the instance format mini-SWE-agent expects.
"""

import os
import sqlite3
from pathlib import Path

ARVO_DB_PATH = Path(os.environ.get("ARVO_DB_PATH", Path(__file__).parent / "arvo.db"))
BUG_IDS_PATH = Path(__file__).parent / "bug_ids.txt"


def load_bug_ids(path: Path = BUG_IDS_PATH) -> list[int]:
    """Read the list of bug localIds to process, one per line."""
    return [int(line.strip()) for line in path.read_text().splitlines() if line.strip()]


def load_bug(local_id: int) -> dict:
    """Fetch one bug record from arvo.db as a plain dict."""
    con = sqlite3.connect(ARVO_DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM arvo WHERE localId = ?", (local_id,))
    row = cur.fetchone()
    con.close()
    return dict(row)


def build_problem_statement(bug: dict) -> str:
    """Turn ARVO crash metadata into a bug-report-style description for the agent."""
    return f"""\
The `{bug['fuzz_target']}` fuzz target in the `{bug['project']}` project ({bug['repo_addr']}) \
crashes with the following issue, detected by {bug['fuzz_engine']} + {bug['sanitizer']}:

Crash type: {bug['crash_type']}
Severity: {bug['severity']}

Crash output:
{bug['crash_output']}

Please investigate the codebase, locate the root cause of this crash, and fix it so the \
program no longer crashes on the input that triggers this issue. Make the minimal change \
necessary to fix the underlying bug.

Environment:
- The source code is already checked out at `/src/{bug['project']}` and is fully \
configured/built. Do not re-clone, re-run autogen/configure/cmake, or change build flags \
from scratch.
- After editing source, rebuild with `compile` (run from `/src/{bug['project']}`). This \
re-runs the project's OSS-Fuzz build script with the correct sanitizer flags already set \
up in the environment, and produces `/out/{bug['fuzz_target']}`.
- The crashing input is at `/tmp/poc`. Reproduce the crash by running `arvo` (or directly: \
`/out/{bug['fuzz_target']} /tmp/poc`).
"""


def build_instance(bug: dict) -> dict:
    """Convert an ARVO bug record into a mini-SWE-agent instance dict."""
    return {
        # --- fields the runner actually uses ---
        "instance_id": str(bug["localId"]),
        "image_name": f"n132/arvo:{bug['localId']}-vul",
        "problem_statement": build_problem_statement(bug),
        # --- extra fields we carry along for scoring later ---
        "fix_commit": bug["fix_commit"],
        "patch_url": bug["patch_url"],
        "repo_addr": bug["repo_addr"],
        "project": bug["project"],
        "crash_type": bug["crash_type"],
    }


if __name__ == "__main__":
    for bug_id in load_bug_ids():
        bug = load_bug(bug_id)
        instance = build_instance(bug)

        print(f"=== Instance {instance['instance_id']} ({instance['project']}) ===")
        print(f"Image: {instance['image_name']}")
        print(f"Crash type: {instance['crash_type']}")
        print()
