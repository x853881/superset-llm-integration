from waxwing.nltosqlandback.sql_analyzer import analyze, syntax_errors


def test_valid_select_is_valid():
    analysis = analyze("SELECT id, name FROM users WHERE active = true")
    assert analysis.valid
    assert analysis.error is None


def test_incomplete_from_clause_is_invalid():
    analysis = analyze("SELECT * FROM")
    assert not analysis.valid
    assert analysis.error


def test_garbage_text_is_invalid():
    analysis = analyze("this is not sql at all !!!")
    assert not analysis.valid
    assert analysis.error


def test_syntax_errors_is_empty_for_valid_sql_in_the_dialect():
    assert syntax_errors("SELECT * FROM t FETCH FIRST 10 ROWS ONLY", "oracle") == []
    assert syntax_errors("SELECT * FROM t CROSS JOIN UNNEST(arr) AS u(x)", "trino") == []
    assert syntax_errors("   ", "oracle") == []


def test_syntax_errors_says_where_the_sql_stops_parsing():
    [problem] = syntax_errors("SELECT *\nFROM t\nWHERE (a = 1\nORDER BY 2", "oracle")
    assert (problem.line, problem.column) == (4, 1)
    assert problem.message == "Expected ) before 'ORDER BY'."


def test_syntax_errors_words_the_common_cases_for_people():
    assert syntax_errors("SELECT a FROM t WHERE", "oracle")[0].message == "The WHERE clause is incomplete."
    assert syntax_errors("SELECT * FRM t", "oracle")[0].message == "Unexpected 't'."
    assert syntax_errors("SELEC * FROM t", "oracle")[0].message == "Something is missing before 'FROM'."
    assert "never closed" in syntax_errors("SELECT * FROM t WHERE a = 'x", "oracle")[0].message


def test_syntax_errors_leaves_jinja_templates_alone():
    assert syntax_errors("SELECT * FROM t WHERE ds = '{{ ds }}' AND (", "oracle") == []
