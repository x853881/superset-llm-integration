"""Unit tests for evals/table_ranking.py: reading the plugin's telemetry, settling each request's
outcome from feedback, and the figures for choosing list lengths.

Run: pytest -m unit
"""

import json

import pytest

import table_ranking

pytestmark = pytest.mark.unit


def _request(trace, used, kept=None, listed=8, not_listed=0, outcome="unknown"):
    stages = []
    if kept is not None:
        stages.append({"stage": "embedding", "listed": 30, "kept": kept})
    stages.append({"stage": "prompt", "listed": listed, "used": used})
    strategy = "embedding" if kept is not None else "whole"
    return {
        "kind": "nlsql_request",
        "traceId": trace,
        "tableRanking": {"strategy": strategy, "stages": stages, "usedNotListed": not_listed, "outcome": outcome},
    }


def _feedback(trace, rating, ts, feedback_id=None, target="sql"):
    return {"kind": "feedback", "traceId": trace, "rating": rating, "ts": ts, "feedbackId": feedback_id or trace, "target": target}


def test_both_the_json_lines_and_the_log_text_are_read():
    line = json.dumps(_request("a", [1]))
    text = "2026-11-23 10:00:00 INFO nlsql_request " + json.dumps({k: v for k, v in _request("b", [2]).items() if k != "kind"})
    found = table_ranking.requests(table_ranking.read_lines([line, text, "something else", ""]))
    assert [r.trace_id for r in found] == ["a", "b"]


def test_feedback_settles_the_outcome_and_the_latest_change_of_mind_counts():
    records = table_ranking.read_lines(json.dumps(line) for line in [
        _request("a", [1]),
        _request("b", [1]),
        _request("c", [1], outcome="failed"),
        _feedback("a", "down", "2026-11-23T10:00:00"),
        _feedback("a", "up", "2026-11-23T10:01:00"),
        _feedback("b", "down", "2026-11-23T10:00:00"),
        _feedback("c", "up", "2026-11-23T10:00:00", target="explain-plan"),
    ])
    assert {r.trace_id: r.outcome for r in table_ranking.requests(records)} == {"a": "accepted", "b": "rejected", "c": "failed"}


def test_the_share_at_each_depth_gives_the_list_length_to_use():
    found = [
        table_ranking.Request(str(n), _request(str(n), used, kept)["tableRanking"], "accepted")
        for n, (used, kept) in enumerate([([1, 2], [2, 3]), ([1, 4], [2, 3, 5, 14]), ([2], [7]), ([1], [1])])
    ]
    figures = table_ranking.report(found)["all"]["embedding"]
    embedding, prompt = figures["embedding"], figures["prompt"]
    assert embedding["shareAtDepth"][3] == 0.5 and embedding["shareAtDepth"][14] == 1.0
    assert embedding["depthFor"]["95%"] == 14
    assert prompt["depthFor"]["50%"] == 2 and prompt["depthFor"]["95%"] == 4
    assert prompt["wastedMean"] == (6 + 6 + 7 + 7) / 4
    assert prompt["meanReciprocalRank"] == pytest.approx((1 + 1 / 2 + 1 + 1 / 4 + 1 / 2 + 1) / 6)


def test_requests_are_split_by_outcome_and_only_good_ones_count_as_evidence():
    found = [
        table_ranking.Request("a", _request("a", [1])["tableRanking"], "accepted"),
        table_ranking.Request("b", _request("b", [5], not_listed=1)["tableRanking"], "rejected"),
    ]
    result = table_ranking.report(found)
    assert result["all"]["whole"]["prompt"]["requests"] == 2
    assert result["ran or accepted"]["whole"]["prompt"]["requests"] == 1
    assert result["rejected"]["whole"]["prompt"]["notListedShare"] == 1.0


def test_it_prints_a_report_from_a_file(tmp_path, capsys):
    path = tmp_path / "llm_calls.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in [_request("a", [1, 3]), _feedback("a", "up", "t")]))
    assert table_ranking.main([str(path)]) == 0
    printed = capsys.readouterr().out
    assert "== ran or accepted ==" in printed and "whole / prompt: 1 requests" in printed
    assert table_ranking.main([str(tmp_path / "llm_calls.jsonl"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["all"]["whole"]["prompt"]["depthFor"]["50%"] == 3
