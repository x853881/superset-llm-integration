"""Record the result each accelerate golden's original query returns, so
Same Rows as Original can check a rewrite without running the original
(see metrics/expected_result.py). Writes `expected_result` into each
golden: the row count and checksum, the row count of every table the query
reads (so the suite notices when the data changes), how long the original
took and the date.

Each original runs in full with no time limit, through psql in Superset's
Postgres container (suite/examples_db.py), so even one that takes hours can
be recorded.

Record only goldens whose original takes longer than TIMING_TIMEOUT_S (and
mark them "execute": false). One that finishes within it is better left to
SQL Result Match, which runs the original each time but shows which rows
differ and gives the judge a summary of them.

    python evals/record_expected_results.py NAME [NAME]  # these goldens
    python evals/record_expected_results.py              # re-record every golden
                                                         # that has a result already
                                                                 # (after reloading the data)

EXAMPLES_DB_CONTAINER (default superset-db-1), EXAMPLES_DB_USER and
EXAMPLES_DB_NAME (both default examples) say where the examples database is.
"""

import datetime
import json
import sys
from typing import List

from metrics.expected_result import row_counts_sql, tables_read
from suite.dataset import golden_files
from suite.examples_db import psql, run_in_full


def record(golden: dict) -> dict:
    sql = golden["input"]
    tables = tables_read(sql)
    counts = psql(row_counts_sql(tables)) if tables else []
    run = run_in_full(sql)
    if run.error:
        raise RuntimeError(run.error)
    print(f"  {run.row_count:,} rows in {run.ms / 1000:,.1f}s")
    return {
        "row_count": run.row_count,
        "checksum": run.checksum,
        "tables": {t: int(n) for t, n in zip(tables, counts)},
        "original_ms": round(run.ms, 1),
        "recorded": datetime.date.today().isoformat(),
    }


def main(names: List[str]) -> None:
    found = set()
    for path in golden_files():
        goldens = json.loads(path.read_text())
        for g in goldens:
            meta = g.get("additional_metadata") or {}
            if meta.get("endpoint") != "accelerate":
                continue
            if names and g["name"] not in names:
                continue
            if not names and "expected_result" not in meta:
                continue
            found.add(g["name"])
            print(g["name"])
            meta["expected_result"] = record(g)
            # Saved after each golden, so hours of recording aren't lost to
            # a failure in the last one.
            path.write_text(json.dumps(goldens, indent=2, ensure_ascii=False) + "\n")
    missing = set(names) - found
    if missing:
        raise SystemExit(f"No accelerate golden named: {', '.join(sorted(missing))}")


def parse_args(args: List[str]) -> List[str]:
    """The golden names; this script takes no options."""
    for arg in args:
        if arg.startswith("-"):
            raise SystemExit(f"Unknown option {arg}: see the top of this file")
    return list(args)


if __name__ == "__main__":
    main(parse_args(sys.argv[1:]))
