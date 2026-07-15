"""Run mini-SWE-agent on every bug in bug_ids.txt, one subprocess per bug.

Each bug is run via `run_single.py` (with MSWEA_BUG_ID set), so each gets a fresh
Python process / Docker container. After each run, reads back results/<id>/trajectory.json
to report exit status, cost, and call count, then prints a summary table at the end.

Bugs that already have a results/<id>/trajectory.json are skipped (resume support).
Pass --force to re-run everything.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from build_instance import load_bug_ids

RESULTS_DIR = Path("results")


def main() -> None:
    force = "--force" in sys.argv

    bug_ids = list(dict.fromkeys(load_bug_ids()))  # dedupe, preserve order
    summary = []

    for bug_id in bug_ids:
        traj_path = RESULTS_DIR / str(bug_id) / "trajectory.json"
        if traj_path.exists() and not force:
            print(f"=== Skipping {bug_id} (results already exist) ===")
        else:
            print(f"=== Running {bug_id} ===")
            env = {**os.environ, "MSWEA_BUG_ID": str(bug_id)}
            subprocess.run([sys.executable, "run_single.py"], env=env, check=False)

        if traj_path.exists():
            info = json.loads(traj_path.read_text())["info"]
            summary.append({
                "bug_id": bug_id,
                "exit_status": info["exit_status"],
                "cost": info["model_stats"]["instance_cost"],
                "api_calls": info["model_stats"]["api_calls"],
            })
        else:
            summary.append({"bug_id": bug_id, "exit_status": "NO_RESULT", "cost": None, "api_calls": None})

    print("\n=== Batch summary ===")
    print(f"{'bug_id':>10}  {'exit_status':<20} {'cost':>8} {'calls':>6}")
    for row in summary:
        cost_str = f"{row['cost']:.4f}" if row["cost"] is not None else "-"
        calls_str = str(row["api_calls"]) if row["api_calls"] is not None else "-"
        print(f"{row['bug_id']:>10}  {row['exit_status']:<20} {cost_str:>8} {calls_str:>6}")

    total_cost = sum(row["cost"] for row in summary if row["cost"] is not None)
    print(f"\nTotal cost: ${total_cost:.4f}")


if __name__ == "__main__":
    main()
