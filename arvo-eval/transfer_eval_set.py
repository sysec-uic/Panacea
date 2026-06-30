"""Select the bugs on which cross-project transfer can even register.

A target bug t qualifies only if:
  1. crash_class(t) spans >=2 distinct projects in the population (transfer is
     possible at all), AND
  2. >=1 prior foreign donor of the same class exists (arm B's slice is non-empty), AND
  3. the mismatched-class foreign pool can fill arm B''s placebo to match B's count.

Pure module: operates on bug records + the frozen store H. See the transfer design.
"""
from collections import defaultdict

from crash_taxonomy import crash_class
from transfer_filter import matched_foreign, placebo_foreign


def _multi_project_classes(bugs):
    projects_by_class = defaultdict(set)
    for b in bugs:
        projects_by_class[crash_class(b["crash_type"])].add(b["project"])
    return {cls for cls, projs in projects_by_class.items() if len(projs) >= 2}


def select_eval_bugs(bugs, heuristics):
    multi = _multi_project_classes(bugs)
    selected = []
    for b in bugs:
        cls = crash_class(b["crash_type"])
        if cls == "other":          # grab-bag bucket: intra-"other" matching isn't real
            continue
        if cls not in multi:
            continue
        matched = matched_foreign(heuristics, before_bug=b["localId"],
                                  project=b["project"], crash_class=cls)
        if not matched:
            continue
        placebo = placebo_foreign(heuristics, before_bug=b["localId"], project=b["project"],
                                  crash_class=cls, k=len(matched), seed=b["localId"])
        if len(placebo) < len(matched):
            continue
        selected.append(b)
    return selected
