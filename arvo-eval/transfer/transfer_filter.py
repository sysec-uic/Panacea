"""Arm-parameterized eligibility filter over the frozen heuristic store H.

All arms share the chronological holdout (`added_after_bug < before_bug`); they
differ only in the project/class predicate. The placebo arm (B') draws the *same
count* as the matched arm (B) from mismatched-class foreign donors, with a per-bug
deterministic seed, so the only variable between B and B' is class relevance.

Pure module: no Docker, no DB, no LLM. See the cross-project-transfer design.
"""
import random

from crash_taxonomy import crash_class


def _holdout(heuristics, before_bug):
    return [h for h in heuristics if h["added_after_bug"] < before_bug]


def matched_foreign(heuristics, *, before_bug, project, crash_class):
    """Arm B: other-project heuristics in the same crash class (holdout-filtered)."""
    return [h for h in _holdout(heuristics, before_bug)
            if h.get("source_project") != project and h.get("crash_class") == crash_class]


def in_project(heuristics, *, before_bug, project, source_bug):
    """Arm C (reference): same-project heuristics, excluding the target's own lesson."""
    return [h for h in _holdout(heuristics, before_bug)
            if h.get("source_project") == project and h.get("source_bug") != source_bug]


def placebo_foreign(heuristics, *, before_bug, project, crash_class, k, seed):
    """Arm B': k other-project heuristics from *different* crash classes.

    Deterministic for a given seed; capped at the available pool size when the
    mismatched-class foreign pool is smaller than k.
    """
    pool = [h for h in _holdout(heuristics, before_bug)
            if h.get("source_project") != project and h.get("crash_class") != crash_class]
    rng = random.Random(seed)
    rng.shuffle(pool)
    return pool[:k]


def select_for_arm(arm, heuristics, *, bug, seed=None):
    """Return the heuristics injected into `bug` under `arm`.

    arm in {"cold", "matched_foreign", "placebo_foreign", "in_project"}.
    `seed` defaults to the bug's localId so placebo sampling is reproducible per bug.
    """
    before = bug["localId"]
    project = bug["project"]
    cls = crash_class(bug["crash_type"])
    if arm == "cold":
        return []
    if arm == "matched_foreign":
        return matched_foreign(heuristics, before_bug=before, project=project, crash_class=cls)
    if arm == "in_project":
        return in_project(heuristics, before_bug=before, project=project, source_bug=before)
    if arm == "placebo_foreign":
        k = len(matched_foreign(heuristics, before_bug=before, project=project, crash_class=cls))
        return placebo_foreign(heuristics, before_bug=before, project=project, crash_class=cls,
                               k=k, seed=before if seed is None else seed)
    raise ValueError(f"unknown arm: {arm}")
