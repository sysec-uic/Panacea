"""Run mini-SWE-agent on a single ARVO instance, end-to-end.

This is our pipeline-validation step: build one instance (reusing build_instance.py),
spin up its Docker environment, let the agent attempt a fix, and report what happened.
"""

import json
import os
from pathlib import Path

import yaml

from build_instance import build_instance, load_bug
from minisweagent import package_dir
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.models.litellm_model import LitellmModel

##############################################
## These will (likely) change in the future ##
##############################################
BUG_ID = 42470179  # the bug we're using to validate the pipeline end-to-end
MODEL_NAME = os.environ.get("MSWEA_MODEL_NAME", "gemini/gemini-2.5-flash")
COST_LIMIT = 1.00  # stop the agent if a single attempt would cost more than this (USD)

# Re-use the same agent prompt templates / config that mini-SWE-agent ships with
DEFAULT_AGENT_CONFIG = yaml.safe_load((Path(package_dir) / "config" / "default.yaml").read_text())["agent"]


def main() -> None:
    bug = load_bug(BUG_ID)
    instance = build_instance(bug)

    print(f"=== Running instance {instance['instance_id']} ({instance['project']}) ===")
    print(f"Image: {instance['image_name']}")
    print(f"Ground-truth fix: {instance['patch_url']}\n")

    env = DockerEnvironment(image=instance["image_name"], timeout=600)
    model = LitellmModel(model_name=MODEL_NAME, set_cache_control="default_end")
    agent = DefaultAgent(model, env, **{**DEFAULT_AGENT_CONFIG, "cost_limit": COST_LIMIT})

    # Record the starting commit so we can diff against it later, even if the
    # agent commits its own changes (as some trajectories do).
    base_commit = env.execute({"command": f"git -C /src/{instance['project']} rev-parse HEAD"})["output"].strip().splitlines()[-1]

    result = agent.run(instance["problem_statement"])

    # Capture whatever the agent changed in the source tree, regardless of how
    # it exited - "submission" is just trailing echo text, not a diff, and the
    # container (with the agent's edits) is removed once we're done. Diffing
    # against base_commit (rather than HEAD) covers both uncommitted changes
    # and any commits the agent made along the way.
    diff_output = env.execute({"command": f"git -C /src/{instance['project']} diff {base_commit}"})["output"]
    diff = "\n".join(
        line for line in diff_output.splitlines() if "ttyname failed" not in line
    )
    if diff:
        diff += "\n"

    print("\n=== Result ===")
    print(f"Exit status: {result['exit_status']}")
    print(f"Model cost: ${agent.cost:.4f}")
    print(f"Model calls: {agent.n_calls}")
    print("\n--- Agent's submission (patch/diff) ---")
    print(result["submission"] or "(empty submission)")

    # Save the trajectory (full step-by-step record) for later inspection
    output_dir = Path("results") / instance["instance_id"]
    output_dir.mkdir(parents=True, exist_ok=True)
    traj_path = output_dir / "trajectory.json"
    agent.save(traj_path, {"instance_id": instance["instance_id"], "result": result})
    print(f"\nSaved trajectory to {traj_path}")

    diff_path = output_dir / "patch.diff"
    diff_path.write_text(diff)
    print(f"Saved diff ({len(diff)} bytes) to {diff_path}")


if __name__ == "__main__":
    main()
