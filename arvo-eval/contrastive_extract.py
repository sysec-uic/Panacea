"""Contrastive heuristic extraction from the (agent-patch, ground-truth-fix) gap.

Idea #2: instead of learning only from the agent's own verified-correct wins, learn
from HOW a wrong/incomplete attempt differed from the canonical ARVO `-fix`. The
ground-truth fix turns a noisy failure into a precise, gradeable lesson:

  "you guarded the read in <site>; the real fix corrected <root cause> upstream."

This complements extract_heuristic.py (success-only). Same store/injection plumbing;
only the prompt and the lesson schema differ — a contrastive lesson additionally
records the wrong_approach and correct_approach so the playbook can warn the agent
off the dead end, not just point at the right one.
"""
import json

from llm import call_llm

SYSTEM = (
    "You teach a C/C++ vulnerability-repair agent by CONTRASTING a wrong or incomplete "
    "patch against the known-correct fix for the same bug. Explain the gap, then distill "
    "a reusable lesson that would steer the agent away from the wrong approach toward the "
    "right one on a DIFFERENT future bug of the same class. Output ONLY a JSON object with "
    "keys: trigger, wrong_approach, correct_approach, lesson, how_to_apply, tags (array of "
    "short slugs), confidence (high|medium|low). Be specific to the bug class, not generic. "
    "Keep each string under 280 characters."
)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


def build_contrastive_prompt(bug: dict, agent_diff: str, gold_diff: str, verdict: str) -> str:
    return f"""Bug {bug['localId']} ({bug['crash_type']}, {bug['sanitizer']}, target {bug['fuzz_target']}).
The agent's patch verdict was: {verdict} (i.e. NOT a correct fix).

Crash output:
{bug.get('crash_output', '')[:2500]}

=== AGENT'S PATCH (wrong / incomplete) ===
{agent_diff[:5000]}

=== GROUND-TRUTH FIX (canonical -fix, correct) ===
{gold_diff[:5000]}

The two patches touch different code. Explain why the agent's approach failed to fix
the real bug, what the correct fix did instead, and produce the contrastive lesson JSON now."""


def extract_contrastive_heuristic(*, bug: dict, agent_diff: str, gold_diff: str,
                                  verdict: str = "still_crashes_or_wrong", llm=call_llm) -> dict:
    """Return a structured contrastive heuristic learned from the agent/gold gap."""
    raw = llm(build_contrastive_prompt(bug, agent_diff, gold_diff, verdict), system=SYSTEM)
    data = json.loads(_strip_fences(raw))
    return {
        "trigger": data["trigger"],
        "wrong_approach": data["wrong_approach"],
        "correct_approach": data["correct_approach"],
        "lesson": data["lesson"],
        "how_to_apply": data["how_to_apply"],
        "tags": list(data.get("tags", [])),
        "confidence": data.get("confidence", "medium"),
        "kind": "contrastive",
    }
