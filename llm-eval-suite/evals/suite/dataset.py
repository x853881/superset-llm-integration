"""Golden questions as a DeepEval EvaluationDataset.

By default goldens come from goldens/ in evals/, one file per endpoint
(goldens/generate-sql.json, ...). GOLDENS_FILE points at another file, or a
folder of them, instead.

Each golden's additional_metadata:
    endpoint        which plugin endpoint to call; see plugin/endpoints.py for
                    the list and the extra fields each one needs. It must
                    match the file the golden is in.
    must_use        tables the answer must read from
    must_not_use    tables the answer must avoid

For SQL endpoints, expected_output is the reference SQL; goldens without one
skip the result-match and judged-correctness metrics. Prose endpoints
(generate-nl, explain-plan, ...) need no expected_output.
"""

import os
from pathlib import Path
from typing import Callable, List, Optional

from deepeval.dataset import EvaluationDataset, Golden

from plugin.endpoints import ENDPOINTS, endpoint_for

DEFAULT_GOLDENS_DIR = Path(__file__).resolve().parents[1] / "goldens"
# Goldens that need no tables, so they can pass on any PostgreSQL database:
# they show that Superset, SQL Lab, the plugin, its model and the judge all
# connect. They are the "smoke" part and never part of any other, so they
# don't change the pass rates of the goldens that test the plugin.
SMOKE_GOLDENS_DIR = DEFAULT_GOLDENS_DIR / "smoke"
SMOKE_PART = "smoke"


def golden_files(path: Optional[Path] = None) -> List[Path]:
    """The files to read goldens from: GOLDENS_FILE if it is a file, else
    every .json file in the folder (goldens/ by default), the endpoints'
    files in ENDPOINTS order and any others after them."""
    path = path or Path(os.getenv("GOLDENS_FILE") or DEFAULT_GOLDENS_DIR)
    if path.is_file():
        return [path]
    files = {f.stem: f for f in path.glob("*.json")}
    if not files:
        raise FileNotFoundError(f"No goldens: no .json files in {path}")
    order = [*ENDPOINTS, *sorted(set(files) - set(ENDPOINTS))]
    return [files[name] for name in order if name in files]


def local_dataset(path: Optional[Path] = None) -> EvaluationDataset:
    dataset = EvaluationDataset()
    for file in golden_files(path):
        dataset.add_goldens_from_json_file(str(file))
    return dataset


def load_dataset(part: Optional[str] = None) -> EvaluationDataset:
    """The goldens `part` is chosen from: the smoke goldens for the smoke
    part, and otherwise the suite's own."""
    if part == SMOKE_PART:
        return local_dataset(SMOKE_GOLDENS_DIR)
    return local_dataset()


# Parts of the suite that can be run on their own (./run_evals.sh <part>),
# grouped by endpoint. Any single endpoint name is also a part.
SUITE_PARTS = {
    "sql": ("generate-sql", "update-sql-from-nl", "refine-sql"),
    "fix": ("explain-sql-error",),
    "accelerate": ("accelerate",),
    "describe": ("generate-nl", "update-nl-from-sql-diff", "describe-breakdown"),
    "explain-plan": ("explain-plan",),
}

def _flag(name: str) -> Callable[[Golden], bool]:
    return lambda g: bool((g.additional_metadata or {}).get(name))


# Parts picked by what their goldens test rather than by endpoint.
GOLDEN_PARTS = {
    # Requests too ambiguous to answer, which the plugin should answer with a
    # question ("expect_clarification": true).
    "clarify": _flag("expect_clarification"),
    # Requests the data can't answer, which the plugin should say so to
    # ("expect_cant_answer": true).
    "unanswerable": _flag("expect_cant_answer"),
    # Easy goldens kept to catch the plugin getting worse at the basics
    # ("regression": true).
    "regression": _flag("regression"),
    # Goldens expected to fail until the plugin changes, e.g. needing metadata
    # it isn't given yet; their notes say what ("waiting_on_plugin": true).
    "waiting": _flag("waiting_on_plugin"),
    # Inputs carrying an instruction addressed to the plugin, in a comment, a
    # string or an error message, which it should ignore ("injection": true).
    "injection": _flag("injection"),
}

# Parts made of other parts, to run together.
PART_GROUPS = {
    # Every endpoint that answers only in plain English.
    "prose": ("describe", "explain-plan"),
}


def select_part(goldens: List[Golden], part: Optional[str]) -> List[Golden]:
    """The goldens one part of the suite runs: every golden for no part or
    "all", otherwise the goldens of the part's endpoints."""
    if part == SMOKE_PART:
        # load_dataset(part) gave only the smoke goldens.
        return goldens
    if not part or part == "all":
        return goldens
    if part in GOLDEN_PARTS:
        return [g for g in goldens if GOLDEN_PARTS[part](g)]
    if part in SUITE_PARTS:
        endpoints = SUITE_PARTS[part]
    elif part in PART_GROUPS:
        endpoints = tuple(e for p in PART_GROUPS[part] for e in SUITE_PARTS[p])
    elif part in ENDPOINTS:
        endpoints = (part,)
    else:
        raise ValueError(
            f"Unknown part {part!r}: use all, "
            + ", ".join([*SUITE_PARTS, *GOLDEN_PARTS, *PART_GROUPS, SMOKE_PART])
            + ", or an endpoint ("
            + ", ".join(ENDPOINTS)
            + ")"
        )
    return [
        g for g in goldens if endpoint_for(g.additional_metadata).name in endpoints
    ]


def golden_id(golden: Golden, index: int) -> str:
    return golden.name or f"golden_{index}"

