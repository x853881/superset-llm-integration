"""How far down the plugin's table ranking each request's tables are: recall at k, for choosing how
wide the shortlist the picking call is offered must be, and whether the closest tables alone are
enough.

    python evals/shortlist_recall.py [requests.json] [--k 8,20,40,80] [--json]

A request file is a JSON list. Each entry has the request as "request" or "input", and the tables
its answer needs as "tables" (schema.table), or - as in a generate-sql golden file - reference SQL
as "expected_output", whose tables are read from it. Without a file it reads the suite's
generate-sql goldens.

Each request goes to the plugin's /table-shortlist, which ranks the suite's database's tables the
way its prompts do (by embedding when the database was embedded, else by words) - as the suite's
user, who needs a role that may choose how tables are picked. A request's depth is the rank of the
deepest table it needs; recall at k is the share of requests with every table in the top k. A
table missing from the ranking altogether (not read, or not visible to the user) is counted apart.

Ticket 9's curves (table_ranking.py) answer the same question from real use, without a marked-up
request set; this needs one, but works before anyone has used the plugin.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

KS = (8, 20, 40, 80)


@dataclass
class Depth:
    name: str
    needed: list[str]
    #: The rank of the deepest table needed, if every one was ranked.
    deepest: Optional[int]
    missing: list[str]
    strategy: Optional[str]


def needed_tables(entry: dict[str, Any], dialect: str) -> list[str]:
    if entry.get("tables"):
        return sorted({str(table).lower() for table in entry["tables"]})
    if entry.get("expected_output"):
        from metrics.expected_result import tables_read

        return sorted({table.lower() for table in tables_read(entry["expected_output"], dialect)})
    return []


def depth(name: str, needed: list[str], ranked: list[str], strategy: Optional[str]) -> Depth:
    position = {table.lower(): rank for rank, table in enumerate(ranked, start=1)}
    missing = [table for table in needed if table not in position]
    ranks = [position[table] for table in needed if table in position]
    return Depth(name, needed, None if missing or not ranks else max(ranks), missing, strategy)


def measure(entries: Iterable[dict[str, Any]], shortlist: Callable[[str], dict[str, Any]], dialect: str) -> list[Depth]:
    out = []
    for index, entry in enumerate(entries):
        text = entry.get("request") or entry.get("input")
        needed = needed_tables(entry, dialect)
        if not text or not needed:
            continue
        result = shortlist(str(text))
        if result.get("status") != "ready":
            raise SystemExit("The plugin knows nothing about the suite's database yet: read it in SQL Lab first.")
        out.append(depth(str(entry.get("name") or index), needed, result.get("tables") or [], result.get("strategy")))
    return out


def report(depths: list[Depth], ks: Iterable[int] = KS) -> dict[str, Any]:
    ranked = [d.deepest for d in depths if d.deepest is not None]
    return {
        "requests": len(depths),
        "strategies": sorted({d.strategy for d in depths if d.strategy}),
        "recallAt": {k: sum(1 for d in ranked if d <= k) / len(depths) if depths else 0.0 for k in ks},
        "medianDepth": statistics.median(ranked) if ranked else None,
        "missingTables": sum(1 for d in depths if d.missing),
        "deepest": sorted(
            ({"name": d.name, "depth": d.deepest, "missing": d.missing} for d in depths),
            key=lambda row: (row["depth"] is not None, -(row["depth"] or 0)),
        )[:10],
    }


def _print(result: dict[str, Any]) -> None:
    print(f"{result['requests']} requests, ranked by {', '.join(result['strategies']) or '?'}")
    print("recall at k: " + "  ".join(f"{k}: {share:.0%}" for k, share in result["recallAt"].items()))
    if result["medianDepth"] is not None:
        print(f"median depth of the deepest table needed: {result['medianDepth']:g}")
    if result["missingTables"]:
        print(f"{result['missingTables']} requests need a table the ranking hasn't got (not read, or not visible)")
    print("deepest:")
    for row in result["deepest"]:
        where = f"rank {row['depth']}" if row["depth"] is not None else f"missing {', '.join(row['missing'])}"
        print(f"  {row['name']}: {where}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("file", nargs="?", help="the requests: JSON, as above (default: the generate-sql goldens)")
    parser.add_argument("--k", default=",".join(map(str, KS)), help="the depths to give recall at")
    parser.add_argument("--json", action="store_true", help="print the figures as JSON")
    args = parser.parse_args(argv)
    from plugin.client import SupersetClient
    from suite.config import SuiteConfig

    path = Path(args.file) if args.file else Path(__file__).parent / "goldens" / "generate-sql.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    ks = [int(k) for k in args.k.split(",") if k.strip()]
    config = SuiteConfig.from_env()
    client = SupersetClient(config)
    depths = measure(entries, lambda text: client.table_shortlist(text, limit=max(ks) * 4), config.sql_dialect)
    if not depths:
        print(f"No requests with the tables they need in {path}.", file=sys.stderr)
        return 1
    result = report(depths, ks)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
