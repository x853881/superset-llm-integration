"""How deep in their table lists NL->SQL answers found the tables they used, from the plugin's
telemetry - for choosing how long those lists need to be.

    python evals/table_ranking.py llm_calls.jsonl [more files...] [--json]

Reads the plugin's log: the JSON lines of NLSQL_LLM_LOG_FILE, or log text with "nlsql_request {...}"
and "feedback {...}" lines in it. Each NL->SQL request's tableRanking attribute (the plugin's
table_ranking.py) gives, per stage that ordered the tables, how many it listed and the ranks the
next stage kept, or the ranks the SQL used. Feedback on a request, joined by its trace, says what
became of its SQL: a thumbs up is "accepted", a thumbs down "rejected"; the latest of a user's
changes of mind counts.

For each strategy and stage it prints:

- the share of requests whose deepest kept or used rank is at most k, for each k. Where that
  reaches, say, 95% is the list length to use. Ranks are absolute, not divided by the list's length:
  the decision is how long a list to give.
- the prompt wasted: tables listed but not used, per request.
- the average of 1/rank of the tables used: how near the top they sit.
- how often the SQL read a table no list offered (usedNotListed).

All of it for every request, then split by outcome. Only "accepted" requests (and "ran", once the
panel reports runs) are evidence of good ordering: SQL that was rejected may have used the wrong
tables. A table needed beyond a list's end never shows as a rank, only as a failure or in
usedNotListed.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Optional

GOOD = ("ran", "accepted")
SHARES = (0.5, 0.8, 0.9, 0.95, 0.99)


@dataclass
class Request:
    trace_id: Optional[str]
    ranking: dict[str, Any]
    outcome: str


def read_lines(lines: Iterable[str]) -> list[tuple[str, dict[str, Any]]]:
    """(kind, record) for each request or feedback line, from JSON lines or log text."""
    found = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("kind") in ("nlsql_request", "feedback"):
                found.append((record["kind"], record))
            continue
        for kind in ("nlsql_request", "feedback"):
            marker = f"{kind} {{"
            at = line.find(marker)
            if at >= 0:
                try:
                    found.append((kind, json.loads(line[at + len(kind) + 1:])))
                except ValueError:
                    pass
                break
    return found


def requests(records: list[tuple[str, dict[str, Any]]]) -> list[Request]:
    """Every request that recorded a tableRanking, with its outcome settled by feedback on it."""
    latest: dict[str, dict[str, Any]] = {}
    for kind, record in records:
        if kind == "feedback" and record.get("target") == "sql" and record.get("traceId"):
            key = record.get("feedbackId") or record["traceId"]
            if key not in latest or str(record.get("ts", "")) >= str(latest[key].get("ts", "")):
                latest[key] = record
    verdict = {record["traceId"]: "accepted" if record.get("rating") == "up" else "rejected" for record in latest.values()}
    out = []
    for kind, record in records:
        ranking = record.get("tableRanking") if kind == "nlsql_request" else None
        if not isinstance(ranking, dict):
            continue
        trace = record.get("traceId")
        outcome = verdict.get(trace) or ranking.get("outcome") or "unknown"
        out.append(Request(trace, ranking, outcome))
    return out


def deepest(stage: dict[str, Any]) -> Optional[int]:
    ranks = stage.get("used", stage.get("kept")) or []
    return max(ranks) if ranks else None


def summarise(group: list[Request]) -> dict[str, Any]:
    """Per strategy and stage: how many requests, the share at each depth, waste, 1/rank, misses."""
    out: dict[str, Any] = {}
    for request in group:
        strategy = request.ranking.get("strategy") or "?"
        stages = request.ranking.get("stages") or []
        for index, stage in enumerate(stages):
            entry = out.setdefault(strategy, {}).setdefault(stage.get("stage") or str(index), {
                "requests": 0, "deepest": [], "listed": [], "wasted": [], "reciprocal": [], "notListed": [],
            })
            entry["requests"] += 1
            entry["listed"].append(int(stage.get("listed") or 0))
            depth = deepest(stage)
            if depth is not None:
                entry["deepest"].append(depth)
            if "used" in stage:
                used = stage.get("used") or []
                entry["wasted"].append(int(stage.get("listed") or 0) - len(used))
                entry["reciprocal"].extend(1 / rank for rank in used)
                entry["notListed"].append(int(request.ranking.get("usedNotListed") or 0))
    return {strategy: {name: _figures(entry) for name, entry in stages.items()} for strategy, stages in out.items()}


def _figures(entry: dict[str, Any]) -> dict[str, Any]:
    depths = sorted(entry["deepest"])
    longest = max(entry["listed"] or [0])
    curve = {k: sum(1 for depth in depths if depth <= k) / len(depths) for k in range(1, longest + 1)} if depths else {}
    figures: dict[str, Any] = {
        "requests": entry["requests"],
        "withARank": len(depths),
        "longestList": longest,
        "depthFor": {f"{share:.0%}": next((k for k, reached in curve.items() if reached >= share), None) for share in SHARES},
        "shareAtDepth": curve,
    }
    if entry["wasted"]:
        figures["wastedMean"] = statistics.mean(entry["wasted"])
        figures["wastedMedian"] = statistics.median(entry["wasted"])
    if entry["reciprocal"]:
        figures["meanReciprocalRank"] = statistics.mean(entry["reciprocal"])
    if entry["notListed"]:
        figures["notListedShare"] = sum(1 for count in entry["notListed"] if count) / len(entry["notListed"])
    return figures


def report(found: list[Request]) -> dict[str, Any]:
    groups = {"all": found, "ran or accepted": [r for r in found if r.outcome in GOOD]}
    for outcome in sorted({r.outcome for r in found}):
        groups[outcome] = [r for r in found if r.outcome == outcome]
    return {name: summarise(group) for name, group in groups.items() if group}


def _print(result: dict[str, Any]) -> None:
    marks = (1, 2, 3, 5, 8, 10, 15, 20, 30, 50, 100)
    for group, strategies in result.items():
        print(f"== {group} ==")
        for strategy, stages in strategies.items():
            for stage, figures in stages.items():
                print(f"  {strategy} / {stage}: {figures['requests']} requests, {figures['withARank']} with a rank, lists up to {figures['longestList']}")
                reached = ", ".join(f"{share} by {k}" for share, k in figures["depthFor"].items() if k is not None)
                if reached:
                    print(f"    deepest rank: {reached}")
                curve = figures["shareAtDepth"]
                shown = [f"≤{k}: {curve[k]:.0%}" for k in marks if k in curve]
                if shown:
                    print(f"    share at depth: {'  '.join(shown)}")
                if "wastedMean" in figures:
                    print(f"    tables listed but not used: mean {figures['wastedMean']:.1f}, median {figures['wastedMedian']:g}")
                if "meanReciprocalRank" in figures:
                    print(f"    mean 1/rank of tables used: {figures['meanReciprocalRank']:.2f}")
                if "notListedShare" in figures:
                    print(f"    requests reading a table no list offered: {figures['notListedShare']:.0%}")
        print()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+", help="the plugin's log: NLSQL_LLM_LOG_FILE, or log text")
    parser.add_argument("--json", action="store_true", help="print the figures as JSON")
    args = parser.parse_args(argv)
    records: list[tuple[str, dict[str, Any]]] = []
    for path in args.files:
        with open(path, encoding="utf-8") as file:
            records.extend(read_lines(file))
    found = requests(records)
    if not found:
        print("No requests with a tableRanking in those files.", file=sys.stderr)
        return 1
    result = report(found)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
