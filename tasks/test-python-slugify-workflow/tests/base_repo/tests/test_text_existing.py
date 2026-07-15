from flowcheck import collapse_spaces, slugify_label


def test_collapse_spaces_handles_mixed_whitespace():
    assert collapse_spaces("  alpha\n\tbeta  ") == "alpha beta"


def test_collapse_spaces_preserves_words():
    assert collapse_spaces("release candidate") == "release candidate"


def test_slugify_keeps_simple_labels():
    assert slugify_label("Hello World") == "hello-world"
