"""Reading each engine's plan, and refusing to plan anything that isn't a plain query.

The fixtures are real EXPLAIN output, captured from Postgres 17, Trino and Oracle Free 23.
"""

import json
from pathlib import Path

import pytest

from waxwing.nltosqlandback import query_plan
from waxwing.nltosqlandback.query_plan import NotPlannable, check_plannable, explain_statement, parse_plan

FIXTURES = Path(__file__).parent / "fixtures"


def test_a_single_select_is_plannable_and_loses_its_semicolon():
    assert check_plannable("SELECT 1;  ", "postgres") == "SELECT 1"
    assert check_plannable("WITH x AS (SELECT 1 AS a) SELECT a FROM x", "postgres").startswith("WITH")
    assert check_plannable("SELECT 1 UNION ALL SELECT 2", "postgres")


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "UPDATE orders SET status = 'x'",
        "INSERT INTO t SELECT 1",
        "DROP TABLE orders",
        "CREATE TABLE t AS SELECT 1",
        "SELECT 1; SELECT 2",
        "WITH gone AS (DELETE FROM orders RETURNING *) SELECT * FROM gone",
        "EXPLAIN ANALYZE SELECT 1",
        "",
    ],
)
def test_anything_but_one_read_only_query_is_refused(sql):
    with pytest.raises(NotPlannable):
        check_plannable(sql, "postgres")


def test_explain_never_analyzes():
    for backend in ("postgresql", "trino", "presto", "mysql", "sqlite", "oracle", "redshift"):
        statements = explain_statement("SELECT 1", backend)
        assert statements, backend
        assert not any("ANALYZE" in statement.upper() for statement in statements), backend
    assert explain_statement("SELECT 1", "some-engine") is None


def test_a_postgres_plan_becomes_an_outline_with_costs_and_details():
    rows = [[(FIXTURES / "postgres_plan.json").read_text()]]
    plan = parse_plan("postgresql", rows)
    assert plan.total_cost == pytest.approx(101.57)
    lines = plan.outline.splitlines()
    assert lines[0].startswith("Limit")
    assert any(line.strip().startswith("Seq Scan on pg_class c") and "Filter:" in line for line in lines)
    assert any("Index Only Scan on pg_attribute a" in line and "Index Cond: (attrelid = c.oid)" in line for line in lines)
    # Indentation follows the tree: the scans are four levels below the Limit.
    assert any(line.startswith("        Seq Scan") for line in lines)
    # Noise is left out.
    assert "Parallel Aware" not in plan.outline and "Plan Width" not in plan.outline


def test_a_postgres_plan_arriving_already_decoded_reads_the_same():
    document = json.loads((FIXTURES / "postgres_plan.json").read_text())
    assert parse_plan("postgresql", [[document]]).outline == parse_plan(
        "postgresql", [[json.dumps(document)]]
    ).outline


def test_a_large_sequential_scan_is_flagged():
    document = [{"Plan": {"Node Type": "Seq Scan", "Relation Name": "orders", "Plan Rows": 2_000_000,
                          "Total Cost": 50_000, "Filter": "(status = 'Shipped')"}}]
    plan = parse_plan("postgresql", [[json.dumps(document)]])
    assert [warning.code for warning in plan.warnings] == ["large_seq_scan"]
    assert "orders" in plan.warnings[0].message and "index" in plan.warnings[0].message


def test_a_trino_distributed_plan_reads_its_fragments_and_ignores_nan_estimates():
    rows = json.loads((FIXTURES / "trino_plan.json").read_text())
    plan = parse_plan("trino", rows)
    assert plan.root is not None
    assert "Output" in plan.outline and "TopN" in plan.outline
    assert "NaN" not in plan.outline
    assert "sourceFragmentIds" not in plan.outline


def test_an_oracle_plan_is_kept_as_its_own_text_with_the_total_cost():
    raw_lines = (FIXTURES / "oracle_plan.txt").read_text().splitlines()
    plan = parse_plan("oracle", [[line] for line in raw_lines])
    assert "HASH GROUP BY" in plan.outline
    assert plan.total_cost == 3323


def test_an_oracle_plan_table_is_read_into_a_tree_with_predicates_and_time():
    raw_lines = (FIXTURES / "oracle_plan.txt").read_text().splitlines()
    plan = parse_plan("oracle", [[line] for line in raw_lines])
    root = plan.root
    assert root.operation == "SELECT STATEMENT" and root.cost == 3323 and root.estimated_rows == 1
    assert [child.operation for child in root.children] == ["HASH GROUP BY"]
    assert root.children[0].children[0].operation == "CONCATENATION"
    nodes = list(root.walk())
    users = next(node for node in nodes if node.object_name == "USER$")
    assert users.operation == "TABLE ACCESS FULL" and users.estimated_rows == 140
    cartesian = next(node for node in nodes if node.operation == "MERGE JOIN CARTESIAN")
    assert cartesian.estimated_rows == 30462 and len(cartesian.children) == 2
    assert plan.estimated_seconds == 1
    assert "possible_cross_join" in [warning.code for warning in plan.warnings]


def test_an_oracle_plan_with_big_tables_flags_the_full_scan():
    raw = """Plan hash value: 2686943305

-----------------------------------------------------------------------------------
| Id  | Operation           | Name    | Rows  | Bytes | Cost (%CPU)| Time     |
-----------------------------------------------------------------------------------
|   0 | SELECT STATEMENT    |         |     9 |   864 | 25895   (1)| 00:00:02 |
|   1 |  SORT ORDER BY      |         |     9 |   864 | 25895   (1)| 00:00:02 |
|*  2 |   HASH JOIN ANTI NA |         |     9 |   864 | 25894   (1)| 00:00:02 |
|*  3 |    TABLE ACCESS FULL| MOVIES  |   918 | 79866 |   375   (0)| 00:00:01 |
|*  4 |    TABLE ACCESS FULL| RATINGS |    12M|   186M| 25499   (1)| 00:00:01 |
-----------------------------------------------------------------------------------

Predicate Information (identified by operation id):
---------------------------------------------------

   2 - access("MOVIE_ID"="MOVIE_ID")
   3 - filter("MOVIE_ID"<=1000)
   4 - filter("RATING">=4)
"""
    plan = parse_plan("oracle", [[line] for line in raw.splitlines()])
    join = plan.root.children[0].children[0]
    assert [child.object_name for child in join.children] == ["MOVIES", "RATINGS"]
    assert join.details == {"Access": '"MOVIE_ID"="MOVIE_ID"'}
    assert join.children[1].estimated_rows == 12_000_000
    assert plan.estimated_seconds == 2
    assert [warning.code for warning in plan.warnings] == ["large_seq_scan"]
    assert '"RATING">=4' in plan.warnings[0].message


def test_a_sqlite_plan_nests_by_parent_and_flags_full_scans():
    rows = [[2, 0, 0, "SCAN orders"], [5, 0, 0, "SEARCH customers USING INTEGER PRIMARY KEY (rowid=?)"]]
    plan = parse_plan("sqlite", rows)
    assert plan.outline.splitlines()[1:] == ["  SCAN orders", "  SEARCH customers USING INTEGER PRIMARY KEY (rowid=?)"]
    assert [warning.code for warning in plan.warnings] == ["full_scan"]


def test_a_long_outline_is_cut_at_whole_lines_and_says_how_many_are_missing():
    root = query_plan.PlanNode("Append", children=[query_plan.PlanNode(f"Seq Scan", object_name=f"t{i}") for i in range(500)])
    outline = query_plan.plan_outline(root, max_chars=200)
    assert len(outline) < 260
    assert outline.splitlines()[-1].endswith("more steps not shown")


def _scan(table, rows, **extra):
    return {"Node Type": "Seq Scan", "Relation Name": table, "Plan Rows": rows, "Total Cost": rows / 10, **extra}


def test_a_nested_loop_without_a_join_condition_is_a_cartesian_product():
    document = [{"Plan": {
        "Node Type": "Nested Loop", "Plan Rows": 890_000_000, "Total Cost": 31_456_000_000,
        "Plans": [_scan("wb_health_population", 11_800), {
            "Node Type": "Materialize", "Plan Rows": 75_700, "Total Cost": 2_600,
            "Plans": [_scan("birth_names", 75_700)],
        }],
    }}]
    plan = parse_plan("postgresql", [[json.dumps(document)]])
    codes = [warning.code for warning in plan.warnings]
    assert "possible_cross_join" in codes and "very_high_cost" in codes
    cross = next(warning for warning in plan.warnings if warning.code == "possible_cross_join")
    assert "wb_health_population" in cross.message and "birth_names" in cross.message


def test_an_indexed_nested_loop_or_a_one_row_side_is_not_a_cartesian_product():
    indexed = [{"Plan": {
        "Node Type": "Nested Loop", "Plan Rows": 5_000, "Total Cost": 900,
        "Plans": [_scan("orders", 5_000), {"Node Type": "Index Scan", "Relation Name": "customers", "Plan Rows": 1,
                                           "Total Cost": 0.3, "Index Cond": "(id = orders.customer_id)"}],
    }}]
    one_row = [{"Plan": {
        "Node Type": "Nested Loop", "Plan Rows": 5_000, "Total Cost": 900,
        "Plans": [_scan("orders", 5_000), {"Node Type": "Aggregate", "Plan Rows": 1, "Total Cost": 10,
                                           "Plans": [_scan("rates", 40)]}],
    }}]
    for document in (indexed, one_row):
        plan = parse_plan("postgresql", [[json.dumps(document)]])
        assert "possible_cross_join" not in [warning.code for warning in plan.warnings]
