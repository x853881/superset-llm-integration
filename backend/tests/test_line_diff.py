from waxwing.nltosqlandback.line_diff import line_diff


def test_identical_text_produces_only_context_lines():
    assert line_diff("SELECT *\nFROM users", "SELECT *\nFROM users") == "  SELECT *\n  FROM users"


def test_appended_line_is_reported_as_an_addition():
    assert line_diff(
        "SELECT *\nFROM users",
        "SELECT *\nFROM users\nWHERE active = true",
    ) == "  SELECT *\n  FROM users\n+ WHERE active = true"


def test_removed_line_is_reported_as_a_removal():
    assert line_diff(
        "SELECT *\nFROM users\nWHERE active = true",
        "SELECT *\nFROM users",
    ) == "  SELECT *\n  FROM users\n- WHERE active = true"


def test_changed_line_is_reported_as_removal_then_addition():
    assert line_diff(
        "SELECT *\nFROM users\nWHERE active = true",
        "SELECT *\nFROM users\nWHERE active = false",
    ) == "  SELECT *\n  FROM users\n- WHERE active = true\n+ WHERE active = false"


def test_empty_old_text_reports_every_new_line_as_an_addition():
    assert line_diff("", "SELECT 1") == "+ SELECT 1"
