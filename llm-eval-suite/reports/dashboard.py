"""Colour dashboard comparing two evaluation runs.

Reads two saved runs (results/test_run_<timestamp>.json), works out what got
better or worse per endpoint and per metric, and writes one self-contained
HTML file you can open offline. Nothing is uploaded anywhere.

The before-and-after numbers cover the goldens both runs asked, so a run with
extra or missing questions doesn't skew the change. Goldens in only one run
are still listed with their results, and each run's pass rate over every
golden it asked is shown too. Pass rates are per attempt, so a
run made with --runs 5 compares fairly with one made with --runs 3. Every
result counts as the run recorded it: a judged metric the judge wasn't asked,
because a check that the answer is right had already failed ("not judged:
..."), counts as failed, so the plugin's failures stay in every number.

    python reports/dashboard.py              # latest run vs the latest earlier run with the same goldens
    python reports/dashboard.py NEW          # that run vs the latest earlier run with the same goldens
    python reports/dashboard.py BASE NEW     # two runs of your choice
    python reports/dashboard.py BASE NEW -o results/my_dashboard.html
    python reports/dashboard.py --no-open    # write it without opening it
    python reports/dashboard.py --list       # the saved runs, to choose two from

The dashboard opens in your browser once it's written, unless --no-open.
"""

import argparse
import ast
import difflib
import json
import re
import statistics
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The suite's folder: this file is in its reports/.
SUITE = HERE.parent
GOLDENS_DIR = SUITE / "evals" / "goldens"
RESULTS_DIR = SUITE / "results"
REASON_CHARS = 500
OUTPUT_CHARS = 2500
INPUT_CHARS = 1500
# A base run picked automatically must have at least this share of the new
# run's goldens.
AUTO_BASE_OVERLAP = 0.8

# What else the plugin said besides its answer, from a case's metadata.
EXTRAS = (
    ("diagnosis", "Its explanation of the error"),
    ("explanation", "Its explanation of the rewrite"),
    ("assumptions", "Assumptions it stated"),
)


def _suite():
    """(names of the goldens in the suite now, golden name -> flags marked on
    it now). Names is None if the golden files can't be read. Read from the
    golden files, not the runs, as report.py does, so older runs show
    today's marks."""
    names, flags = set(), {}
    try:
        for path in sorted(GOLDENS_DIR.glob("*.json")):
            for g in json.loads(path.read_text()):
                names.add(g.get("name"))
                meta = g.get("additional_metadata") or {}
                marked = [f for f in ("regression", "waiting_on_plugin") if meta.get(f)]
                if marked:
                    flags[g.get("name")] = marked
    except (OSError, ValueError, AttributeError, TypeError):
        return None, flags
    return names or None, flags


def _metric_info():
    """METRICS (what each metric checks) and JUDGED_BY from report.py, read
    without importing it, as it needs the suite's packages."""
    found = {}
    try:
        tree = ast.parse((HERE / "report.py").read_text())
    except (OSError, SyntaxError, ValueError):
        return {}, {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in ("METRICS", "JUDGED_BY")):
            try:
                found[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return found.get("METRICS", {}), found.get("JUDGED_BY", {})


def _label(path: Path) -> str:
    m = re.search(r"(\d{8})_(\d{6})", path.name)
    if not m:
        return path.stem
    when = datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S")
    return f"{when.day} {when:%b %H:%M}"


def _clip(text, n):
    text = "" if text is None else str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def _diff(old: str, new: str):
    """Word-level differences as [op, text] pairs: "=" kept, "-" only in the
    old text, "+" only in the new."""
    a, b = re.split(r"(\s+)", old), re.split(r"(\s+)", new)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(["=", "".join(a[i1:i2])])
            continue
        if i2 > i1:
            out.append(["-", "".join(a[i1:i2])])
        if j2 > j1:
            out.append(["+", "".join(b[j1:j2])])
    return out


def load_run(path: Path):
    """The run's goldens (name -> {endpoint, input, expected, attempts}) and
    the judges it used."""
    data = json.loads(path.read_text())
    goldens, judges = {}, set()
    for case in data.get("testCases") or []:
        meta = case.get("metadata") or {}
        g = goldens.setdefault(case["name"], {
            "endpoint": meta.get("endpoint") or "unknown",
            "input": case.get("input") or "",
            "expected": case.get("expectedOutput") or "",
            "attempts": [],
        })
        metrics = {}
        for m in case.get("metricsData") or []:
            metrics[m["name"]] = {
                "success": bool(m.get("success")),
                "score": m.get("score"),
                "threshold": m.get("threshold"),
                "reason": m.get("reason") or m.get("error") or "",
                "judged": bool(m.get("evaluationModel")) or m["name"].endswith("[GEval]"),
            }
            if m.get("evaluationModel"):
                judges.add(m["evaluationModel"])
        g["attempts"].append({
            "success": bool(case.get("success")),
            "metrics": metrics,
            "output": case.get("actualOutput"),
            "extras": [(label, meta[key]) for key, label in EXTRAS if meta.get(key)],
            "time": case.get("completionTime"),
            "crashed": bool(meta.get("plugin_crashed")),
            "retries": meta.get("plugin_retries") or 0,
            "error": meta.get("plugin_error") or "",
        })
    return {"goldens": goldens, "judges": sorted(judges), "duration": data.get("runDuration")}


def _rate(passed, total):
    return None if not total else passed / total


def _tally(attempts, metric=None):
    """(passed, total) over attempts, for the whole case or one metric, as
    the run recorded them."""
    if metric is None:
        return sum(a["success"] for a in attempts), len(attempts)
    hits = [a["metrics"][metric]["success"] for a in attempts if metric in a["metrics"]]
    return sum(hits), len(hits)


def _dots(attempts, metric=None):
    """Each attempt's result in order: 1 passed, 0 failed."""
    if metric is None:
        return "".join("1" if a["success"] else "0" for a in attempts)
    return "".join("1" if a["metrics"][metric]["success"] else "0"
                   for a in attempts if metric in a["metrics"])


def _scores(attempts, metric):
    return [a["metrics"][metric]["score"] for a in attempts
            if metric in a["metrics"] and isinstance(a["metrics"][metric]["score"], (int, float))]


def _mean(xs):
    return round(sum(xs) / len(xs), 2) if xs else None


def _cell(old, new):
    (op, ot), (np_, nt) = old, new
    o, n = _rate(op, ot), _rate(np_, nt)
    return {
        "old": [op, ot], "new": [np_, nt],
        "delta": None if o is None or n is None else round((n - o) * 100, 1),
    }


def _tier(passed, total):
    """report.py's tiers: A passed every attempt, B more than half, C some, D none."""
    if not total:
        return None
    if passed == total:
        return "A"
    if passed == 0:
        return "D"
    return "B" if passed * 2 > total else "C"


def _metric_names(attempts):
    names = []
    for a in attempts:
        for m in a["metrics"]:
            if m not in names:
                names.append(m)
    return names


def _with_scores(cell, b, w, metric):
    """Mean scores, as recorded, when the metric scores more than pass/fail:
    judged ones, and rule-based ones with partial scores (Faster Than
    Original)."""
    old, new = _scores(b, metric), _scores(w, metric)
    judged = any(a["metrics"][metric]["judged"] for a in b + w if metric in a["metrics"])
    if judged or any(0 < s < 1 for s in old + new):
        threshold = next((a["metrics"][metric]["threshold"] for a in w + b
                          if metric in a["metrics"] and a["metrics"][metric]["threshold"] is not None), None)
        cell["score"] = [_mean(old), _mean(new)]
        cell["threshold"] = threshold
    return cell


def _first_reason(attempts, metric):
    return next((a["metrics"][metric]["reason"] for a in attempts
                 if metric in a["metrics"] and not a["metrics"][metric]["success"]), "")


def _attempt(a):
    """One attempt in full: the answer, what else the plugin said, and every
    metric's result and reason."""
    time = a["time"]
    return {
        "passed": a["success"],
        "output": _clip(a["output"], OUTPUT_CHARS) if a["output"] else "",
        "extras": [[label, _clip(text, OUTPUT_CHARS)] for label, text in a["extras"]],
        "time": round(time, 1) if isinstance(time, (int, float)) else None,
        "crashed": a["crashed"],
        "retries": a["retries"],
        "error": _clip(a["error"], REASON_CHARS),
        "metrics": [{"name": n, "ok": m["success"], "score": m["score"],
                     "threshold": m["threshold"], "judged": m["judged"],
                     "reason": _clip(m["reason"], REASON_CHARS)}
                    for n, m in a["metrics"].items()],
    }


def _shown_first(b, w):
    """Which attempt of each run to show first, as the pair that shows the
    change: the new run's first failure (or first attempt), and from the base
    run an attempt with the other outcome when there is one."""
    new_i = next((i for i, a in enumerate(w) if not a["success"]), 0)
    want = not w[new_i]["success"] if w else False
    old_i = next((i for i, a in enumerate(b) if a["success"] == want), 0)
    return {"old": old_i, "new": new_i}


def _one_run(name, g, side, flags):
    """A golden that only one run asked, listed with that run's results and
    nothing for the other, so its failures are visible though there is no
    change to work out."""
    attempts, none = g["attempts"], [0, 0]
    b, w = (attempts, []) if side == "base" else ([], attempts)
    metrics = []
    for m in _metric_names(attempts):
        tally = _tally(attempts, m)
        if tally[0] == tally[1]:
            continue  # passed every attempt
        cell = {"old": tally if side == "base" else none, "new": tally if side == "new" else none,
                "delta": None}
        metrics.append({"name": m, **_with_scores(cell, b, w, m),
                        "dots": [_dots(b, m), _dots(w, m)],
                        "reason": _clip(_first_reason(w, m), REASON_CHARS),
                        "oldReason": _clip(_first_reason(b, m), REASON_CHARS)})
    tally = _tally(attempts)
    first = next((i for i, a in enumerate(attempts) if not a["success"]), 0)
    return {
        "name": name, "endpoint": g["endpoint"], "only": side, "flags": flags, "edits": {},
        "input": _clip(g["input"], INPUT_CHARS), "expected": _clip(g["expected"], OUTPUT_CHARS),
        "old": tally if side == "base" else none, "new": tally if side == "new" else none,
        "delta": None,
        "tier": [_tier(*tally), None] if side == "base" else [None, _tier(*tally)],
        "dots": [_dots(b), _dots(w)],
        "metrics": metrics,
        "attempts": {"old": [_attempt(a) for a in b], "new": [_attempt(a) for a in w]},
        "show": {"old": first, "new": first},
    }


def _median_time(attempts):
    times = [a["time"] for a in attempts if isinstance(a["time"], (int, float))]
    return statistics.median(times) if times else None


def _health_side(run, names):
    """Speed and reliability over these goldens' attempts in one run. The
    median time is the median of each golden's median, so a golden asked
    more often doesn't count for more."""
    per_golden = [t for t in (_median_time(run["goldens"][n]["attempts"]) for n in names)
                  if t is not None]
    attempts = [a for n in names for a in run["goldens"][n]["attempts"]]
    latency = [a["metrics"]["Plugin Latency"] for a in attempts if "Plugin Latency" in a["metrics"]]
    return {
        "attempts": len(attempts),
        "median": round(statistics.median(per_golden), 1) if per_golden else None,
        "over": [sum(not m["success"] for m in latency), len(latency)],
        "crashed": sum(a["crashed"] for a in attempts),
        "retried": sum(bool(a["retries"]) for a in attempts),
    }


def compare(base_path: Path, new_path: Path):
    base_run, new_run = load_run(base_path), load_run(new_path)
    base, new = base_run["goldens"], new_run["goldens"]
    suite, flags = _suite()
    common = sorted(set(base) & set(new))
    # Counted like the rest, and marked, so a golden since removed from the
    # suite is easy to spot.
    gone = {n for n in set(base) | set(new) if suite is not None and n not in suite}

    def marks(name):
        return flags.get(name, []) + (["no_longer_in_suite"] if name in gone else [])

    by_endpoint = defaultdict(list)
    for name in common:
        by_endpoint[new[name]["endpoint"]].append(name)

    def pooled(names, metric=None):
        b = [_tally(base[n]["attempts"], metric) for n in names]
        w = [_tally(new[n]["attempts"], metric) for n in names]
        return _cell((sum(x for x, _ in b), sum(y for _, y in b)),
                     (sum(x for x, _ in w), sum(y for _, y in w)))

    endpoints, health = [], []
    for ep, names in by_endpoint.items():
        b_all = [a for n in names for a in base[n]["attempts"]]
        w_all = [a for n in names for a in new[n]["attempts"]]
        metrics = []
        for m in _metric_names(b_all + w_all):
            cell = _with_scores(pooled(names, m), b_all, w_all, m)
            if not cell["old"][1] or not cell["new"][1]:
                cell["only"] = "new" if cell["new"][1] else "base"
            metrics.append({"name": m, **cell})
        endpoints.append({
            "name": ep, "goldens": len(names), "all": pooled(names), "metrics": metrics,
        })
        health.append({"name": ep, "goldens": len(names),
                       "old": _health_side(base_run, names), "new": _health_side(new_run, names)})

    goldens = []
    for name in common:
        b, w = base[name]["attempts"], new[name]["attempts"]
        metrics = []
        for m in _metric_names(b + w):
            cell = _cell(_tally(b, m), _tally(w, m))
            if cell["old"][0] == cell["old"][1] and cell["new"][0] == cell["new"][1]:
                continue  # passed every attempt in both runs: nothing to show
            cell = _with_scores(cell, b, w, m)
            metrics.append({"name": m, **cell,
                            "dots": [_dots(b, m), _dots(w, m)],
                            "reason": _clip(_first_reason(w, m), REASON_CHARS),
                            "oldReason": _clip(_first_reason(b, m), REASON_CHARS)})
        edits = {}
        for key in ("input", "expected"):
            if base[name][key] != new[name][key]:
                edits[key] = _diff(base[name][key], new[name][key])
        cell = _cell(_tally(b), _tally(w))
        goldens.append({
            "name": name,
            "endpoint": new[name]["endpoint"],
            "flags": marks(name),
            "edits": edits,
            "input": _clip(new[name]["input"], INPUT_CHARS),
            "expected": _clip(new[name]["expected"], OUTPUT_CHARS),
            **cell,
            "tier": [_tier(*cell["old"]), _tier(*cell["new"])],
            "dots": [_dots(b), _dots(w)],
            "metrics": metrics,
            "attempts": {"old": [_attempt(a) for a in b], "new": [_attempt(a) for a in w]},
            "show": _shown_first(b, w),
        })

    meanings, judged_by = _metric_info()
    metric_info = {}
    for m in sorted({m["name"] for e in endpoints for m in e["metrics"]}):
        meaning = next((v for k, v in meanings.items() if m.startswith(k)), "")
        how = next((v for k, v in judged_by.items() if m.startswith(k)), "")
        if not how and m.endswith("[GEval]"):
            how = "LLM judge, scored against a rubric"
        metric_info[m] = {"meaning": meaning, "judgedBy": how}

    def side(path, run):
        everything = [a for g in run["goldens"].values() for a in g["attempts"]]
        return {"file": path.name, "label": _label(path), "reps": _reps(run["goldens"]),
                "goldens": len(run["goldens"]), "judges": run["judges"],
                "duration": run["duration"], "passed": list(_tally(everything))}

    only_base, only_new = sorted(set(base) - set(new)), sorted(set(new) - set(base))
    return {
        "base": side(base_path, base_run),
        "new": side(new_path, new_run),
        "overall": pooled(common),
        "onlyBase": only_base,
        "onlyNew": only_new,
        "notInSuite": sorted(gone),
        "endpoints": endpoints,
        "goldens": goldens,
        "oneRun": [_one_run(n, base[n], "base", marks(n)) for n in only_base]
                  + [_one_run(n, new[n], "new", marks(n)) for n in only_new],
        "health": {"overall": {"name": "All endpoints", "goldens": len(common),
                               "old": _health_side(base_run, common),
                               "new": _health_side(new_run, common)},
                   "endpoints": health},
        "metricInfo": metric_info,
        "generated": datetime.now().strftime("%d %b %Y %H:%M"),
    }


def _reps(goldens):
    counts = sorted(len(g["attempts"]) for g in goldens.values())
    return counts[len(counts) // 2] if counts else 0


def _golden_names(path: Path):
    try:
        return {c.get("name") for c in json.loads(path.read_text()).get("testCases") or []}
    except (OSError, ValueError):
        return set()


def pick_base(new_path: Path):
    """The latest run before `new_path` that has at least AUTO_BASE_OVERLAP
    of its goldens, or None."""
    wanted = _golden_names(new_path)
    if not wanted:
        return None
    earlier = [p for p in sorted(RESULTS_DIR.glob("test_run_*.json")) if p.name < new_path.name]
    for path in reversed(earlier):
        if len(wanted & _golden_names(path)) >= AUTO_BASE_OVERLAP * len(wanted):
            return path
    return None


def list_runs():
    """Print the saved runs, oldest first, to choose two from."""
    rows = []
    for path in sorted(RESULTS_DIR.glob("test_run_*.json")):
        try:
            run = load_run(path)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        attempts = [a for g in run["goldens"].values() for a in g["attempts"]]
        passed, total = _tally(attempts)
        rows.append((path.name, _label(path), str(len(run["goldens"])), f"x{_reps(run['goldens'])}",
                     f"{passed}/{total}", f"{passed / total:.0%}" if total else "-",
                     "; ".join(run["judges"]) or "no judge recorded"))
    if not rows:
        sys.exit(f"No runs in {RESULTS_DIR}. Run ./run_evals.sh first.")
    head = ("Run file", "When", "Goldens", "Asked", "Passed", "Rate", "Judge")
    widths = [max(len(r[i]) for r in rows + [head]) for i in range(len(head) - 1)]
    for r in [head] + rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)) + "  " + r[-1])


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="With no runs named, the latest run is compared with the latest earlier run "
               f"that has at least {AUTO_BASE_OVERLAP:.0%} of its goldens.")
    ap.add_argument("runs", nargs="*", type=Path, metavar="RUN",
                    help="BASE NEW (earlier run, then the later one), or just NEW")
    ap.add_argument("-o", "--out", type=Path, help="HTML file to write")
    ap.add_argument("--no-open", dest="open", action="store_false",
                    help="write the dashboard without opening it in your browser")
    # Opening is the default; --open is still accepted.
    ap.add_argument("--open", dest="open", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--list", action="store_true",
                    help="list the saved runs (goldens, attempts, pass rate, judge) and stop")
    args = ap.parse_args()
    if args.list:
        list_runs()
        return

    if len(args.runs) > 2:
        ap.error("name at most two runs: BASE NEW")
    for path in args.runs:
        if not path.is_file():
            ap.error(f"{path} not found")
    if len(args.runs) == 2:
        base_path, new_path = args.runs
    else:
        new_path = args.runs[0] if args.runs else max(RESULTS_DIR.glob("test_run_*.json"), default=None)
        if new_path is None:
            sys.exit(f"No runs in {RESULTS_DIR}. Run ./run_evals.sh first.")
        base_path = pick_base(new_path)
        if base_path is None:
            sys.exit(f"No earlier run has {AUTO_BASE_OVERLAP:.0%} of {new_path.name}'s goldens. "
                     "Name both runs: python reports/dashboard.py BASE NEW")
        print(f"Comparing {base_path.name} (base) with {new_path.name}")

    data = compare(base_path, new_path)
    if not data["goldens"]:
        sys.exit("The two runs have no goldens in common.")
    out = args.out or RESULTS_DIR / (
        f"dashboard_{base_path.stem.replace('test_run_', '')}"
        f"_vs_{new_path.stem.replace('test_run_', '')}.html")
    # "<" only occurs inside JSON strings, where < reads the same, so no
    # answer text can close the <script> it is embedded in.
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    template = (HERE / "dashboard_template.html").read_text()
    out.write_text(template.replace("/*__DATA__*/null", payload))
    print(f"Wrote {out}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
