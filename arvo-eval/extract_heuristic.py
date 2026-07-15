"""Turn one verified-correct repair into a structured, reusable heuristic."""
import json

from llm import call_llm

SYSTEM = (
    "You distill C/C++ vulnerability fixes into terse, reusable repair heuristics "
    "for the mruby interpreter. Output ONLY a JSON object with keys: trigger, "
    "root_cause_lesson, how_to_apply, tags (array of short slugs), confidence "
    "(high|medium|low). Be specific to the bug class, not generic advice. Keep each "
    "string under 240 characters."
)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


def build_prompt(bug: dict, diff: str, trajectory_summary: str, verdict: str) -> str:
    return f"""Bug {bug['localId']} ({bug['crash_type']}, {bug['sanitizer']}, target {bug['fuzz_target']}).
Verdict: {verdict}.

Crash output:
{(bug.get('crash_output') or '')[:3000]}

Accepted fix diff:
{diff[:6000]}

Agent reasoning summary:
{trajectory_summary[:2000]}

Produce the heuristic JSON now."""


def extract_heuristic(*, bug: dict, diff: str, trajectory_summary: str, verdict: str, llm=call_llm) -> dict:
    raw = llm(build_prompt(bug, diff, trajectory_summary, verdict), system=SYSTEM)
    data = json.loads(_strip_fences(raw))
    # Normalize required keys so the store never receives a malformed entry.
    return {
        "trigger": data["trigger"],
        "root_cause_lesson": data["root_cause_lesson"],
        "how_to_apply": data["how_to_apply"],
        "tags": list(data.get("tags", [])),
        "confidence": data.get("confidence", "medium"),
    }
