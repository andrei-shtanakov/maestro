from maestro.scope_gate import find_escapes, normalize


def test_exact_match_is_in_scope():
    assert find_escapes(["src/foo.py"], ["src/foo.py"]) == []


def test_exact_pattern_does_not_match_other_file():
    assert find_escapes(["src/bar.py"], ["src/foo.py"]) == ["src/bar.py"]


def test_double_star_covers_nested():
    assert find_escapes(["src/a/b.py", "src/foo.py"], ["src/**"]) == []


def test_single_star_does_not_cross_slash():
    # '*.py' matches top-level only
    assert find_escapes(["a/foo.py"], ["*.py"]) == ["a/foo.py"]
    assert find_escapes(["foo.py"], ["*.py"]) == []


def test_dir_double_star_matches_contents_not_bare_dir():
    assert find_escapes(["dir/x.py"], ["dir/**"]) == []
    # a bare 'dir' path (no trailing slash) is NOT matched by 'dir/**'
    assert find_escapes(["dir"], ["dir/**"]) == ["dir"]


def test_leading_double_star():
    assert find_escapes(["a/b/foo.py", "foo.py"], ["**/foo.py"]) == []


def test_escape_in_parent_dir():
    assert find_escapes(["other/x.py"], ["src/**"]) == ["other/x.py"]


def test_empty_scope_skips():
    assert find_escapes(["anything.py"], []) == []


def test_deleted_path_string_still_matched():
    # find_escapes never touches the filesystem; a deleted path is just a string
    assert find_escapes(["src/gone.py"], ["src/**"]) == []


def test_multiple_patterns_union():
    assert find_escapes(["src/a.py", "docs/b.md", "x/c"], ["src/**", "docs/**"]) == [
        "x/c"
    ]


def test_normalize_strips_dot_slash_and_backslash():
    assert normalize(["./src/a.py", "src\\b.py"]) == ["src/a.py", "src/b.py"]
