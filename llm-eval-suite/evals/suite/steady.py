"""Goldens steady enough to ask only once in a run of several (--runs N).

Asking a golden that always passes N times shows nothing new, and the judge
spends tokens on it each time. A golden is steady when, in the results files
of its last HISTORY runs (results/test_run_*.json, newest first), it

- ran at least MIN_RUNS times, all with the question it has now,
- passed every one of them,
- and every judged metric scored at least MIN_JUDGED_SCORE (above the pass
  mark of 0.8), from the judge this run uses.

It is then asked once, not N times, and the report says so. As its one run
joins its history, it stays steady only while it keeps passing: one failure
and it's asked N times again. A judge other than this run's leaves a golden
with judged metrics unsteady, as pass rates compare only between runs judged
by the same model.

./run_evals.sh --all-runs (EVAL_ALL_RUNS=1) asks every golden N times.
"""

import json
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

from judge.names import plain_model_name

HISTORY = 3
MIN_RUNS = 3
MIN_JUDGED_SCORE = 0.9
# The most results files read, newest first, looking for each golden's runs.
MAX_FILES = 20

JUDGED = "[GEval]"


def _steady_case(case: Mapping, question: str, judge: Optional[str]) -> bool:
    if case.get("input") != question or not case.get("success"):
        return False
    for m in case.get("metricsData") or []:
        if JUDGED not in m.get("name", ""):
            continue
        # Without ", borderline re-asked of ..." (judge/second_opinion.py):
        # a score high enough here is never re-asked.
        model = plain_model_name((m.get("evaluationModel") or "").split(", ")[0])
        if judge and model != judge:
            return False
        if (m.get("score") or 0) < MIN_JUDGED_SCORE:
            return False
    return True


def steady_goldens(
    questions: Mapping[str, str],
    results_dir: Path,
    judge: Optional[str] = None,
) -> List[str]:
    """The names, of those in `questions` (name: the question as the results
    record it, LLMTestCase.input), of the goldens to ask once. `judge` is
    this run's judge model (JUDGE_MODEL_NAME); None means no judge."""
    files = sorted(results_dir.glob("test_run_*.json"), reverse=True)[:MAX_FILES]
    history: Dict[str, List[List[Mapping]]] = {name: [] for name in questions}
    for path in files:
        if all(len(runs) >= HISTORY for runs in history.values()):
            break
        try:
            cases: Iterable[Mapping] = json.loads(path.read_text()).get("testCases") or []
        except (OSError, ValueError):
            continue
        found: Dict[str, List[Mapping]] = {}
        for case in cases:
            if case.get("name") in history:
                found.setdefault(case["name"], []).append(case)
        for name, runs in found.items():
            if len(history[name]) < HISTORY:
                history[name].append(runs)
    steady = []
    for name, files_runs in history.items():
        runs = [case for file_runs in files_runs for case in file_runs]
        if len(runs) >= MIN_RUNS and all(
            _steady_case(case, questions[name], judge) for case in runs
        ):
            steady.append(name)
    return steady
