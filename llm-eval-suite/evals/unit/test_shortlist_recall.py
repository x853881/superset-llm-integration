"""Unit tests for evals/shortlist_recall.py: the tables a request needs, how deep they rank, and
recall at k.

Run: pytest -m unit
"""

import pytest

import shortlist_recall

pytestmark = pytest.mark.unit


def test_the_tables_needed_come_from_the_entry_or_its_reference_sql():
    assert shortlist_recall.needed_tables({"tables": ["Sales.Orders", "sales.orders"]}, "postgres") == ["sales.orders"]
    golden = {"input": "q", "expected_output": "WITH x AS (SELECT 1) SELECT * FROM a.t JOIN b.u ON 1 = 1, x"}
    assert shortlist_recall.needed_tables(golden, "postgres") == ["a.t", "b.u"]


def test_recall_at_k_counts_requests_with_every_table_in_the_top_k():
    ranked = [f"s.t{n}" for n in range(1, 101)]
    entries = [
        {"name": "near", "request": "q", "tables": ["s.t1", "s.t5"]},
        {"name": "far", "request": "q", "tables": ["s.t2", "s.t30"]},
        {"name": "gone", "request": "q", "tables": ["s.t1", "other.x"]},
        {"name": "no tables", "request": "q"},
    ]
    depths = shortlist_recall.measure(entries, lambda _text: {"status": "ready", "strategy": "words", "tables": ranked}, "postgres")
    result = shortlist_recall.report(depths)
    assert result["requests"] == 3
    assert result["recallAt"] == {8: 1 / 3, 20: 1 / 3, 40: 2 / 3, 80: 2 / 3}
    assert result["missingTables"] == 1
    assert [row["name"] for row in result["deepest"]] == ["gone", "far", "near"]


def test_a_database_not_read_yet_stops_it():
    with pytest.raises(SystemExit):
        shortlist_recall.measure([{"request": "q", "tables": ["a.b"]}], lambda _text: {"status": "none"}, "postgres")
