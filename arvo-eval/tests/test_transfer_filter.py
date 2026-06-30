from transfer_filter import select_for_arm, matched_foreign, in_project, placebo_foreign


def H(bug_id, project, cls, after):
    return {"id": f"h-{bug_id}", "source_bug": bug_id, "source_project": project,
            "crash_class": cls, "added_after_bug": after, "trigger": f"t{bug_id}"}


HS = [
    H(1, "mruby", "uninit", 1),
    H(2, "php", "uninit", 2),
    H(3, "php", "uaf", 3),
    H(4, "vlc", "uninit", 10),
    H(5, "wireshark", "heap-oob", 4),
]


def bug(local_id, project, crash_type):
    return {"localId": local_id, "project": project, "crash_type": crash_type}


def test_cold_injects_nothing():
    assert select_for_arm("cold", HS, bug=bug(99, "mruby", "Use-of-uninitialized-value")) == []


def test_holdout_excludes_present_and_future():
    # before_bug=5 -> only added_after_bug < 5 are eligible (h1,h2,h3,h5; not h4@10).
    got = matched_foreign(HS, before_bug=5, project="mruby", crash_class="uninit")
    assert [h["id"] for h in got] == ["h-2"]   # other project + uninit + holdout


def test_matched_foreign_excludes_same_project_and_other_class():
    b = bug(11, "mruby", "Use-of-uninitialized-value")
    got = select_for_arm("matched_foreign", HS, bug=b)
    # eligible uninit from other projects before 11: h2(php), h4(vlc)
    assert sorted(h["id"] for h in got) == ["h-2", "h-4"]
    assert all(h["source_project"] != "mruby" and h["crash_class"] == "uninit" for h in got)


def test_in_project_keeps_same_project_excludes_self():
    got = in_project(HS, before_bug=11, project="php", source_bug=3)
    assert [h["id"] for h in got] == ["h-2"]   # php h2 kept, h3 is self


def test_placebo_matches_matched_count_and_avoids_class_and_project():
    b = bug(11, "mruby", "Use-of-uninitialized-value")
    k = len(select_for_arm("matched_foreign", HS, bug=b))   # = 2
    got = select_for_arm("placebo_foreign", HS, bug=b, seed=0)
    # placebo pool = other project + NOT uninit, before 11: h3(php,uaf), h5(wireshark,heap-oob)
    assert len(got) == k
    assert all(h["source_project"] != "mruby" and h["crash_class"] != "uninit" for h in got)


def test_placebo_capped_when_pool_smaller_than_k():
    b = bug(11, "mruby", "Use-of-uninitialized-value")
    small = [HS[0], HS[1], HS[3], HS[2]]   # one non-uninit foreign donor only (h3)
    got = placebo_foreign(small, before_bug=11, project="mruby", crash_class="uninit", k=2, seed=0)
    assert len(got) == 1
    assert got[0]["id"] == "h-3"


def test_placebo_deterministic_for_a_seed():
    b = bug(11, "mruby", "Use-of-uninitialized-value")
    a = select_for_arm("placebo_foreign", HS, bug=b, seed=7)
    c = select_for_arm("placebo_foreign", HS, bug=b, seed=7)
    assert [h["id"] for h in a] == [h["id"] for h in c]
