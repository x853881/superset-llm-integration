"""Readable report of the latest evaluation run.

`deepeval test run` saves each run as results/test_run_<timestamp>.json when
DEEPEVAL_RESULTS_FOLDER is set, and run_evals.sh also has pytest write
results/latest_junit.xml. This prints both: exact scores, full failure
reasons and the SQL the plugin wrote, plus any question that crashed or was
skipped before it could be graded.

A run made with ./run_evals.sh --runs N asked each question N times. Its
report gives each question's pass rate and each metric's, then every run of
the questions that failed at least once. A question that passed every run
gets only its row in the table.

    python reports/report.py                           # latest run in results/
    python reports/report.py --run results/test_run_X.json --junit results/latest_junit.xml
"""

import argparse
import json
import math
import os
import re
import shutil
import sys
import textwrap
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# The suite's folder: this file is in its reports/.
SUITE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUITE / "evals"))
from judge.metrics import NOT_JUDGED  # noqa: E402
from judge.second_opinion import SECOND_OPINION  # noqa: E402
from plugin.endpoints import ENDPOINTS, TEXT  # noqa: E402
from suite.dataset import SMOKE_GOLDENS_DIR, golden_files  # noqa: E402


def _flagged_goldens(flag: str) -> frozenset:
    """Names of the goldens marked with this flag now. Read from the golden
    files, not the run, so an older run's report shows today's marks too."""
    try:
        return frozenset(
            g["name"]
            for path in golden_files()
            for g in json.loads(path.read_text())
            if (g.get("additional_metadata") or {}).get(flag)
        )
    except (OSError, ValueError, KeyError, TypeError):
        return frozenset()


# Goldens that should pass every run ("regression": true).
REGRESSION = _flagged_goldens("regression")
# Goldens expected to fail until the plugin changes ("waiting_on_plugin": true).
WAITING = _flagged_goldens("waiting_on_plugin")


def _suite_goldens() -> Optional[frozenset]:
    """Names of the goldens in the suite now, the smoke goldens among them,
    so a report of an older run leaves out the ones since removed. None if
    the golden files can't be read (then nothing is left out)."""
    try:
        files = golden_files()
        if SMOKE_GOLDENS_DIR.is_dir():
            files += golden_files(SMOKE_GOLDENS_DIR)
        return frozenset(
            g["name"] for path in files for g in json.loads(path.read_text())
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _shown(name: str) -> str:
    """A golden's name as the report shows it: REGRESSION in front of those
    that should pass every run, so a new A (no REGRESSION) stands out, and
    WAITING in front of those expected to fail until the plugin changes, so
    their failures aren't read as new. An underscore, not a space, so a narrow
    column doesn't wrap the name away from it."""
    if name in REGRESSION:
        return f"REGRESSION_{name}"
    if name in WAITING:
        return f"WAITING_{name}"
    return name

RESULTS_DIR = SUITE / "results"

# Endpoints whose answer is prose rather than SQL.
PROSE_ENDPOINTS = {name for name, e in ENDPOINTS.items() if e.output == TEXT}

# --runs N (pytest-repeat) ends each test's id with "-<run>-<N>".
RUN_SUFFIX = re.compile(r"-(\d+)-(\d+)$")

# What each metric means, so a failure reads as a sentence, not just a name.
METRICS = {
    "SQL Safety": "the SQL is one read-only SELECT, with no dangerous functions or sensitive columns.",
    "SQL Static Performance": "the SQL avoids slow patterns and uses the expected tables.",
    "Plugin Latency": "the plugin answers within its time limit (PLUGIN_LATENCY_BUDGET_S, or the endpoint's own in PLUGIN_LATENCY_BUDGETS).",
    "SQL Result Match": "running the SQL returns the same rows as the reference SQL.",
    "Same Rows as Original": "the rewrite returns the original's recorded row count and checksum: the same rows, in any order.",
    "SQL Correctness": "the judge agrees the SQL answers the question like the reference does.",
    "Query Performance & Table Selection": "the judge agrees it uses the right tables and efficient filters.",
    "SQL Runs": "the SQL runs without a database error.",
    "Plugin Answered": "the plugin gives an answer instead of an error or refusal.",
    "Description Accuracy": "the judge agrees the description is accurate, complete and reads like a request a user could type.",
    "Description Update": "the judge agrees the description reflects the SQL change, keeps the rest, and adds no commentary.",
    "Plan Explanation": "the judge agrees the explanation matches the plan, says where the cost goes and gets the numbers right.",
    "Breakdown Accuracy": "the judge agrees the summary and each section describe what that part of the SQL does.",
    "Tooltip Terms": "terms marked for the panel's hover are within the limit (none in descriptions) and none are SQL keywords or table/column names.",
    "Breakdown Coverage": "the breakdown's sections cover every line of the SQL, without partly overlapping (one inside another is fine).",
    "Description Unchanged": "an edit that doesn't change the result leaves the description reading as before.",
    "Forbidden Text": "the answer ignores an instruction hidden in the input, containing none of the text it asks for.",
    "Faster Than Original": "timed on the database, the rewrite saves at least ACCELERATE_MIN_SAVING of the original's time (score = share saved; 0.5 = twice as fast). Wrong rows fail untimed.",
    "Speed vs Reference": "timed on the database, the SQL is no more than REFERENCE_TIME_TOLERANCE slower than the reference SQL, or is slower by no more than REFERENCE_MIN_SLOWDOWN_MS. Wrong rows fail untimed.",
    "Error Diagnosis": "the judge agrees the plugin's explanation names the real cause of the error.",
    "Asked for Clarification": "the plugin asked a question back about a request too ambiguous to answer, or answered and stated which reading it assumed.",
    "Said It Can't Answer": "for a request the data can't answer, the plugin said so (or asked what to show instead) rather than writing SQL.",
    "Explains What's Missing": "the judge agrees the plugin named what the data lacks and offered alternatives the tables can answer.",
    "Clarification Covers Readings": "the plugin's questions offer every reading of the request the golden lists.",
    "Clarification Quality": "the judge agrees the questions are about the real ambiguity, with distinct options the data can answer.",
}


# What decides each metric's pass or fail. Metrics without an entry here are
# graded by the LLM judge, named from the results file.
JUDGED_BY = {
    "SQL Safety": "Rules: SQLGlot parses the SQL. No LLM",
    "SQL Static Performance": "Rules: SQLGlot and the schema metadata. No LLM",
    "Plugin Latency": "Timer: the plugin's response time. No LLM",
    "SQL Result Match": "Database: SQL Lab runs both queries and the rows are compared. No LLM",
    "Same Rows as Original": "Database: the rewrite's rows checksummed and compared with the original's recorded result. No LLM",
    "SQL Runs": "Database: SQL Lab runs the SQL. No LLM",
    "Faster Than Original": "Database timer: both queries run and timed. No LLM",
    "Speed vs Reference": "Database timer: both queries run and timed. No LLM",
    "Plugin Answered": "Rules: the plugin gave an answer. No LLM",
    "Tooltip Terms": "Rules: the plugin's [[term|meaning]] markup counted and checked. No LLM",
    "Breakdown Coverage": "Rules: section line ranges compared with the SQL. No LLM",
    "Description Unchanged": "Comparison: old and new description text. No LLM",
    "Forbidden Text": "Rules: text the answer must not contain. No LLM",
    "Asked for Clarification": "Rules: the plugin's questions, or its stated assumptions. No LLM",
    "Said It Can't Answer": "Rules: the plugin's status, and whether it wrote SQL. No LLM",
    "Clarification Covers Readings": "Rules: the golden's readings searched for in the questions. No LLM",
}


@dataclass
class Question:
    """Every run of one golden: the graded results, and (ERROR|SKIPPED,
    message) for each run that never reached DeepEval."""

    endpoint: str = ""
    cases: List[dict] = field(default_factory=list)
    ungraded: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def runs(self) -> int:
        return len(self.cases) + len(self.ungraded)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.get("success"))

    @property
    def grade(self) -> float:
        """The average of its runs' grades; a run that wasn't graded is 0."""
        return sum(_grade(c) for c in self.cases) / self.runs if self.runs else 0.0

    @property
    def asked_once(self) -> bool:
        """Whether it was asked once, being steady (suite/steady.py), in a
        run that asked the others several times."""
        return any((c.get("metadata") or {}).get("asked_once") for c in self.cases)

    @property
    def tier(self) -> str:
        """How hard the golden is for the plugin: A passed every run, B more
        than half, C some, D none."""
        if self.passed == self.runs:
            return "A"
        if self.passed == 0:
            return "D"
        return "B" if self.passed * 2 > self.runs else "C"


TIERS = {
    "A": "passes every run",
    "B": "passes most runs",
    "C": "passes some runs",
    "D": "never passes",
}


def _judged_by(metric: dict) -> str:
    for name, judge in JUDGED_BY.items():
        if metric["name"].startswith(name):
            return judge
    model = metric.get("evaluationModel") or "model not recorded"
    return f"LLM judge: {model}, scored against a rubric"


def _meaning(metric_name: str) -> str:
    for name, meaning in METRICS.items():
        if metric_name.startswith(name):
            return meaning
    return ""


# A reason's first sentence: a full stop, ! or ? then a capital letter.
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def _short(reason: str, limit: int = 160) -> str:
    """A metric's reason cut to its first sentence and at most `limit`
    characters, for the tables; the full reason is printed below them. A
    re-asked judgement keeps its "[re-asked: ...]" ending."""
    reason, marker, reasked = reason.strip().partition(f" {SECOND_OPINION}")
    first = SENTENCE_END.split(reason, maxsplit=1)[0]
    first = first if len(first) <= limit else first[: limit - 1].rstrip() + "…"
    return f"{first}{marker}{reasked}"


# SQL metrics compared between ambiguous requests the plugin answered anyway
# and clear generate-sql requests.
COMPARED_SQL_METRICS = (
    "SQL Safety",
    "SQL Static Performance",
    "SQL Result Match",
    "SQL Correctness",
    "Query Performance & Table Selection",
)


def _width() -> int:
    """The report's width: the terminal's (or COLUMNS), 120 at most and when
    there is neither, as when run_evals.sh writes the report to a file."""
    return max(min(shutil.get_terminal_size((120, 24)).columns, 120), 40)


def _wrap(text: str, width: int, break_words: bool = False) -> List[str]:
    """The text's lines, each wrapped to the width. Indents and line breaks
    are kept. A word longer than the width is left whole, or split when
    break_words is set (a table cell has to fit its column)."""
    lines: List[str] = []
    for line in str(text).split("\n"):
        lines += textwrap.wrap(
            line, width, replace_whitespace=False, break_on_hyphens=False,
            break_long_words=break_words,
        ) or [""]
    return lines


def _say(text: str = "") -> None:
    """Print text wrapped to the report's width."""
    print("\n".join(_wrap(text, _width())))


def _rule(title: str = "") -> None:
    """A line across the report with a title in the middle of it."""
    width = _width()
    if not title:
        print("-" * width)
    elif len(title) + 4 <= width:
        print(f" {title} ".center(width, "-"))
    else:
        print("-" * width)
        _say(title)


def _panel(text: str) -> None:
    """Text between two heavy lines, for the report's headline."""
    print("=" * _width())
    _say(text)
    print("=" * _width())


def _col(name: str = "", right: bool = False, width: Optional[int] = None,
         most: Optional[int] = None) -> dict:
    """A table column: right-aligned or not, a fixed width, or the most it
    may take before its cells wrap."""
    return {"name": name, "right": right, "width": width, "most": most}


def _table(columns: list, rows: list, title: str = "", header: bool = True,
           lines: bool = False) -> None:
    """Print rows as aligned columns no wider than the report. Each column
    is as wide as its longest cell; when they don't all fit, the widest are
    narrowed and their cells wrap onto more lines. A row of None draws a
    line, and lines=True draws one between every two rows."""
    columns = [c if isinstance(c, dict) else _col(c) for c in columns]
    gap = "  "
    cells = [[str(c) for c in row] + [""] * (len(columns) - len(row))
             for row in rows if row is not None]
    widths = []
    for i, col in enumerate(columns):
        texts = [row[i] for row in cells] + ([col["name"]] if header else [])
        longest = max((len(l) for t in texts for l in t.split("\n")), default=0)
        widths.append(col["width"] or max(min(longest, col["most"] or longest), 1))
    fixed = [bool(col["width"]) for col in columns]
    room = _width() - len(gap) * (len(columns) - 1)
    while sum(widths) > room:
        widest = max(
            (i for i in range(len(columns)) if not fixed[i]),
            key=lambda i: widths[i], default=None,
        )
        if widest is None or widths[widest] <= 8:
            break
        widths[widest] -= 1

    def show(row: list) -> None:
        wrapped = [_wrap(cell, w, break_words=True) for cell, w in zip(row, widths)]
        for n in range(max(len(w) for w in wrapped)):
            parts = [
                (w[n] if n < len(w) else "").rjust(width) if col["right"]
                else (w[n] if n < len(w) else "").ljust(width)
                for w, width, col in zip(wrapped, widths, columns)
            ]
            print(gap.join(parts).rstrip())

    line = gap.join("-" * w for w in widths)
    if title:
        _say(title)
    if header:
        show([col["name"] for col in columns])
        print(line)
    previous = None
    for n, row in enumerate(rows):
        if row is None or (lines and n and previous is not None):
            print(line)
        if row is not None:
            show([str(c) for c in row] + [""] * (len(columns) - len(row)))
        previous = row
    if header:
        print(line)


def _print_ambiguous_vs_clear(cases: list) -> None:
    """How the SQL the plugin wrote for ambiguous requests (clarification
    goldens it answered instead of asking) compares with its SQL for clear
    generate-sql requests: much better or worse means ambiguity changes how
    well it writes SQL, not just whether it asks."""
    def group(pick):
        scores = {m: [] for m in COMPARED_SQL_METRICS}
        for c in cases:
            if pick(c.get("metadata") or {}):
                for m in c.get("metricsData", []):
                    base = m["name"].replace(" [GEval]", "")
                    if base in scores:
                        scores[base].append(m)
        return scores

    ambiguous = group(lambda meta: meta.get("expect_clarification") and meta.get("plugin_sql"))
    clear = group(
        lambda meta: meta.get("endpoint") == "generate-sql"
        and not meta.get("expect_clarification")
        and not meta.get("expect_cant_answer")
    )
    if not any(ambiguous.values()):
        return

    def cell(ms: list) -> str:
        if not ms:
            return "-"
        ok = sum(bool(m.get("success")) for m in ms)
        scored = [m["score"] for m in ms if m.get("score") is not None]
        mean = f", mean {sum(scored) / len(scored):.2f}" if scored else ""
        return f"{ok} of {len(ms)} ({_percent(ok, len(ms))}{mean})"

    _table(
        ["Metric", _col("Ambiguous, answered anyway", right=True), _col("Clear", right=True)],
        [(name, cell(ambiguous[name]), cell(clear[name])) for name in COMPARED_SQL_METRICS],
        title="SQL for ambiguous requests it answered instead of asking, vs clear requests (generate-sql)",
    )


def _print_key(cases: list) -> None:
    """What each metric that graded these answers checks, once for the
    whole report rather than on every row."""
    names = sorted({m["name"] for c in cases for m in c.get("metricsData", [])})
    rows = [(n, _meaning(n)) for n in names if _meaning(n)]
    if not rows:
        return
    _rule("What each metric checks")
    speed = [n for n in names if n.startswith(SPEED_METRICS)]
    gate = (
        f" {_and(speed)} {'counts' if len(speed) == 1 else 'count'} 0% when the "
        "rows are wrong: a fast wrong answer is no use."
        if speed else ""
    )
    _say(
        "Grade: how close a run came to passing. A run passes only "
        "when every metric passes, so its grade is its furthest-off metric's: "
        "100% if every metric passed, otherwise that metric's score as a share "
        "of its pass mark (SQL Correctness 0.60 against 0.80 is 75%). A failed "
        "pass/fail check is 0%, except a timing one over budget, which is the "
        f"budget as a share of the time taken (31.2s against 30s is 96%).{gate} "
        "With --runs, a golden's grade is the average over its runs, a run "
        "that couldn't be graded counting 0%.\n"
    )
    _table([_col("Metric", most=34), "Passes if"], rows, header=False)


def _money(value: Optional[float]) -> str:
    return "-" if value is None else f"${value:.4f}"


def _and(names: list) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _percent(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "-"


# The first judge's score at the end of a re-asked judgement's reason, e.g.
# "[re-asked: small-model gave 0.7, large-model 0.8]"
# (judge/second_opinion.py).
_FIRST_SCORE = re.compile(re.escape(SECOND_OPINION) + r"\S+ gave ([\d.]+)")

# A timing metric's reason, e.g. "36.1s (budget 30.0s)".
_OVER_BUDGET = re.compile(r"([\d.]+)s \(budget ([\d.]+)s\)")


def _not_judged(metric: dict) -> bool:
    """A judged metric the judge wasn't asked, as a check that the answer is
    right had already failed (skip_judges_after_failure in
    suite/endpoint_metrics.py). The report leaves it out of the counts: the
    check that failed is the run's failure."""
    return (metric.get("reason") or "").startswith(NOT_JUDGED)


def _metric_grade(metric: dict) -> float:
    """How close one metric came to passing: 1 if it passed, otherwise its
    score as a share of its pass mark, kept under 1. A pass/fail check has
    no partial score, so it is 0, except one over its time budget, which is
    the budget as a share of the time taken (31.2s against 30s is 96%). 0
    with no score, e.g. when the metric errored."""
    if metric.get("success"):
        return 1.0
    over = _OVER_BUDGET.match(metric.get("reason") or "")
    if over and float(over[1]) > 0:
        return min(float(over[2]) / float(over[1]), 0.99)
    score, threshold = metric.get("score"), metric.get("threshold") or 0
    if score is None or threshold <= 0:
        return 0.0
    return min(score / threshold, 0.99)


# The checks that the rows are right. A faster rewrite that returns the wrong
# rows is no use, so the speed metrics grade 0 when one of these failed (or,
# with neither, when SQL Correctness failed).
ROW_CHECKS = ("SQL Result Match", "Same Rows as Original")
SPEED_METRICS = ("Faster Than Original",)


def _rows_wrong(metrics: List[dict]) -> bool:
    checks = [m for m in metrics if m["name"] in ROW_CHECKS]
    if not checks:
        checks = [m for m in metrics if m["name"].startswith("SQL Correctness")]
    return any(not m.get("success") for m in checks)


def _grade(case: dict) -> float:
    """A run's grade: how close it came to passing, which is how close its
    furthest-off metric came to its pass mark. A run passes only when every
    metric does, so the metrics that passed don't lift it: one judge metric
    at 0.50 against 0.80 grades the run 62% however many others passed, and
    a failed pass/fail check grades it 0%. 100% only when it passed. A
    metric that wasn't judged doesn't count: the check that failed first
    grades the run."""
    metrics = [m for m in case.get("metricsData", []) if not _not_judged(m)]
    if not metrics:
        return 0.0
    rows_wrong = _rows_wrong(metrics)
    return min(
        0.0 if m["name"].startswith(SPEED_METRICS) and rows_wrong else _metric_grade(m)
        for m in metrics
    )


def _grade_text(grade: float) -> str:
    """Rounded down, so a run that failed never shows 100%."""
    return f"{math.floor(grade * 100)}%"


def _failed_metric(metric: dict) -> str:
    score = metric.get("score")
    if score is None:
        return f"{metric['name']} (error)"
    return f"{metric['name']} ({score:.2f}/{metric.get('threshold') or 0:.2f})"


def _run_totals(cases: list) -> str:
    """The judge's tokens and cost for the whole run."""
    judged = [m for c in cases for m in c.get("metricsData", []) if m.get("evaluationModel")]
    if not judged:
        return ""
    costs = [m["evaluationCost"] for m in judged if m.get("evaluationCost") is not None]
    reasked = [m for m in judged if SECOND_OPINION in (m.get("reason") or "")]
    # Where the second judge's verdict differs from the first's.
    changed = sum(
        1 for m in reasked
        if (float(_FIRST_SCORE.search(m["reason"])[1]) >= (m.get("threshold") or 0) - 1e-9)
        != bool(m.get("success"))
    )
    return (
        f"Judge: {len(judged)} judged metrics, "
        f"{sum(m.get('inputTokenCount') or 0 for m in judged):,} tokens in, "
        f"{sum(m.get('outputTokenCount') or 0 for m in judged):,} out, "
        f"{_money(sum(costs) if costs else None)}"
        + (
            f"; {len(reasked)} borderline scores judged again, {changed} verdicts changed"
            if reasked
            else ""
        )
    )


def _run_label(run: dict, run_path: Optional[Path]) -> str:
    """The part of the suite and the results file, for the headline."""
    label = ""
    if os.getenv("EVAL_PART"):
        label += f"   part: {os.environ['EVAL_PART']}"
    if run_path:
        label += f"   ({run_path.name}, {run.get('runDuration', 0):.0f}s)"
    return label


def _print_internals(meta: dict) -> None:
    """The plugin's steps with how long each took, and its retries, from its
    progress stream."""
    steps = meta.get("plugin_steps") or []
    if not steps:
        return
    _say(
        "Inside the plugin: "
        + " -> ".join(
            f"{s.get('label') or s.get('key')}"
            + (f" {s['seconds']:.1f}s" if s.get("seconds") is not None else "")
            + ("" if s.get("status") in (None, "done", "active") else f" ({s['status']})")
            for s in steps
        )
        + f"; {meta.get('plugin_retries', 0)} retr"
        + ("y" if meta.get("plugin_retries") == 1 else "ies")
    )


def _new_request(test_case: dict) -> str:
    text = test_case.get("input", "")
    marker = "New request: "
    return text.split(marker, 1)[1] if marker in text else text


def _print_request(case: dict) -> None:
    endpoint = (case.get("metadata") or {}).get("endpoint", "")
    _say(f"Endpoint: {endpoint}")
    _say(f"Request: {_new_request(case)}")


def _print_answer(case: dict) -> None:
    """The plugin's answer, what happened inside the plugin, and each metric's
    score and reason."""
    meta = case.get("metadata") or {}
    _say("Plugin's answer:")
    # SQL or prose, printed as the plugin wrote it.
    _say(case.get("actualOutput") or "")
    diagnosis = (case.get("metadata") or {}).get("diagnosis")
    if diagnosis:
        _say("Plugin's explanation of the error:")
        _say(diagnosis)
    explanation = meta.get("explanation")
    if explanation:
        _say("Plugin's explanation of the rewrite:")
        _say(explanation)
    _print_internals(case.get("metadata") or {})

    rows = []
    shortened = []
    for m in case.get("metricsData", []):
        score = m.get("score")
        skipped = _not_judged(m)
        score_text = (
            "-"
            if score is None or skipped
            else f"{score:.2f} / {m.get('threshold', 0):.2f}"
        )
        why = m.get("error") or m.get("reason") or ""
        if _short(why) != why.strip():
            shortened.append((m["name"], why))
        rows.append((
            m["name"],
            _judged_by(m),
            score_text,
            "SKIP" if skipped else "PASS" if m.get("success") else "FAIL",
            _short(why),
        ))
    _table(
        [
            _col("Metric", most=22),
            _col("Judged by", most=22),
            _col("Score", right=True, width=13),
            _col("Result", width=6),
            "Why",
        ],
        rows,
        lines=True,
    )
    for name, why in shortened:
        _say(f"{name}: {why}")


def _print_reference(sql: str) -> None:
    _say("Reference SQL:")
    _say(sql)


def _without_run(test_id: str) -> str:
    """A test's id without the "-<run>-<N>" that --runs adds."""
    match = RUN_SUFFIX.search(test_id)
    if match and int(match.group(2)) > 1:
        return test_id[: match.start()]
    return test_id


def _problem(case: ET.Element) -> Optional[Tuple[str, str]]:
    """(ERROR|SKIPPED, message) for a junit test that didn't pass."""
    for tag, label in (
        ("error", "ERROR"),
        ("failure", "ERROR"),
        ("skipped", "SKIPPED"),
    ):
        el = case.find(tag)
        if el is not None:
            return label, (el.get("message") or el.text or "").strip()
    return None


def _ungraded(junit: Optional[Path], graded: Counter) -> List[Tuple[str, str, str]]:
    """(question, ERROR|SKIPPED, message) for each run that never reached
    DeepEval. `graded` counts each question's results, so a question with
    fewer results than runs in the junit file had runs that weren't graded."""
    if not junit or not junit.exists():
        return []
    runs: Counter = Counter()
    problems: Dict[str, List[Tuple[str, str]]] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        match = re.search(r"\[(.+)\]$", case.get("name", ""))
        name = _without_run(match.group(1) if match else case.get("name", "?"))
        problem = _problem(case)
        if problem and "empty parameter set" in problem[1]:
            continue  # a part of the suite with none of these goldens
        runs[name] += 1
        if problem:
            problems.setdefault(name, []).append(problem)
    ungraded = []
    for name, found in problems.items():
        missing = max(runs[name] - graded[name.split(":", 1)[-1]], 0)
        # A graded run that failed is in the junit file too, as the
        # AssertionError DeepEval's assert_test raises; the runs that weren't
        # graded are the others.
        found.sort(key=lambda p: p[1].startswith("AssertionError"))
        ungraded += [
            (name, label, message.splitlines()[-1] if message else "")
            for label, message in found[:missing]
        ]
    return ungraded


def _questions(cases: list, ungraded: list) -> Dict[str, Question]:
    """Each question's runs, in the order the questions first ran."""
    questions: Dict[str, Question] = {}
    for case in cases:
        q = questions.setdefault(case.get("name", "?"), Question())
        q.endpoint = q.endpoint or (case.get("metadata") or {}).get("endpoint", "")
        q.cases.append(case)
    for name, label, message in ungraded:
        q = questions.setdefault(name.split(":", 1)[-1], Question())
        q.endpoint = q.endpoint or (name.split(":", 1)[0] if ":" in name else "")
        q.ungraded.append((label, message))
    return questions


def _report_single_runs(
    run: dict,
    run_path: Optional[Path],
    cases: list,
    ungraded: list,
) -> int:
    """One run of each question: every question in full, failures first."""
    failed = [c for c in cases if not c.get("success")]
    errored = [u for u in ungraded if u[1] == "ERROR"]
    total = len(cases) + len(ungraded)
    passed = len(cases) - len(failed)

    headline = f"{passed} of {total} questions passed"
    headline += "   average grade " + _grade_text(
        sum(_grade(c) for c in cases) / total if total else 0.0
    )
    if errored:
        headline += f"   {len(errored)} could not be graded"
    headline += _run_label(run, run_path)
    _panel(headline)
    totals = _run_totals(cases)
    if totals:
        _say(totals)

    rows = []
    for case in cases:
        bad = [
            _failed_metric(m)
            for m in case.get("metricsData", [])
            if not m.get("success") and not _not_judged(m)
        ]
        rows.append((
            _shown(case.get("name", "?")),
            (case.get("metadata") or {}).get("endpoint", ""),
            "PASS" if case.get("success") else "FAIL",
            _grade_text(_grade(case)),
            ", ".join(bad) or "-",
        ))
    for name, label, message in ungraded:
        endpoint = name.split(":", 1)[0] if ":" in name else ""
        rows.append((name.split(":", 1)[-1], endpoint, label, _grade_text(0.0), message))
    _table(
        [
            _col("Question", most=45),
            "Endpoint",
            _col("Result", width=7),
            _col("Grade", right=True, width=5),
            "Failed metrics (score/pass mark)",
        ],
        rows,
    )

    for name, label, message in ungraded:
        _rule(f"{label} {name}")
        _say(message or "(no message)")
        _say("Full output: results/latest_console.log\n")

    for case in failed + [c for c in cases if c.get("success")]:
        ok = case.get("success")
        _rule(
            f"{'PASS' if ok else 'FAIL'} "
            f"{_grade_text(_grade(case))} {_shown(case.get('name'))}"
        )
        _print_request(case)
        _print_answer(case)
        if not ok and case.get("expectedOutput"):
            _print_reference(case["expectedOutput"])
        _say()

    _print_ambiguous_vs_clear(cases)
    _print_key(cases)
    # A skipped question counts against the run, as it does with --runs: a
    # run where Superset was down and nothing was graded isn't a pass.
    return 1 if passed < total else 0


# The order the suite runs its endpoints in (suite/dataset.py), which the
# report groups goldens by.
ENDPOINT_ORDER = (
    "generate-sql",
    "update-sql-from-nl",
    "refine-sql",
    "explain-sql-error",
    "accelerate",
    "generate-nl",
    "update-nl-from-sql-diff",
    "explain-plan",
    "describe-breakdown",
)


def _in_order(questions: Dict[str, Question]) -> Dict[str, Question]:
    """Grouped by endpoint in suite order, then each endpoint's ladder from
    tier A to D, best pass rate and grade first within a tier."""
    def key(item: Tuple[str, Question]):
        name, q = item
        endpoint = ENDPOINT_ORDER.index(q.endpoint) if q.endpoint in ENDPOINT_ORDER else len(ENDPOINT_ORDER)
        return (endpoint, q.tier, -q.passed / max(q.runs, 1), -q.grade, name)

    return dict(sorted(questions.items(), key=key))


def _print_ladder(questions: Dict[str, Question]) -> None:
    """How many of each endpoint's goldens are at each tier. An endpoint with
    goldens at every tier shows the plugin getting better or worse; one whose
    goldens all pass, or all fail, hides it."""
    counts: Dict[str, Counter] = {}
    for q in questions.values():
        counts.setdefault(q.endpoint, Counter())[q.tier] += 1
    _table(
        ["Endpoint"]
        + [
            _col(f"{tier}: {meaning.replace('passes ', '')}", right=True)
            for tier, meaning in TIERS.items()
        ],
        [(endpoint, *(str(c[t]) for t in TIERS)) for endpoint, c in counts.items()],
        title="Difficulty ladder: each endpoint's goldens by tier",
    )


def _print_changes(
    questions: Dict[str, Question],
    before: Dict[str, Question],
    before_path: Optional[Path],
) -> None:
    """The goldens that changed tier, or moved 5 points or more of grade,
    since an earlier run (--compare): what a change to the plugin did."""
    moved = []
    for name, q in questions.items():
        old = before.get(name)
        if old and old.runs and q.runs and (
            q.tier != old.tier or abs(q.grade - old.grade) >= 0.05
        ):
            moved.append((q.grade - old.grade, name, old, q))
    _rule(f"Changes since {before_path.name if before_path else 'the earlier run'}")
    if moved:
        _table(
            [
                "Question",
                _col("Before", right=True),
                _col("Now", right=True),
                _col("Grade change", right=True),
            ],
            [
                (
                    _shown(name),
                    f"{old.tier} {old.passed}/{old.runs} {old.grade:.0%}",
                    f"{q.tier} {q.passed}/{q.runs} {q.grade:.0%}",
                    f"{delta * 100:+.0f} points",
                )
                for delta, name, old, q in sorted(moved)
            ],
        )
    else:
        _say("No golden changed tier or moved 5 points of grade.")
    for label, names in (
        ("Not in the earlier run", [n for n in questions if n not in before]),
        ("Not in this run", [n for n in before if n not in questions]),
    ):
        if names:
            shown = ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
            _say(f"{label} ({len(names)}): {shown}")


def _report_pass_rates(
    run: dict,
    run_path: Optional[Path],
    cases: list,
    questions: Dict[str, Question],
    before: Optional[Dict[str, Question]] = None,
    before_path: Optional[Path] = None,
) -> int:
    """Several runs of each question (--runs): each question's pass rate and
    each metric's, then every run of the questions that failed at least once,
    lowest pass rate first."""
    runs = sum(q.runs for q in questions.values())
    passed = sum(q.passed for q in questions.values())
    steady = sum(1 for q in questions.values() if q.passed == q.runs)
    errored = sum(
        1 for q in questions.values() for label, _ in q.ungraded if label == "ERROR"
    )
    most = max(q.runs for q in questions.values())

    headline = (
        f"{steady} of {len(questions)} questions passed every run"
        f"   {passed} of {runs} runs passed ({_percent(passed, runs)})"
        f"   average grade "
        + _grade_text(sum(q.grade * q.runs for q in questions.values()) / runs)
        + f"   {most} runs of each"
    )
    once = sum(1 for q in questions.values() if q.asked_once)
    if once:
        headline += f" ({once} steady golden{'' if once == 1 else 's'} asked once)"
    if errored:
        headline += (
            f"   {errored} run{'' if errored == 1 else 's'} "
            "could not be graded"
        )
    headline += _run_label(run, run_path)
    _panel(headline)
    totals = _run_totals(cases)
    if totals:
        _say(totals)

    rows: list = []
    endpoint = None
    for name, q in questions.items():
        # Grouped by endpoint (_in_order): a heading row starts each group.
        if q.endpoint != endpoint:
            if endpoint is not None:
                rows.append(None)
            endpoint = q.endpoint
            rows.append((endpoint or "unknown endpoint",))
        failures = Counter(
            m["name"]
            for c in q.cases
            for m in c.get("metricsData", [])
            if not m.get("success") and not _not_judged(m)
        )
        failures.update("not graded" for _ in q.ungraded)
        rows.append((
            _shown(name),
            f"{q.passed} of {q.runs}" + (" steady" if q.asked_once else ""),
            _percent(q.passed, q.runs),
            _grade_text(q.grade),
            q.tier,
            ", ".join(
                f"{name.removesuffix(' [GEval]')} ({n})" for name, n in failures.most_common()
            ) or "-",
        ))
    _table(
        [
            _col("Question", most=60),
            "Passed",
            _col("Rate", right=True),
            _col("Grade", right=True),
            "Tier",
            "Failed metrics (runs)",
        ],
        rows,
    )
    _print_ladder(questions)
    if before is not None:
        _print_changes(questions, before, before_path)

    # How often each metric passed, over every run it graded (not those it
    # wasn't judged on).
    tally: Dict[str, List[int]] = {}
    for q in questions.values():
        for case in q.cases:
            for m in case.get("metricsData", []):
                if _not_judged(m):
                    continue
                counts = tally.setdefault(m["name"], [0, 0])
                counts[0] += bool(m.get("success"))
                counts[1] += 1
    _table(
        ["Metric", _col("Passed", right=True), _col("Rate", right=True)],
        [
            (name, f"{ok} of {n}", _percent(ok, n))
            for name, (ok, n) in sorted(
                tally.items(), key=lambda item: (item[1][0] / item[1][1], item[0])
            )
        ],
        title="Each metric, over every run",
    )

    shaky = sorted(
        (item for item in questions.items() if item[1].passed < item[1].runs),
        key=lambda item: item[1].passed / item[1].runs,
    )
    for name, q in shaky:
        _rule(f"{q.passed} of {q.runs} passed {_grade_text(q.grade)} {_shown(name)}")
        if q.cases:
            _print_request(q.cases[0])
        else:
            _say(f"Endpoint: {q.endpoint}")
        for i, case in enumerate(q.cases, 1):
            result = "PASS" if case.get("success") else "FAIL"
            _say(f"\nRun {i}: {result} {_grade_text(_grade(case))}")
            _print_answer(case)
        for label, message in q.ungraded:
            _say(
                f"\nA run that wasn't graded: {label} "
                f"{message or '(no message)'}"
            )
        if q.ungraded:
            _say("Full output: results/latest_console.log")
        reference = next(
            (c["expectedOutput"] for c in q.cases if c.get("expectedOutput")), None
        )
        if reference:
            _print_reference(reference)
        _say()
    if steady:
        _say(
            f"{steady} question{'' if steady == 1 else 's'} passed every run: "
            "see the table at the top."
        )

    _print_ambiguous_vs_clear(cases)
    _print_key(cases)
    return 1 if passed < runs else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--run", type=Path, help="test_run_*.json (default: latest)"
    )
    parser.add_argument(
        "--junit", type=Path,
        help="pytest's results, for runs that weren't graded (default: "
        "results/latest_junit.xml, for the latest run only)",
    )
    parser.add_argument(
        "--no-run", action="store_true", help="only report pytest errors"
    )
    parser.add_argument(
        "--compare", type=Path,
        help="an earlier test_run_*.json: list the goldens whose tier or grade moved since",
    )
    args = parser.parse_args()

    latest = next(iter(sorted(RESULTS_DIR.glob("test_run_*.json"), reverse=True)), None)
    run_path = None if args.no_run else (args.run or latest)
    run = json.loads(run_path.read_text()) if run_path else {}
    cases = run.get("testCases") or []
    # latest_junit.xml belongs to the latest run: an earlier run's report
    # would count that run's goldens as not graded.
    junit = args.junit
    if junit is None and (run_path is None or run_path.resolve() == (latest and latest.resolve())):
        junit = RESULTS_DIR / "latest_junit.xml"
    ungraded = _ungraded(junit, Counter(c.get("name") for c in cases))
    suite = _suite_goldens()
    if suite is not None:
        cases = [c for c in cases if c.get("name") in suite]
        ungraded = [u for u in ungraded if u[0].split(":", 1)[-1] in suite]
        run = {**run, "testCases": cases}
    if not cases and not ungraded:
        sys.exit(f"No results in {RESULTS_DIR}. Run ./run_evals.sh first.")

    questions = _in_order(_questions(cases, ungraded))
    before = None
    if args.compare:
        before = _questions(json.loads(args.compare.read_text()).get("testCases") or [], [])
    if any(q.runs > 1 for q in questions.values()):
        return _report_pass_rates(
            run, run_path, cases, questions, before, args.compare
        )
    code = _report_single_runs(run, run_path, cases, ungraded)
    if before is not None:
        _print_changes(questions, before, args.compare)
    return code


if __name__ == "__main__":
    sys.exit(main())
