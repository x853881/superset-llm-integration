"""The examples database, reached through psql in its container instead of
through SQL Lab: no time limit, and Superset stays free. TIMING_VIA=psql
times queries this way (plugin/client.py), and record_expected_results.py
runs each accelerate original in full this way.

Runs on this machine, not in Docker: it needs the docker command.

    EXAMPLES_DB_CONTAINER   the examples database's container (superset-db-1)
    EXAMPLES_DB_USER/NAME   who to connect as, and the database (examples)
"""

import os
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional

from metrics.expected_result import fingerprint_sql

CONTAINER = os.getenv("EXAMPLES_DB_CONTAINER", "superset-db-1")
USER = os.getenv("EXAMPLES_DB_USER", "examples")
DATABASE = os.getenv("EXAMPLES_DB_NAME", "examples")


def psql(sql: str) -> List[str]:
    """The one row `sql` returns, as its values, run through psql in the
    examples database's container. Raises RuntimeError with psql's message."""
    out = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", USER, "-d", DATABASE,
         "-X", "-q", "-A", "-t", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True,
    )
    if out.returncode:
        raise RuntimeError(out.stderr.strip())
    return out.stdout.strip().split("\t")


@dataclass
class FullRun:
    """One run of a query in full: how long it took, and its row count and
    checksum; or why it failed."""

    ms: Optional[float] = None
    row_count: Optional[int] = None
    checksum: Optional[str] = None
    error: Optional[str] = None


def run_in_full(sql: str) -> FullRun:
    """Run `sql` once in full, read-only, timing it."""
    run = FullRun()
    start = time.monotonic()
    try:
        rows, checksum = psql("SET TRANSACTION READ ONLY; " + fingerprint_sql(sql))
    except RuntimeError as e:
        run.error = str(e)[:500]
        return run
    run.ms = (time.monotonic() - start) * 1000
    run.row_count, run.checksum = int(rows), checksum
    return run
