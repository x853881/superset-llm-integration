"""Placing Accelerate's changes in the SQL by the fragments they quote."""

from waxwing.nltosqlandback.change_anchors import Change, anchor, anchored_count, locate, parse_changes, tokens

ORIGINAL = """SELECT e.lei,
       (SELECT COUNT(*) FROM relationships r WHERE r.parent = e.lei) AS children
FROM entities e
WHERE e.status = 'ACTIVE'"""

REWRITTEN = """WITH child_counts AS (
    SELECT parent, COUNT(*) AS children
    FROM relationships
    GROUP BY parent
)
SELECT e.lei, COALESCE(c.children, 0) AS children
FROM entities e
LEFT JOIN child_counts c ON c.parent = e.lei
WHERE e.status = 'ACTIVE'"""


def test_a_fragment_is_found_whatever_its_spacing_line_breaks_and_keyword_case():
    where = locate("left join child_counts c\n   on c.parent = e.lei", REWRITTEN)
    assert where is not None
    assert REWRITTEN[where[0]:where[1]] == "LEFT JOIN child_counts c ON c.parent = e.lei"


def test_comments_dont_stop_a_match():
    sql = "SELECT a -- the key\n, b FROM t"
    where = locate("SELECT a, b", sql)
    assert where is not None and sql[where[0]:where[1]] == "SELECT a -- the key\n, b"


def test_string_literals_and_quoted_names_must_match_exactly():
    assert locate("e.status = 'active'", REWRITTEN) is None
    assert locate("e.status = 'ACTIVE'", REWRITTEN) is not None
    assert locate('SELECT "Name" FROM t', 'SELECT "name" FROM t') is None


def test_a_fragment_that_isnt_there_or_is_empty_has_no_place():
    assert locate("JOIN accounts a ON a.id = e.id", REWRITTEN) is None
    assert locate("", REWRITTEN) is None
    assert locate("  -- just a comment\n", REWRITTEN) is None


def test_tokens_keep_their_positions_and_compare_by_lower_case():
    sql = "Select A.x,'It''s' FROM [My Table]"
    found = tokens(sql)
    assert [token.key for token in found] == ["select", "a", ".", "x", ",", "'It''s'", "from", "[My Table]"]
    assert sql[found[5].start:found[5].end] == "'It''s'"


def test_changes_are_parsed_from_the_new_shape_and_the_old():
    assert parse_changes([
        {"summary": "Correlated subquery to a join", "why": "Read once, not per row", "before": "(SELECT", "after": "LEFT JOIN"},
        "kept the filter",
        {"summary": "", "why": ""},
        {"summary": None, "why": "only a reason"},
    ]) == [
        Change("Correlated subquery to a join", "Read once, not per row", "(SELECT", "LEFT JOIN"),
        Change("kept the filter"),
        Change("", "only a reason"),
    ]
    assert parse_changes(None) == [] and parse_changes("not a list") == []


def test_fragments_found_are_replaced_by_the_exact_text_and_others_are_dropped():
    changes = anchor(
        [
            Change(
                "Correlated subquery to a join",
                "It ran once per entity",
                before="(select count(*) from relationships r where r.parent = e.lei)",
                after="left join child_counts c on c.parent = e.lei",
            ),
            Change("Made up", "Never happened", before="DISTINCT ON (x)", after="QUALIFY ROW_NUMBER()"),
            Change("Added a CTE", after="WITH child_counts AS ("),
        ],
        ORIGINAL,
        REWRITTEN,
    )
    assert changes[0].before == "(SELECT COUNT(*) FROM relationships r WHERE r.parent = e.lei)"
    assert changes[0].after == "LEFT JOIN child_counts c ON c.parent = e.lei"
    # Not found: kept as a note, but pinned nowhere rather than somewhere wrong.
    assert changes[1] == Change("Made up", "Never happened")
    assert changes[2].before == "" and changes[2].after == "WITH child_counts AS ("
    assert anchored_count(changes) == 2
