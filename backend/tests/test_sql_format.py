"""Laying a query out - generated SQL and the breakdown's - and leaving alone anything that doesn't parse."""

import json
from unittest.mock import patch

import pytest

from streams import FakeLlm, fake_schema_preamble, run
from waxwing.nltosqlandback import sql_breakdown
from waxwing.nltosqlandback.sql_format import format_sql


def test_a_query_is_laid_out_one_clause_per_line_keeping_comments():
    formatted = format_sql("select a, count(*) n from t where b = 1 -- only b\ngroup by a", "postgres")
    assert formatted.splitlines()[0] == "SELECT"
    assert "GROUP BY" in formatted
    assert "only b" in formatted


def test_unparseable_sql_is_left_alone():
    assert format_sql("select from where", "postgres") is None
    assert format_sql("   ", "postgres") is None


def test_a_trailing_semicolon_survives():
    assert format_sql("select 1;", None).endswith(";")


def test_the_breakdown_can_describe_the_formatted_query():
    llm = FakeLlm(json.dumps({"summary": "s", "sections": [{"title": "t", "kind": "select", "startLine": 1, "endLine": 3}]}))
    with patch("waxwing.nltosqlandback.llm_client.stream_complete", llm), \
            patch("waxwing.nltosqlandback.nlsql_backend.resolve_llm_provider") as provider, \
            patch("waxwing.nltosqlandback.nlsql_backend.schema_preamble", fake_schema_preamble()):
        provider.return_value.model = "m"
        result, _events = run(sql_breakdown.describe_breakdown("select a, b from t where c = 1", format_sql=True))
    assert result["formatted"] is True
    assert result["sql"].startswith("SELECT\n")
    assert "   1 | SELECT" in llm.prompts[0]


def test_formatting_never_changes_the_syntax_tree():
    from waxwing.nltosqlandback.sql_format import format_sql

    # Oracle has no LIMIT: sqlglot would write FETCH FIRST - a different query to it, so it's left alone.
    assert format_sql("select * from s.t limit 10", "oracle") is None
    laid_out = format_sql("select a::int, b from s.t where c != 1 -- why", "postgres")
    assert laid_out == "SELECT\n  CAST(a AS INT),\n  b\nFROM s.t\nWHERE\n  c <> 1 /* why */"


@pytest.mark.formats_sql
def test_generated_sql_is_laid_out_before_it_reaches_the_editor():
    from waxwing.nltosqlandback import nlsql_backend
    from waxwing.nltosqlandback.sql_generation_result import SqlGenerationResult

    result = nlsql_backend.formatted(SqlGenerationResult(status="Success", sql_text="select a, count(*) from s.t group by a"))
    assert result.sql_text == "SELECT\n  a,\n  COUNT(*)\nFROM s.t\nGROUP BY\n  a"
    unparseable = SqlGenerationResult(status="Success", sql_text="select from where")
    assert nlsql_backend.formatted(unparseable).sql_text == "select from where"
