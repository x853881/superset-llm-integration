"""Same Rows as Original: accelerate's rewrite returns exactly the rows the
original query returns, checked against a fingerprint of the original's
result recorded once by record_expected_results.py (`expected_result` in the
golden's additional_metadata). No judge involved.

The fingerprint is the row count and an MD5 of every row as text, sorted:
row order doesn't matter, but duplicates, column order and how each value
prints do. Postgres works it out, so a result of any size comes back as one
row, nothing is cut off at SQL_ROW_LIMIT, and the original never has to run
during the suite. PostgreSQL only.
"""

from typing import Any, Dict, List, Mapping

from deepeval.test_case import LLMTestCase
from sqlglot import exp, parse_one

from metrics.common import PassFailMetric, generated_sql, no_output_reason
from metrics.sql_safety import check_sql_safety, strip_sql_fences
from suite.schema_metadata import SQL_DIALECT, TableMetadata

# Fixed, so a value prints the same way whichever session runs the query.
SESSION_SETTINGS = (
    "SET DateStyle TO ISO, MDY; SET TimeZone TO 'UTC'; SET extra_float_digits TO 1; "
)


def fingerprint_sql(sql: str) -> str:
    """One row for `sql`'s result: row_count, and checksum ('' for no rows)."""
    body = strip_sql_fences(sql).strip().rstrip(";")
    return (
        SESSION_SETTINGS
        + "SELECT count(*) AS row_count, "
        "coalesce(md5(string_agg(t::text, E'\\n' ORDER BY t::text)), '') AS checksum "
        f"FROM (\n{body}\n) t"
    )


def tables_read(sql: str, dialect: str = SQL_DIALECT) -> List[str]:
    """The schema-qualified tables `sql` reads; a CTE's name has no schema,
    so it isn't one."""
    tables = parse_one(strip_sql_fences(sql), dialect).find_all(exp.Table)
    return sorted({f"{t.db}.{t.name}" for t in tables if t.db})


def row_counts_sql(tables: List[str]) -> str:
    """One row: each table's row count, in a column named after it."""
    return "SELECT " + ", ".join(
        f'(SELECT count(*) FROM {t}) AS "{t}"' for t in tables
    )


class SameRowsAsOriginalMetric(PassFailMetric):
    """For accelerate goldens with `expected_result`: the rewrite's result has
    the original's row count and checksum. A table whose row count has
    changed since the result was recorded is an error, not a failure: the
    recorded result no longer says what the original returns."""

    name = "Same Rows as Original"

    def __init__(
        self,
        client: Any,
        metadata: Mapping[str, TableMetadata],
        expected: Mapping[str, Any],
        dialect: str = SQL_DIALECT,
        timeout_s: float = 25.0,
        threshold: float = 1.0,
    ):
        super().__init__(threshold)
        self.client = client
        self.metadata = metadata
        self.expected = expected
        self.dialect = dialect
        self.timeout_s = timeout_s

    def _check_data(self) -> None:
        recorded: Dict[str, int] = self.expected.get("tables") or {}
        if not recorded:
            return
        row = self.client.execute_sql(row_counts_sql(list(recorded))).iloc[0]
        now = dict(zip(recorded, (int(n) for n in row)))
        changed = [
            f"{t}: {n:,} rows then, {now[t]:,} now"
            for t, n in recorded.items()
            if now[t] != n
        ]
        if changed:
            raise ValueError(
                "expected_result was recorded on different data ("
                + "; ".join(changed)
                + "): re-run python evals/record_expected_results.py"
            )

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        sql = generated_sql(test_case)
        if sql is None:
            return self._finish(False, no_output_reason(test_case))
        if not check_sql_safety(sql, self.dialect, self.metadata).is_safe:
            return self._finish(False, "not run: SQL failed the safety gate")
        self._check_data()
        statement = (
            f"SET statement_timeout TO '{int(self.timeout_s * 1000)}ms'; "
            + fingerprint_sql(sql)
        )
        try:
            row = self.client.execute_sql(statement).iloc[0]
        except Exception as e:
            if "statement timeout" in str(e):
                return self._finish(
                    False, f"the rewrite did not finish within {self.timeout_s:g}s"
                )
            return self._finish(False, f"the rewrite failed to run: {e}")
        return self._compare(int(row.iloc[0]), row.iloc[1])

    def _compare(self, rows: int, checksum: str) -> float:
        want_rows = self.expected["row_count"]
        if rows != want_rows:
            return self._finish(
                False, f"{rows:,} rows; the original returns {want_rows:,}"
            )
        if checksum != self.expected["checksum"]:
            return self._finish(
                False,
                f"{rows:,} rows, as the original returns, but not the same rows: "
                "values differ, or the columns are in a different order",
            )
        return self._finish(True, f"the same {rows:,} rows as the original")
