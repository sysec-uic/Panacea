import json

from transfer_analysis import (
    verified_correct, oracle_confirmed, per_bug_arm_metric, arm_rate,
    paired_diffs, bootstrap_ci, permutation_test, donors_curve, decide, analyze,
)


def rec(bug, arm, trial, score, n_donors=2, crash_class="uninit"):
    return {"bug_id": bug, "arm": arm, "trial": trial, "score": score,
            "n_donors": n_donors, "crash_class": crash_class}


# --- metrics ---------------------------------------------------------------

def test_indicators():
    assert [verified_correct(s) for s in (0, 1, 2)] == [0, 1, 1]
    assert [oracle_confirmed(s) for s in (0, 1, 2)] == [0, 0, 1]


def test_per_bug_arm_metric_means_over_trials():
    recs = [rec(1, "matched_foreign", 0, 2), rec(1, "matched_foreign", 1, 0)]
    m = per_bug_arm_metric(recs, verified_correct)
    assert m[(1, "matched_foreign")] == 0.5     # one solved, one not


def test_arm_rate_is_mean_of_per_bug_values():
    recs = [rec(1, "matched_foreign", 0, 2), rec(2, "matched_foreign", 0, 0)]
    assert arm_rate(recs, "matched_foreign", verified_correct) == 0.5


# --- paired structure ------------------------------------------------------

def test_paired_diffs_only_bugs_in_both_arms():
    recs = [rec(1, "matched_foreign", 0, 2), rec(1, "placebo_foreign", 0, 0),
            rec(2, "matched_foreign", 0, 2)]   # bug 2 missing from placebo -> dropped
    diffs = paired_diffs(recs, "matched_foreign", "placebo_foreign", verified_correct)
    assert diffs == [(1, 1.0)]                 # B=1.0, B'=0.0 -> diff +1.0


# --- statistics ------------------------------------------------------------

def test_bootstrap_ci_is_deterministic_and_brackets_constant_effect():
    diffs = [(i, 1.0) for i in range(20)]      # all +1 -> CI degenerate at 1.0
    point, lo, hi = bootstrap_ci(diffs, seed=0)
    assert point == 1.0 and lo == 1.0 and hi == 1.0


def test_bootstrap_ci_excludes_zero_for_strong_positive():
    diffs = [(i, 1.0) for i in range(15)] + [(99, 0.0)]
    point, lo, hi = bootstrap_ci(diffs, seed=1)
    assert lo > 0 and point > 0


def test_permutation_all_zero_diffs_p_is_one():
    assert permutation_test([(i, 0.0) for i in range(8)]) == 1.0


def test_permutation_strong_one_sided_is_small():
    p = permutation_test([(i, 1.0) for i in range(10)])   # exact: only 1 of 2^10 sign-flips
    assert p < 0.01


def test_permutation_symmetric_is_large():
    p = permutation_test([(1, 1.0), (2, -1.0), (3, 1.0), (4, -1.0)])
    assert p > 0.5


# --- donors curve ----------------------------------------------------------

def test_donors_curve_buckets_effect_by_donor_count():
    recs = [
        rec(1, "matched_foreign", 0, 2, n_donors=1), rec(1, "placebo_foreign", 0, 0, n_donors=1),
        rec(2, "matched_foreign", 0, 2, n_donors=5), rec(2, "placebo_foreign", 0, 2, n_donors=5),
    ]
    curve = {b["bucket"]: b for b in donors_curve(recs, verified_correct)}
    assert curve["1"]["mean_effect"] == 1.0 and curve["1"]["n_bugs"] == 1
    assert curve["4+"]["mean_effect"] == 0.0 and curve["4+"]["n_bugs"] == 1


# --- decision rule ---------------------------------------------------------

def test_decide_go_on_significant_positive():
    out = decide(effect=0.4, ci_low=0.1, ci_high=0.7, p_value=0.01, per_class=[("uninit", 0.4)])
    assert out["verdict"] == "go"


def test_decide_no_go_on_null():
    out = decide(effect=0.0, ci_low=-0.2, ci_high=0.2, p_value=0.9, per_class=[("uninit", 0.0)])
    assert out["verdict"] == "no-go"


def test_decide_partial_when_only_some_classes_positive():
    out = decide(effect=0.05, ci_low=-0.1, ci_high=0.2, p_value=0.4,
                 per_class=[("uninit", 0.5), ("heap-oob", -0.1)])
    assert out["verdict"] == "partial"


# --- end to end ------------------------------------------------------------

def test_analyze_reads_ledger_and_returns_verdict(tmp_path):
    recs = []
    for bug in range(1, 11):
        recs.append(rec(bug, "matched_foreign", 0, 2))     # B solves
        recs.append(rec(bug, "placebo_foreign", 0, 0))     # B' does not
    p = tmp_path / "phaseB_ledger.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n")

    report = analyze(p)
    assert report["n_eval_bugs"] == 10
    assert report["effect"] == 1.0
    assert report["arm_rates"]["matched_foreign"] == 1.0
    assert report["arm_rates"]["placebo_foreign"] == 0.0
    assert report["decision"]["verdict"] == "go"
