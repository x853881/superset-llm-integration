"""In a run of several (--runs N), steady goldens are asked once
(suite/steady.py): their later runs are deselected here, in every worker
alike, as each reads the same results files."""

import os
from pathlib import Path

import pytest

from plugin.endpoints import endpoint_for
from suite.steady import steady_goldens

# The names of the goldens this run asks once.
ASKED_ONCE = pytest.StashKey[frozenset]()

# The parameter pytest-repeat gives each run of a test: 0 for the first.
REPEAT_STEP = "__pytest_repeat_step_number"


def _question(golden) -> str:
    meta = golden.additional_metadata or {}
    return endpoint_for(meta).describe(golden.input, meta)


def pytest_collection_modifyitems(config, items):
    config.stash[ASKED_ONCE] = frozenset()
    results = os.getenv("DEEPEVAL_RESULTS_FOLDER")
    if (getattr(config.option, "count", 1) or 1) < 2 or os.getenv("EVAL_ALL_RUNS") or not results:
        return
    goldens = {
        item.callspec.params["golden"].name: item.callspec.params["golden"]
        for item in items
        if hasattr(item, "callspec") and "golden" in item.callspec.params
    }
    judge = None
    if os.getenv("JUDGE_PROVIDER", "openai").strip().lower() != "none":
        judge = os.getenv("JUDGE_MODEL_NAME", "").strip() or None
    once = frozenset(
        steady_goldens(
            {name: _question(g) for name, g in goldens.items() if name},
            Path(results),
            judge,
        )
    )
    config.stash[ASKED_ONCE] = once
    if not once:
        return
    keep, dropped = [], []
    for item in items:
        params = getattr(item, "callspec", None) and item.callspec.params
        later = params and params.get(REPEAT_STEP, 0) > 0
        if later and params["golden"].name in once:
            dropped.append(item)
        else:
            keep.append(item)
    config.hook.pytest_deselected(items=dropped)
    items[:] = keep


@pytest.fixture
def asked_once(request) -> bool:
    """Whether this test's golden is asked only once in this run."""
    golden = request.node.callspec.params.get("golden")
    return golden is not None and golden.name in request.config.stash.get(ASKED_ONCE, frozenset())
