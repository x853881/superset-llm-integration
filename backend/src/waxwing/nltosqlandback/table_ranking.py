"""How deep in each ordered table list the tables an NL->SQL answer used were - for choosing how long
those lists need to be.

Each stage that orders a database's tables - an embedding match, a word match, the prompt itself -
is noted in a `Listing` as the request's prompt is built. Once the SQL is written, `record` turns it
into the request's ``tableRanking`` telemetry attribute: per stage, how many tables it listed and the
1-based ranks the next stage kept, or - for the last, the list the prompt gave the model - the ranks
of the tables the SQL reads. Ranks only, so the logs carry no table names; with debug logging on,
the names are logged too. ``llm-eval-suite/evals/table_ranking.py`` reads it back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from . import query_tables

logger = logging.getLogger(__name__)

SchemaTable = tuple[str, str]

#: The prompt had every table the user may see, in its own order.
WHOLE = "whole"
#: The prompt had the tables closest to the request by embedding.
EMBEDDING = "embedding"
#: The prompt had the tables sharing the most words with the request.
WORDS = "words"
#: The bundled semantic schema's embedding match.
SEMANTIC_SCHEMA = "semantic-schema"


@dataclass
class Listing:
    """The ordered table lists one request's prompt was built from, first to last; the last is what
    the prompt listed, in its order."""

    strategy: Optional[str] = None
    #: The table-selection option the request used (see ``table_selection``).
    option: Optional[str] = None
    stages: list[tuple[str, list[SchemaTable]]] = field(default_factory=list)

    def add(self, stage: str, tables: list[SchemaTable]) -> None:
        self.stages.append((stage, list(tables)))


def record(listing: Listing, sql: str, dialect: Optional[str], outcome: str) -> Optional[dict[str, Any]]:
    """The ``tableRanking`` attribute for SQL written from `listing`'s prompt: each stage's length,
    the ranks the next stage kept, the ranks of the tables `sql` reads in the last, and how many it
    reads that no list offered. `outcome` is what's known of the SQL when the request ends - "failed"
    when none was written, else "unknown" (feedback, joined by trace, says the rest)."""
    if listing.strategy is None or not listing.stages:
        return None
    stages: list[dict[str, Any]] = []
    for (name, tables), following in zip(listing.stages, listing.stages[1:]):
        ranks = {_lower(key): rank for rank, key in enumerate(tables, start=1)}
        kept = sorted({ranks[_lower(key)] for key in following[1] if _lower(key) in ranks})
        stages.append({"stage": name, "listed": len(tables), "kept": kept})
    name, last = listing.stages[-1]
    used, not_listed = _used_ranks(sql, dialect, last) if sql else ([], 0)
    stages.append({"stage": name, "listed": len(last), "used": used})
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "table ranking %s: %s",
            listing.strategy,
            "; ".join(f"{stage}: {', '.join(f'{s}.{t}' for s, t in tables[:50])}" for stage, tables in listing.stages),
        )
    out: dict[str, Any] = {"strategy": listing.strategy, "stages": stages, "usedNotListed": not_listed, "outcome": outcome}
    if listing.option:
        out["option"] = listing.option
    return out


def _lower(key: SchemaTable) -> SchemaTable:
    return (key[0].lower(), key[1].lower())


def _used_ranks(sql: str, dialect: Optional[str], listed: list[SchemaTable]) -> tuple[list[int], int]:
    """The ranks in `listed` of the tables `sql` reads - a name without a schema taking its best
    match - and how many of them `listed` hasn't got."""
    try:
        wanted = query_tables.tables_used(sql, dialect)
    except Exception as ex:  # noqa: BLE001 - telemetry mustn't fail the request
        logger.debug("could not parse the SQL for its tables: %s", ex)
        return [], 0
    ranks: set[int] = set()
    missing = 0
    lowered = [_lower(key) for key in listed]
    for schema, name in wanted:
        found = [rank for rank, (s, t) in enumerate(lowered, start=1) if t == name and (schema is None or s == schema)]
        if found:
            ranks.add(min(found))
        else:
            missing += 1
    return sorted(ranks), missing
