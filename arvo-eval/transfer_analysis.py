"""Phase 5: turn the Phase B ledger into the cross-project-transfer verdict.

Reads `phaseB_ledger.jsonl`, reduces it to per-bug paired (matched-foreign B vs
placebo-foreign B') values on a chosen metric, and reports the effect with a bootstrap
CI, a paired permutation test, the donors-curve, and the pre-registered go/no-go
decision. Pure stdlib -- no Docker, no numpy; fully fixture-tested.

Primary metric = verified-correct (score >= 1); secondary = oracle-confirmed (score == 2).
The decision rides on the single pre-registered B vs B' contrast (see the experiment
design's §2 decision rule). Per-class effects are descriptive only -- N per class is too
small for inference, so they flag "partial" candidates, never a significance claim.
"""
import itertools
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

MATCHED, PLACEBO, COLD = "matched_foreign", "placebo_foreign", "cold"


def verified_correct(score: int) -> int:
    return 1 if score >= 1 else 0


def oracle_confirmed(score: int) -> int:
    return 1 if score == 2 else 0


def load_records(ledger_path) -> list[dict]:
    path = Path(ledger_path)
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def per_bug_arm_metric(records, metric) -> dict:
    """Mean of `metric(score)` over trials, keyed by (bug_id, arm)."""
    by_cell = defaultdict(list)
    for r in records:
        by_cell[(r["bug_id"], r["arm"])].append(metric(r["score"]))
    return {k: statistics.mean(v) for k, v in by_cell.items()}


def arm_rate(records, arm, metric) -> float:
    """Eval-set mean of the per-bug value for one arm."""
    vals = [v for (bug, a), v in per_bug_arm_metric(records, metric).items() if a == arm]
    return statistics.mean(vals) if vals else 0.0


def paired_diffs(records, arm_a, arm_b, metric) -> list:
    """Per-bug (value[arm_a] - value[arm_b]) for bugs present in BOTH arms."""
    m = per_bug_arm_metric(records, metric)
    bugs = sorted({bug for (bug, a) in m if a == arm_a} & {bug for (bug, a) in m if a == arm_b})
    return [(bug, m[(bug, arm_a)] - m[(bug, arm_b)]) for bug in bugs]


def bootstrap_ci(diffs, *, n=10000, alpha=0.05, seed=0):
    """Percentile bootstrap over bugs. Returns (point_estimate, ci_low, ci_high)."""
    import random
    vals = [d for _, d in diffs]
    if not vals:
        return 0.0, 0.0, 0.0
    point = statistics.mean(vals)
    rng = random.Random(seed)
    k = len(vals)
    means = sorted(statistics.mean(rng.choices(vals, k=k)) for _ in range(n))
    lo = means[int((alpha / 2) * n)]
    hi = means[int((1 - alpha / 2) * n) - 1]
    return point, lo, hi


def permutation_test(diffs, *, n=10000, seed=0) -> float:
    """Two-sided paired sign-flip permutation p-value on the per-bug diffs.

    Exact enumeration of all 2^k sign assignments when k is small; sampled otherwise.
    """
    import random
    vals = [d for _, d in diffs]
    k = len(vals)
    if k == 0:
        return 1.0
    observed = abs(statistics.mean(vals))
    if k <= 14:                                  # exact
        total = 2 ** k
        hits = sum(
            1 for signs in itertools.product((1, -1), repeat=k)
            if abs(statistics.mean(s * v for s, v in zip(signs, vals))) >= observed - 1e-12
        )
        return hits / total
    rng = random.Random(seed)                    # sampled
    hits = sum(
        1 for _ in range(n)
        if abs(statistics.mean(rng.choice((1, -1)) * v for v in vals)) >= observed - 1e-12
    )
    return hits / n


def _bucket(n_donors: int) -> str:
    if n_donors <= 1:
        return "1"
    if n_donors <= 3:
        return "2-3"
    return "4+"


def donors_curve(records, metric, arm_a=MATCHED, arm_b=PLACEBO) -> list:
    """Mean B-B' effect bucketed by donors-available (the matched-arm donor count)."""
    donors = {r["bug_id"]: r["n_donors"] for r in records if r["arm"] == arm_a}
    buckets = defaultdict(list)
    for bug, diff in paired_diffs(records, arm_a, arm_b, metric):
        buckets[_bucket(donors.get(bug, 0))].append(diff)
    order = {"1": 0, "2-3": 1, "4+": 2}
    return [{"bucket": b, "n_bugs": len(v), "mean_effect": statistics.mean(v)}
            for b, v in sorted(buckets.items(), key=lambda kv: order[kv[0]])]


def decide(*, effect, ci_low, ci_high, p_value, per_class, alpha=0.05) -> dict:
    """Pre-registered rule. Go: B significantly > B'. Partial: only some classes
    positive. No-go: null. Per-class effects are exploratory (small N)."""
    if effect > 0 and ci_low > 0 and p_value < alpha:
        return {"verdict": "go",
                "rationale": f"B>B' significant (effect={effect:+.3f}, CI=[{ci_low:.3f},{ci_high:.3f}], p={p_value:.3f})."}
    positive = [c for c, e in per_class if e > 0]
    if positive and effect >= 0:
        return {"verdict": "partial",
                "rationale": f"Overall null but positive in {positive} (exploratory, per-class N small) -> "
                             "rerun scoped to those classes."}
    return {"verdict": "no-go",
            "rationale": f"No B>B' signal (effect={effect:+.3f}, CI=[{ci_low:.3f},{ci_high:.3f}], p={p_value:.3f}); "
                         "federate per-project, skip the global layer."}


def per_class_effects(records, metric, arm_a=MATCHED, arm_b=PLACEBO) -> list:
    cls = {r["bug_id"]: r["crash_class"] for r in records}
    by_class = defaultdict(list)
    for bug, diff in paired_diffs(records, arm_a, arm_b, metric):
        by_class[cls.get(bug)].append(diff)
    return [(c, statistics.mean(v)) for c, v in sorted(by_class.items())]


def analyze(ledger_path, *, metric=verified_correct, seed=0) -> dict:
    records = load_records(ledger_path)
    diffs = paired_diffs(records, MATCHED, PLACEBO, metric)
    point, lo, hi = bootstrap_ci(diffs, seed=seed)
    p = permutation_test(diffs, seed=seed)
    per_class = per_class_effects(records, metric)
    arms = sorted({r["arm"] for r in records})
    return {
        "n_eval_bugs": len(diffs),
        "arm_rates": {a: arm_rate(records, a, metric) for a in arms},
        "effect": point, "ci_low": lo, "ci_high": hi, "p_value": p,
        "per_class": per_class,
        "donors_curve": donors_curve(records, metric),
        "decision": decide(effect=point, ci_low=lo, ci_high=hi, p_value=p, per_class=per_class),
    }


def _format(report) -> str:
    L = ["=== Cross-project transfer: B (matched-foreign) vs B' (placebo) ===",
         f"eval bugs (paired): {report['n_eval_bugs']}"]
    for a, r in report["arm_rates"].items():
        L.append(f"  {a:18s} rate = {r:.3f}")
    L.append(f"effect B-B' = {report['effect']:+.3f}  "
             f"95% CI [{report['ci_low']:+.3f}, {report['ci_high']:+.3f}]  p = {report['p_value']:.3f}")
    L.append("per-class effect (exploratory): " +
             ", ".join(f"{c}={e:+.3f}" for c, e in report["per_class"]))
    L.append("donors curve: " +
             ", ".join(f"{b['bucket']}:{b['mean_effect']:+.3f}(n={b['n_bugs']})"
                       for b in report["donors_curve"]))
    L.append(f"VERDICT: {report['decision']['verdict'].upper()} -- {report['decision']['rationale']}")
    return "\n".join(L)


def main():
    out = Path(__file__).parent / "results" / "transfer"
    ledger = Path(sys.argv[1]) if len(sys.argv) > 1 else out / "phaseB_ledger.jsonl"
    metric = oracle_confirmed if "--oracle-confirmed" in sys.argv else verified_correct
    print(_format(analyze(ledger, metric=metric)))


if __name__ == "__main__":
    main()
