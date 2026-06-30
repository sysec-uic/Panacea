from transfer_eval_set import select_eval_bugs


def H(bug_id, project, cls, after):
    return {"id": f"h-{bug_id}", "source_bug": bug_id, "source_project": project,
            "crash_class": cls, "added_after_bug": after}


def bug(local_id, project, crash_type):
    return {"localId": local_id, "project": project, "crash_type": crash_type}


def test_excludes_single_project_class():
    # uaf appears in only one project across the population -> no target qualifies on it.
    bugs = [bug(1, "php", "Heap-use-after-free READ 8"),
            bug(2, "php", "Heap-use-after-free READ 1")]
    hs = [H(1, "php", "uaf", 1)]
    assert select_eval_bugs(bugs, hs) == []


def test_requires_prior_foreign_donor():
    # uninit is multi-project, but the only foreign donor is added AFTER the target.
    bugs = [bug(1, "mruby", "Use-of-uninitialized-value"),
            bug(10, "php", "Use-of-uninitialized-value")]
    hs = [H(10, "php", "uninit", 10)]            # donor not prior to bug 1
    assert select_eval_bugs(bugs, hs) == []


def test_selects_bug_with_prior_foreign_donor_and_fillable_placebo():
    bugs = [bug(1, "php", "Use-of-uninitialized-value"),
            bug(2, "php", "Heap-buffer-overflow READ 1"),
            bug(20, "mruby", "Use-of-uninitialized-value")]
    hs = [H(1, "php", "uninit", 1),              # prior foreign uninit donor for bug 20
          H(2, "php", "heap-oob", 2)]            # mismatched-class foreign donor -> placebo fill
    got = [b["localId"] for b in select_eval_bugs(bugs, hs)]
    assert got == [20]


def test_drops_target_when_placebo_cannot_be_filled():
    # bug 20 has a matched foreign donor (k=1) but NO mismatched-class foreign donor.
    bugs = [bug(1, "php", "Use-of-uninitialized-value"),
            bug(20, "mruby", "Use-of-uninitialized-value")]
    hs = [H(1, "php", "uninit", 1)]
    assert select_eval_bugs(bugs, hs) == []


def test_other_class_is_never_a_target():
    # "other" is a grab-bag (UNKNOWN READ etc.); intra-"other" matching is not real
    # class matching, so such bugs are excluded as targets even with prior donors.
    bugs = [bug(1, "php", "UNKNOWN READ"),
            bug(2, "vlc", "Some-novel-thing"),     # both -> "other", multi-project
            bug(20, "mruby", "UNKNOWN READ")]
    hs = [H(1, "php", "other", 1), H(2, "vlc", "other", 2)]
    assert select_eval_bugs(bugs, hs) == []
