"""Metrics that time real runs of the queries on the database. Neither
involves the judge.

    Faster Than Original   accelerate's rewrite against its input query
                           (`metadata["original_sql"]`)
    Speed vs Reference     other SQL against the golden's reference SQL

Given the rows the SQL and the other query returned (`results`, from
run_queries), a SQL whose rows differ fails without being timed: being fast
at the wrong answer is no pass, and an empty result takes no time at all.
"""

from typing import Any, Mapping, Optional, Tuple

import pandas as pd
from deepeval.test_case import LLMTestCase

from metrics.common import no_output_reason, PassFailMetric, generated_sql
from metrics.query_results import QueryResults, compare_results
from metrics.sql_safety import check_sql_safety, same_query
from suite import locks
from suite.schema_metadata import SQL_DIALECT, TableMetadata


class _TimedMetric(PassFailMetric):
    """Times the generated SQL against another query on the database.

    Each query is run once to warm the cache, then `runs` more times, and the
    medians are compared. A query still running after `timeout_s` is cancelled
    and counts as taking at least that long. Pass or fail depends only on the
    percentage difference, never on a flat number of milliseconds. SQL that
    fails the safety gate is never run, nor SQL whose rows differ from the
    other query's in `results`."""

    def __init__(
        self,
        client: Any,
        metadata: Mapping[str, TableMetadata],
        dialect: str = SQL_DIALECT,
        runs: int = 3,
        timeout_s: float = 25.0,
        threshold: float = 1.0,
        results: Optional[QueryResults] = None,
    ):
        super().__init__(threshold)
        self.client = client
        self.metadata = metadata
        self.dialect = dialect
        self.runs = runs
        self.timeout_s = timeout_s
        self.results = results

    def _median_ms(self, sql: str) -> Optional[float]:
        """None when the query hit the time limit, which is then not retried.
        The warm-up and every run hold the database lock together, so no
        other query the suite sends runs in between (suite/locks.py)."""
        with locks.timing():
            if self.client.time_query(sql, self.timeout_s) is None:  # warm-up
                return None
            times = []
            for _ in range(self.runs):
                took = self.client.time_query(sql, self.timeout_s)
                if took is None:
                    return None
                times.append(took)
        return float(pd.Series(times).median())

    def _wrong_rows(self) -> str:
        """Why the SQL fails untimed: its rows differ from the other query's,
        or it failed to run. Empty when `results` match, or weren't given."""
        r = self.results
        if r is None:
            return ""
        if r.actual is None:
            return f"not timed: {r.actual_problem}"
        if r.expected is None:
            return ""
        same, diff = compare_results(r)
        return "" if same else f"not timed: the rows are wrong, so its speed doesn't count ({diff})"

    def _times(
        self,
        test_case: LLMTestCase,
        other_sql: Optional[str],
        other: str,
        label: str = "the SQL",
    ) -> Tuple[Optional[float], Optional[float], str]:
        """(generated SQL's ms, other query's ms, reason it fails without a
        comparison). The generated SQL is timed first; if it hits the time
        limit it fails whatever the other query does, so that one isn't run.
        A None time for the other query means it hit the time limit."""
        if not other_sql:
            return None, None, f"no {other} to compare with"
        sql = generated_sql(test_case)
        if sql is None:
            return None, None, no_output_reason(test_case)
        if not check_sql_safety(sql, self.dialect, self.metadata).is_safe:
            return None, None, "not run: SQL failed the safety gate"
        wrong = self._wrong_rows()
        if wrong:
            return None, None, wrong
        try:
            took = self._median_ms(sql)
        except Exception as e:
            return None, None, f"{label} failed to run: {e}"
        if took is None:
            return None, None, f"{label} did not finish within {self.timeout_s:g}s"
        # A query of the golden's own that can't run is a problem with the
        # golden, so let it surface as a metric error.
        return took, self._median_ms(other_sql), ""


class FasterThanOriginalMetric(_TimedMetric):
    """For accelerate: how much of the original query's time
    (`metadata["original_sql"]`) the rewrite saves.

    Scored, not just passed or failed: the score is the share of the time
    saved, 1 - rewrite / original, so a rewrite that takes almost no time
    scores close to 1, one as slow as the original scores 0, and a slower one
    scores 0 too. 10 minutes down to 8 scores 0.2; an hour down to 3 minutes
    scores 0.95. It passes at `threshold` or more (0.5 = twice as fast). An
    original too slow to finish is compared against the time limit, so its
    score is a lower bound."""

    name = "Faster Than Original"

    def __init__(
        self,
        client: Any,
        metadata: Mapping[str, TableMetadata],
        dialect: str = SQL_DIALECT,
        runs: int = 3,
        timeout_s: float = 25.0,
        threshold: float = 0.5,
        results: Optional[QueryResults] = None,
    ):
        super().__init__(client, metadata, dialect, runs, timeout_s, threshold, results)

    def _judge(
        self, before_ms: float, rewrite_ms: float, times: str, at_least: bool = False
    ) -> float:
        saving = 1 - rewrite_ms / before_ms if before_ms else 0.0
        if saving >= 0:
            speedup = before_ms / rewrite_ms if rewrite_ms else float("inf")
            change = f"{saving:.0%} of the time saved, {speedup:,.1f}x as fast"
        else:
            change = f"{-saving:.0%} slower"
        if at_least:
            change = "at least " + change
        self.score = max(saving, 0.0)
        self.success = self.score >= self.threshold
        self.reason = (
            f"{times} ({change}; needs at least {self.threshold:.0%} saved, "
            f"{1 / (1 - self.threshold):,.1f}x as fast)"
            if self.threshold < 1
            else f"{times} ({change})"
        )
        return self.score

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        original = (test_case.metadata or {}).get("original_sql")
        rewrite = generated_sql(test_case)
        if original and rewrite and same_query(rewrite, original, self.dialect):
            return self._finish(
                False,
                "the rewrite is the original query unchanged (ignoring layout), "
                "so there is nothing to time",
            )
        rewrite_ms, original_ms, problem = self._times(
            test_case, original, "original query (original_sql)", "the rewrite"
        )
        if problem:
            return self._finish(False, problem)
        limit = f"within {self.timeout_s:g}s"
        if original_ms is None:
            # Compared against the time limit, so "at least" this much faster.
            return self._judge(
                self.timeout_s * 1000,
                rewrite_ms,
                f"original did not finish {limit}; rewrite {rewrite_ms:,.1f} ms",
                at_least=True,
            )
        return self._judge(
            original_ms,
            rewrite_ms,
            f"original {original_ms:,.1f} ms -> rewrite {rewrite_ms:,.1f} ms "
            f"(median of {self.runs} runs)",
        )


class SpeedVsReferenceMetric(_TimedMetric):
    """The generated SQL is not meaningfully slower than the golden's
    reference SQL, which is written as the query a good analyst would use.
    Catches SQL that returns the right answer the slow way, which would then
    need accelerating.

    Two steps: within `tolerance` (0.2 = 20%) slower passes. Beyond that, it
    fails only if it is also more than `min_slowdown_ms` slower, so tiny
    queries whose percentages are mostly timing jitter don't fail."""

    name = "Speed vs Reference"

    def __init__(
        self,
        client: Any,
        metadata: Mapping[str, TableMetadata],
        dialect: str = SQL_DIALECT,
        tolerance: float = 0.2,
        min_slowdown_ms: float = 500.0,
        runs: int = 3,
        timeout_s: float = 25.0,
        threshold: float = 1.0,
        results: Optional[QueryResults] = None,
    ):
        super().__init__(client, metadata, dialect, runs, timeout_s, threshold, results)
        self.tolerance = tolerance
        self.min_slowdown_ms = min_slowdown_ms

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        sql_ms, reference_ms, problem = self._times(
            test_case, test_case.expected_output, "reference SQL"
        )
        if problem:
            return self._finish(False, problem)
        limit = f"within {self.timeout_s:g}s"
        allowed = f"allowed up to {self.tolerance:.0%} slower"
        if reference_ms is None:
            return self._finish(
                True,
                f"reference did not finish {limit}; SQL {sql_ms:,.0f} ms",
            )
        times = f"SQL {sql_ms:,.1f} ms vs reference {reference_ms:,.1f} ms"
        if sql_ms <= reference_ms:
            saving = 1 - sql_ms / reference_ms if reference_ms else 0.0
            return self._finish(True, f"{times} ({saving:.0%} faster)")
        overhead = sql_ms / reference_ms - 1 if reference_ms else float("inf")
        slower = f"{times} ({overhead:.0%} slower; {allowed})"
        runs = f"median of {self.runs} runs"
        if overhead <= self.tolerance:
            return self._finish(True, f"{slower}, {runs}")
        extra_ms = sql_ms - reference_ms
        if extra_ms <= self.min_slowdown_ms:
            return self._finish(
                True,
                f"{slower}, but only {extra_ms:,.1f} ms slower, within the "
                f"{self.min_slowdown_ms:g} ms that counts as meaningful; {runs}",
            )
        return self._finish(
            False,
            f"{slower} and {extra_ms:,.1f} ms slower, more than the "
            f"{self.min_slowdown_ms:g} ms that counts as meaningful; {runs}",
        )
