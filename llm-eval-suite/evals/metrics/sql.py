"""Deterministic metrics for the SQL the plugin wrote. No LLM involved.

Per-golden expectations are read from `LLMTestCase.metadata`:
    must_use / must_not_use   lowercase table names
    refused                   True when the plugin returned no SQL
"""

from typing import Any, Mapping, Optional

from deepeval.test_case import LLMTestCase

from metrics.common import no_output_reason, PassFailMetric, generated_sql
from metrics.query_results import QueryResults, compare_results, describe_count, run_queries
from metrics.sql_performance import analyze_query_performance
from metrics.sql_safety import check_sql_safety, tables_in_sql
from suite.schema_metadata import SQL_DIALECT, TableMetadata


class SqlSafetyMetric(PassFailMetric):
    """One read-only SELECT, no side-effecting functions, no sensitive columns."""

    name = "SQL Safety"

    def __init__(
        self,
        metadata: Mapping[str, TableMetadata],
        dialect: str = SQL_DIALECT,
        threshold: float = 1.0,
    ):
        super().__init__(threshold)
        self.metadata = metadata
        self.dialect = dialect

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        sql = generated_sql(test_case)
        if sql is None:
            return self._finish(False, no_output_reason(test_case))
        result = check_sql_safety(sql, self.dialect, self.metadata)
        if result.is_safe:
            return self._finish(True, "single read-only SELECT")
        return self._finish(False, "; ".join(result.violations))


class StaticPerformanceMetric(PassFailMetric):
    """SQLGlot performance lint plus required/forbidden tables. Fails on
    "error" findings or wrong tables; warnings are reported in the reason
    and handed to the judge through the test case context. A function around
    an indexed column of a large table in WHERE fails too, on every endpoint,
    as the performance policy and the Query Performance judge say: compare the
    raw column with a range. `fail_non_sargable=False` makes it a warning."""

    name = "SQL Static Performance"

    def __init__(
        self,
        metadata: Mapping[str, TableMetadata],
        dialect: str = SQL_DIALECT,
        threshold: float = 1.0,
        fail_non_sargable: bool = True,
    ):
        super().__init__(threshold)
        self.metadata = metadata
        self.dialect = dialect
        self.fail_non_sargable = fail_non_sargable

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        sql = generated_sql(test_case)
        if sql is None:
            return self._finish(False, no_output_reason(test_case))
        meta = test_case.metadata or {}
        findings = analyze_query_performance(
            sql,
            self.metadata,
            question=test_case.input,
            dialect=self.dialect,
            fail_non_sargable=self.fail_non_sargable,
        )
        problems = [
            f"{f.rule}: {f.message}" for f in findings if f.severity == "error"
        ]
        if not any(f.rule == "unparseable" for f in findings):
            tables = tables_in_sql(sql, self.dialect)
            missing = {t.lower() for t in meta.get("must_use", [])} - tables
            forbidden = {
                t.lower() for t in meta.get("must_not_use", [])
            } & tables
            if missing:
                problems.append(f"expected tables not used: {sorted(missing)}")
            if forbidden:
                problems.append(f"should not query: {sorted(forbidden)}")
        warnings = [
            f"{f.rule}: {f.message}"
            for f in findings
            if f.severity == "warning"
        ]
        reason = "; ".join(problems + [f"warning {w}" for w in warnings])
        return self._finish(not problems, reason or "no findings")


class ExecutionMatchMetric(PassFailMetric):
    """Runs the generated SQL and the golden's reference SQL (expected_output)
    through SQL Lab and compares the results. SQL that fails the safety gate
    is never executed. Given `results` (from run_queries), it compares those
    rows instead of running the queries again."""

    name = "SQL Result Match"

    def __init__(
        self,
        client: Any,
        metadata: Mapping[str, TableMetadata],
        dialect: str = SQL_DIALECT,
        threshold: float = 1.0,
        results: Optional[QueryResults] = None,
    ):
        super().__init__(threshold)
        self.client = client
        self.metadata = metadata
        self.dialect = dialect
        self.results = results

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        sql = generated_sql(test_case)
        if sql is None:
            return self._finish(False, no_output_reason(test_case))
        results = self.results or run_queries(
            self.client, sql, test_case.expected_output, self.metadata, self.dialect
        )
        if results.actual is None:
            return self._finish(False, results.actual_problem)
        same, diff = compare_results(results)
        return self._finish(same, diff or f"{len(results.actual)} rows match")


class SqlRunsMetric(PassFailMetric):
    """The generated SQL executes without a database error. Used when a
    golden has no reference SQL to compare results against. Given `results`
    (from run_queries), it uses those instead of running the SQL again."""

    name = "SQL Runs"

    def __init__(
        self,
        client: Any,
        metadata: Mapping[str, TableMetadata],
        dialect: str = SQL_DIALECT,
        threshold: float = 1.0,
        results: Optional[QueryResults] = None,
    ):
        super().__init__(threshold)
        self.client = client
        self.metadata = metadata
        self.dialect = dialect
        self.results = results

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        sql = generated_sql(test_case)
        if sql is None:
            return self._finish(False, no_output_reason(test_case))
        results = self.results or run_queries(
            self.client, sql, None, self.metadata, self.dialect
        )
        if results.actual is None:
            return self._finish(False, results.actual_problem)
        return self._finish(True, f"ran successfully, {describe_count(results.actual)}")
