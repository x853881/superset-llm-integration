"""Running the generated SQL and its reference once, comparing the rows,
and describing them for the correctness judge.
"""

import numbers
from collections import Counter
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Tuple

import pandas as pd

from judge.prompts import RESULT_ROWS
from metrics.sql_safety import check_sql_safety
from suite.schema_metadata import SQL_DIALECT, TableMetadata


# Why generated SQL wasn't run.
UNSAFE = "not executed: SQL failed the safety gate"


@dataclass
class QueryResults:
    """What the generated SQL and the golden's reference SQL returned. The
    test fetches them once (run_queries), so SQL Result Match, SQL Runs and
    the correctness judge all see the same rows. `actual` is None when the
    SQL wasn't run or failed, and `actual_problem` says which; `expected` is
    None without a reference, or when the SQL itself didn't run."""

    actual: Optional[pd.DataFrame] = None
    actual_problem: Optional[str] = None
    expected: Optional[pd.DataFrame] = None


def cut_off(frame: Optional[pd.DataFrame]) -> bool:
    """Whether SQL Lab stopped at its row limit (SQL_ROW_LIMIT) with rows
    left over, so `frame` holds only some of what the query returns."""
    return frame is not None and bool(frame.attrs.get("cut_off"))


def run_queries(
    client: Any,
    sql: str,
    reference_sql: Optional[str],
    metadata: Mapping[str, TableMetadata],
    dialect: str = SQL_DIALECT,
) -> QueryResults:
    """Runs the generated SQL through SQL Lab, unless it fails the safety
    gate, then the reference SQL if there is one. A reference query that
    fails, or returns more rows than SQL Lab fetches, raises: that's a
    problem with the golden, not the plugin."""
    if not check_sql_safety(sql, dialect, metadata).is_safe:
        return QueryResults(actual_problem=UNSAFE)
    try:
        actual = client.execute_sql(sql)
    except Exception as e:
        return QueryResults(actual_problem=f"failed to execute: {e}")
    if not reference_sql:
        return QueryResults(actual=actual)
    try:
        expected = client.execute_sql(reference_sql)
    except Exception as e:
        raise RuntimeError(f"the golden's reference SQL failed to run: {e}") from e
    if cut_off(expected):
        raise RuntimeError(
            f"the golden's reference SQL returns more than {_rows(len(expected))}, "
            "SQL Lab's row limit (SQL_ROW_LIMIT), so there's nothing complete to "
            "compare with"
        )
    return QueryResults(actual=actual, expected=expected)


###############################################
# Result comparison
###############################################


def _cell(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return value
    if isinstance(value, numbers.Number):
        return round(float(value), 6)
    # Padding is not a difference: TO_CHAR(week, 'Month') pads names to 9.
    return str(value).strip()


def _matched(value: Any) -> Any:
    """`_cell`, with NULL compared as 0: a total of nothing shown as 0 rather
    than NULL makes no difference to the person reading it. A rewrite whose
    NULL changes which rows come back still fails, on the rows."""
    cell = _cell(value)
    return 0.0 if cell is None else cell


def frames_equivalent(
    actual: pd.DataFrame, expected: pd.DataFrame
) -> Tuple[bool, str]:
    """Order-insensitive comparison of query results. Columns are matched by
    name when both frames have the same names (case-insensitive), otherwise
    by position so differing aliases don't fail an otherwise correct query.
    NULL and 0 count as the same value (`_matched`)."""
    if actual.shape != expected.shape:
        return False, f"shape {actual.shape} != expected {expected.shape}"
    a_cols = [str(c).lower() for c in actual.columns]
    e_cols = [str(c).lower() for c in expected.columns]
    if sorted(a_cols) == sorted(e_cols) and len(set(e_cols)) == len(e_cols):
        actual = actual.set_axis(a_cols, axis=1)[e_cols]

    def rows(df: pd.DataFrame) -> Counter:
        return Counter(
            tuple(_matched(v) for v in row)
            for row in df.itertuples(index=False, name=None)
        )

    got, want = rows(actual), rows(expected)
    if got == want:
        return True, ""
    missing = list((want - got).elements())[:5]
    unexpected = list((got - want).elements())[:5]
    return False, f"rows differ; missing: {missing}; unexpected: {unexpected}"


def compare_results(results: QueryResults) -> Tuple[bool, str]:
    """frames_equivalent, except that SQL cut off at the row limit never
    matches: run_queries makes sure the reference wasn't, so the SQL returns
    more rows than the reference, and comparing only the first ones could
    pass it."""
    if cut_off(results.actual):
        return False, (
            f"the SQL returns {describe_count(results.actual)}; the reference returns "
            f"{_rows(len(results.expected))}"
        )
    return frames_equivalent(results.actual, results.expected)


###############################################
# What the queries returned, for the judge
###############################################


def describe_results(results: QueryResults, rows: int = RESULT_ROWS) -> str:
    """What running the queries returned, for the correctness judge: row
    counts, columns, the first rows of each in the order the database
    returned them, and whether the rows match with order ignored (the same
    comparison as SQL Result Match)."""
    if results.actual is None:
        return (
            "Result of running the SQL on the database: the actual output was "
            f"not run ({results.actual_problem})."
        )
    lines = ["Result of running the SQL on the database:"]
    lines += _returned("the actual output", results.actual, rows)
    if results.expected is not None:
        lines += _returned(
            "the expected output (the reference SQL)", results.expected, rows
        )
        same, diff = compare_results(results)
        lines.append(
            f"With row order ignored, both return the same {_rows(len(results.actual))}."
            if same
            else f"With row order ignored, they return different rows: {diff}."
        )
    return "\n".join(lines)


def _rows(n: int) -> str:
    return f"{n} row{'' if n == 1 else 's'}"


def describe_count(frame: pd.DataFrame) -> str:
    if cut_off(frame):
        return f"more than {_rows(len(frame))} (SQL Lab's row limit)"
    return _rows(len(frame))


def _returned(label: str, frame: pd.DataFrame, rows: int) -> List[str]:
    shown = min(rows, len(frame))
    head = f"Running {label} returned {describe_count(frame)}"
    if not shown:
        return [f"{head}, with columns {', '.join(str(c) for c in frame.columns)}."]
    which = f"the first {shown}" if shown < len(frame) else "all of them"
    lines = [f"{head}; {which}, in the order returned:"]
    lines.append("  " + " | ".join(str(c) for c in frame.columns))
    for row in frame.head(shown).itertuples(index=False, name=None):
        lines.append("  " + " | ".join(_shown(v) for v in row))
    return lines


def _shown(value: Any) -> str:
    cell = _cell(value)
    if cell is None:
        return "NULL"
    if isinstance(cell, float) and cell.is_integer():
        cell = int(cell)
    text = str(cell)
    return text if len(text) <= 40 else text[:37] + "..."
