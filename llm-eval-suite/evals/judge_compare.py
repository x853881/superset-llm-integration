"""Re-judge a finished run with other judges, so several models grade the same
answers.

    python evals/judge_compare.py --run results/test_run_<ts>.json \\
        model-a model-b

The plugin isn't asked again. Each judged metric is rebuilt from what the run
saved (the question, the plugin's answer, the reference, the context, the
query results and the error explanation) and graded by each model you name,
through the suite's judge provider (JUDGE_PROVIDER and its other settings,
judge/model.py), under the criteria in judge/prompts.py as they are now. The context is as the run saved it, so
schema notes added since aren't in it. The run's own judgements are read from
the run, not redone, and come first.

Name the run's own judge too and it judges again, which shows what a change
to the criteria did: its saved judgements are then labelled "<model> (run)"
and come last, so agreement is with its new ones. --metric limits it to the
metrics whose names start with what you give:

    python evals/judge_compare.py --run results/test_run_<ts>.json \\
        --metric "SQL Correctness" --metric "Query Performance" \\
        the-runs-judge model-a model-b

Prints a comparison and saves every judgement next to the run file, as
judge_comparison_<run>.json, or judge_comparison_<run>_2.json and so on if
that's taken. Metrics the run didn't judge, because a check that the
answer is right failed first, are left out. With JUDGE_COMPARE_MODELS set to
model names, ./run_evals.sh runs this on its own run when it finishes.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Dict, List, Optional, Sequence

from dotenv import load_dotenv

load_dotenv(
    os.getenv("EVAL_ENV_FILE", str(Path(__file__).resolve().parents[1] / ".env.eval"))
)

from deepeval.models import DeepEvalBaseLLM  # noqa: E402
from deepeval.test_case import LLMTestCase  # noqa: E402

from judge.metrics import (  # noqa: E402
    NOT_JUDGED,
    clarification_quality_metric,
    missing_data_explanation_metric,
    error_diagnosis_metric,
    prose_metric,
    query_performance_metric,
    sql_correctness_metric,
)
from judge.model import JudgeSettings, build_judge, judge_threshold  # noqa: E402
from judge.names import plain_model_name  # noqa: E402

# The run's own judgements, when its judge is named to judge again.
RUN_LABEL = " (run)"


def _base_name(metric_name: str) -> str:
    return metric_name.split(" [")[0]


def _wanted(name: str, wanted: Optional[Sequence[str]]) -> bool:
    return not wanted or any(name.lower().startswith(w.lower()) for w in wanted)


def _model(name: Optional[str]) -> str:
    return plain_model_name(name or "the run's judge")


def _metric(name: str, endpoint: str, judge: DeepEvalBaseLLM):
    if name == "SQL Correctness":
        return sql_correctness_metric(judge)
    if name.startswith("Query Performance"):
        return query_performance_metric(judge)
    if name == "Error Diagnosis":
        return error_diagnosis_metric(judge)
    if name == "Clarification Quality":
        return clarification_quality_metric(judge)
    if name == "Explains What's Missing":
        return missing_data_explanation_metric(judge)
    return prose_metric(judge, endpoint)


def _test_case(case: Dict[str, Any]) -> LLMTestCase:
    """The test case as test_live_eval built it for the judge."""
    meta = case.get("metadata") or {}
    return LLMTestCase(
        input=case["input"],
        actual_output=case["actualOutput"],
        expected_output=case.get("expectedOutput"),
        context=case.get("context"),
        metadata={
            k: meta[k]
            for k in ("query_results", "diagnosis", "expected_diagnosis", "clarifications", "plugin_sql")
            # An empty clarifications list is kept: the plugin didn't ask.
            if meta.get(k) or (k == "clarifications" and k in meta)
        },
    )


def _rows_match(case: Dict[str, Any]) -> Optional[bool]:
    match = next(
        (m for m in case.get("metricsData") or [] if m["name"] == "SQL Result Match"),
        None,
    )
    return None if match is None else bool(match["success"])


def _row(index: int, case: Dict[str, Any], name: str, model: str, **verdict) -> Dict[str, Any]:
    return {
        "case": index,
        "golden": case["name"],
        "endpoint": (case.get("metadata") or {}).get("endpoint"),
        "metric": name,
        "model": model,
        "rows_match": _rows_match(case),
        **verdict,
    }


def provider_judge(model: str) -> DeepEvalBaseLLM:
    """`model` as a judge of the suite's provider, with its other settings
    and no second opinion."""
    return build_judge(JudgeSettings.from_env({**os.environ, "JUDGE_MODEL_NAME": model}))


async def rejudge(
    run: Dict[str, Any],
    models: List[str],
    make_judge: Callable[[str], DeepEvalBaseLLM] = provider_judge,
    limit: int = 4,
    wanted: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Every judged metric of the run (or those whose names start with one of
    `wanted`), as the run's judge graded it and as each of `models` grades the
    same answer. At most `limit` judgements at once."""
    saved = []
    jobs = []
    for index, case in enumerate(run["testCases"]):
        for m in case.get("metricsData") or []:
            if "GEval" not in m["name"] or not _wanted(_base_name(m["name"]), wanted):
                continue
            # Not judged in the run, as a check that the answer is right failed
            # first: nothing to compare with.
            if (m.get("reason") or "").startswith(NOT_JUDGED):
                continue
            name = _base_name(m["name"])
            saved.append(
                _row(
                    index, case, name, _model(m.get("evaluationModel")),
                    score=m.get("score"), success=bool(m.get("success")),
                    reason=m.get("reason"), error=m.get("error"),
                    input_tokens=m.get("inputTokenCount"),
                    output_tokens=m.get("outputTokenCount"),
                )
            )
            jobs += [(index, case, name, model) for model in models]

    # The run's judge, named again, judges under today's criteria.
    again = {r["model"] for r in saved} & set(models)
    for r in saved:
        if r["model"] in again:
            r["model"] += RUN_LABEL
    judges = {model: make_judge(model) for model in models}
    gate = asyncio.Semaphore(limit)

    async def grade(index, case, name, model):
        metric = _metric(name, (case.get("metadata") or {}).get("endpoint"), judges[model])
        async with gate:
            try:
                await metric.a_measure(_test_case(case), _show_indicator=False)
            except Exception as e:  # noqa: BLE001 - one failed judgement isn't the run
                return _row(index, case, name, model, score=None, success=False,
                            reason=None, error=f"{type(e).__name__}: {e}",
                            input_tokens=None, output_tokens=None)
        return _row(
            index, case, name, model,
            score=metric.score, success=bool(metric.success), reason=metric.reason,
            error=getattr(metric, "error", None),
            input_tokens=getattr(metric, "input_tokens", None),
            output_tokens=getattr(metric, "output_tokens", None),
        )

    fresh = list(await asyncio.gather(*(grade(*job) for job in jobs)))
    return fresh + saved if again else saved + fresh


def _cell(rows: List[Dict[str, Any]]) -> str:
    graded = [r for r in rows if r["score"] is not None]
    if not rows:
        return "-"
    passed = sum(r["success"] for r in graded)
    score = f" ({mean(r['score'] for r in graded):.2f})" if graded else ""
    return f"{passed}/{len(rows)}{score}"


def _percent(part: int, whole: int) -> str:
    return f"{part}/{whole} ({part / whole:.0%})" if whole else "-"


def comparison_text(rows: List[Dict[str, Any]], run_name: str = "") -> str:
    models = list(dict.fromkeys(r["model"] for r in rows))
    first = models[0]
    by = {(r["case"], r["metric"], r["model"]): r for r in rows}
    keys = sorted({(r["case"], r["metric"]) for r in rows})
    names = list(dict.fromkeys(r["metric"] for r in rows))
    threshold = judge_threshold()
    near = f"Scored {threshold - 0.1:.1f} or {threshold:.1f}, a step from flipping"
    labels = [*names, f"Same verdict as {first}", f"Mean score gap to {first}", near]
    width = max(22, *(len(m) + 2 for m in models))
    label = max(34, *(len(c) + 2 for c in labels))

    def line(name: str, cells: List[str]) -> str:
        return f"{name:<{label}}" + "".join(f"{c:<{width}}" for c in cells)

    saved = next((m for m in models if m.endswith(RUN_LABEL)), None)
    judged_by = (
        f"The run was judged by {saved[:-len(RUN_LABEL)]}; its verdicts in the run are '{saved}'."
        if saved
        else f"The run was judged by {first}."
    )
    out = [
        f"Judges compared on the same answers{f': {run_name}' if run_name else ''}",
        f"{len({k[0] for k in keys})} tests, {len(keys)} judged metrics each. {judged_by} "
        f"Cells are passed/judged (mean score); agreement is with {first}.",
        "",
        line("Metric", models),
    ]
    for metric in names:
        out.append(line(metric, [
            _cell([r for r in rows if r["metric"] == metric and r["model"] == m]) for m in models
        ]))
    out.append(line("All judged metrics", [_cell([r for r in rows if r["model"] == m]) for m in models]))

    def pairs(model: str):
        """(model's verdict, first's verdict) on each metric both graded."""
        for c, k in keys:
            mine, theirs = by.get((c, k, model)), by.get((c, k, first))
            if mine and theirs and mine["score"] is not None and theirs["score"] is not None:
                yield mine, theirs

    def agreement(model: str) -> str:
        both = list(pairs(model))
        return _percent(sum(a["success"] == b["success"] for a, b in both), len(both))

    def gap(model: str) -> str:
        both = list(pairs(model))
        return f"{mean(abs(a['score'] - b['score']) for a, b in both):.2f}" if both else "-"

    def close(model: str) -> str:
        scores = [r["score"] for r in rows if r["model"] == model and r["score"] is not None]
        steps = sum(threshold - 0.1 - 1e-9 <= s < threshold + 0.1 - 1e-9 for s in scores)
        return _percent(steps, len(scores))

    def against_rows(model: str, rows_match: bool, success: bool) -> str:
        judged = [
            r for r in rows
            if r["model"] == model and r["metric"] == "SQL Correctness"
            and r["score"] is not None and r["rows_match"] is rows_match
        ]
        return _percent(sum(r["success"] is success for r in judged), len(judged))

    out.append(line(f"Same verdict as {first}", ["-" if m == first else agreement(m) for m in models]))
    out.append(line(f"Mean score gap to {first}", ["-" if m == first else gap(m) for m in models]))
    out.append(line(near, [close(m) for m in models]))
    out.append(line("Fails although rows match", [against_rows(m, True, False) for m in models]))
    out.append(line("Passes although rows differ", [against_rows(m, False, True) for m in models]))
    tokens = []
    for m in models:
        mine = [r for r in rows if r["model"] == m]
        tokens.append(
            f"{sum(r['input_tokens'] or 0 for r in mine):,} / {sum(r['output_tokens'] or 0 for r in mine):,}"
        )
    out.append(line("Tokens in / out", tokens))
    out.append(line("Errors", [str(sum(1 for r in rows if r["model"] == m and r["score"] is None)) for m in models]))
    out += [
        "",
        "The rows lines are SQL Correctness against the database's comparison with the",
        "reference, which every correctness judge is shown. Failing although the rows",
        "match is a risk this data doesn't show, or another metric's concern: read why.",
    ]

    name_width = max(20, *(len(m) for m in models))
    split = [
        (c, k) for c, k in keys
        if len({by[(c, k, m)]["success"] for m in models if (c, k, m) in by and by[(c, k, m)]["score"] is not None}) > 1
    ]
    out += ["", f"Where they disagree ({len(split)} of {len(keys)} judged metrics):"]
    for c, k in split:
        some = next(by[(c, k, m)] for m in models if (c, k, m) in by)
        rows_note = "" if some["rows_match"] is None else f", rows match: {some['rows_match']}"
        out.append(f"\n- {some['golden']} ({some['endpoint']}): {k}{rows_note}")
        for m in models:
            r = by.get((c, k, m))
            if r is None:
                continue
            if r["score"] is None:
                out.append(f"    {m:<{name_width}} error: {(r['error'] or '')[:150]}")
                continue
            verdict = "PASS" if r["success"] else "FAIL"
            reason = " ".join((r["reason"] or "").split())[:240]
            out.append(f"    {m:<{name_width}} {r['score']:.1f} {verdict}  {reason}")
    errors = [r for r in rows if r["score"] is None]
    if errors:
        out += ["", f"First error: {errors[0]['model']}: {(errors[0]['error'] or '')[:300]}"]
    return "\n".join(out)


def _free_path(path: Path) -> Path:
    """`path`, or the first of path_2, path_3, ... that isn't taken."""
    candidate, n = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        n += 1
    return candidate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run", required=True, help="a test_run_<ts>.json results file")
    parser.add_argument(
        "models", nargs="+",
        help="models of the suite's judge provider to judge with; "
        "name the run's own judge to have it judge again",
    )
    parser.add_argument(
        "--metric", action="append", dest="metrics", metavar="NAME",
        help='only metrics whose names start with NAME, e.g. "SQL Correctness"; repeat for more',
    )
    parser.add_argument("--limit", type=int, default=4, help="judgements at once (default 4)")
    parser.add_argument(
        "--out",
        help="file for every judgement (default: judge_comparison_<run>.json "
        "next to the run, never overwriting one)",
    )
    args = parser.parse_args()

    run_path = Path(args.run)
    run = json.loads(run_path.read_text())
    print(f"Re-judging {run_path.name} with {', '.join(args.models)}...", file=sys.stderr)
    rows = asyncio.run(rejudge(run, args.models, limit=args.limit, wanted=args.metrics))
    if not rows:
        sys.exit(f"{run_path.name} has no judged metrics" + (f" matching {args.metrics}" if args.metrics else ""))
    saved = Path(args.out) if args.out else _free_path(
        run_path.with_name(f"judge_comparison_{run_path.stem}.json")
    )
    saved.write_text(json.dumps(rows, indent=1))
    print(comparison_text(rows, run_path.name))
    print(f"\nEvery judgement: {saved}")


if __name__ == "__main__":
    main()
