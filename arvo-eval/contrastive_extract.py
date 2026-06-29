"""Contrastive heuristic extraction from a (rejected-attempt, accepted-attempt) pair.

Idea #2, deployment-faithful variant: learn from HOW a rejected patch differed from
an accepted one for the same bug. Crucially, BOTH patches are the agent's own work,
and "accepted" is decided by the deployment oracle (crash gone + `make test`) — NOT by
the ARVO `-fix` image. The system must keep working on future bugs that have no `-fix`,
so nothing here may depend on it.

The retry loop in repair_loop.py produces the pair naturally: the last rejected attempt
plus the attempt that finally passed. The lesson distilled from that transition
("attempt 1 guarded the reader and tests failed; the passing attempt fixed the emitter")
warns a FUTURE bug of the same class off the dead end.
"""
import json

from llm import call_llm

SYSTEM = (
    "You teach a C/C++ vulnerability-repair agent by CONTRASTING a rejected patch against "
    "an accepted patch for the same bug (both written by the agent; accepted = crash gone "
    "AND the project's own test suite passes). Explain why the rejected approach was wrong, "
    "what the accepted one did instead, and distill a reusable lesson that steers the agent "
    "away from the dead end on a DIFFERENT future bug of the same class. Output ONLY a JSON "
    "object with keys: trigger, wrong_approach, correct_approach, lesson, how_to_apply, "
    "tags (array of short slugs), confidence (high|medium|low). Be specific to the bug "
    "class, not generic. Keep each string under 280 characters."
)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


def build_contrastive_prompt(bug: dict, rejected_diff: str, accepted_diff: str,
                             rejected_verdict: str) -> str:
    return f"""Bug {bug['localId']} ({bug['crash_type']}, {bug['sanitizer']}, target {bug['fuzz_target']}).

Crash output:
{bug.get('crash_output', '')[:2500]}

=== REJECTED ATTEMPT (verdict: {rejected_verdict}) ===
{rejected_diff[:5000]}

=== ACCEPTED ATTEMPT (crash gone AND make test passed) ===
{accepted_diff[:5000]}

The two patches take different approaches. Explain why the rejected one failed to truly
fix the bug, what the accepted one did instead, and produce the contrastive lesson JSON now."""


def extract_contrastive_heuristic(*, bug: dict, rejected_diff: str, accepted_diff: str,
                                  rejected_verdict: str = "fixed_tests_failed",
                                  llm=call_llm) -> dict:
    """Return a structured contrastive heuristic learned from the rejected/accepted gap."""
    raw = llm(build_contrastive_prompt(bug, rejected_diff, accepted_diff, rejected_verdict),
              system=SYSTEM)
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
