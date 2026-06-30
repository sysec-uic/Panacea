from crash_taxonomy import crash_class


def test_known_families():
    assert crash_class("Use-of-uninitialized-value") == "uninit"
    assert crash_class("Heap-buffer-overflow READ 1") == "heap-oob"
    assert crash_class("Heap-buffer-overflow WRITE {*}") == "heap-oob"
    assert crash_class("Stack-use-after-return READ 4") == "stack-oob"
    assert crash_class("Stack-buffer-overflow READ 2") == "stack-oob"
    assert crash_class("Heap-use-after-free READ 8") == "uaf"
    assert crash_class("Null-dereference READ") == "null-deref"
    assert crash_class("Segv on unknown address") == "null-deref"


def test_read_write_size_suffix_collapsed():
    assert crash_class("Heap-buffer-overflow READ 1") == crash_class("Heap-buffer-overflow READ 8")
    assert crash_class("Heap-buffer-overflow READ 8") == crash_class("Heap-buffer-overflow WRITE {*}")


def test_unknown_maps_to_other():
    assert crash_class("UNKNOWN READ") == "other"
    assert crash_class("Some-new-sanitizer-thing") == "other"
    assert crash_class("") == "other"
    assert crash_class(None) == "other"


def test_case_and_whitespace_insensitive():
    assert crash_class("  heap-buffer-overflow read 8 ") == "heap-oob"
    assert crash_class("USE-OF-UNINITIALIZED-VALUE") == "uninit"
