"""Shared fixtures: suite config, Superset API client and the judge."""

from dataclasses import replace
from typing import Optional

import pytest
import requests

from plugin.client import SupersetClient
from suite.config import SuiteConfig, unresolved_judge_settings
from suite.schema_metadata import load_schema_metadata


@pytest.fixture(scope="session")
def suite_config() -> SuiteConfig:
    return SuiteConfig.from_env()


@pytest.fixture(scope="session")
def schema_metadata():
    return load_schema_metadata()


@pytest.fixture
def unit_config(suite_config: SuiteConfig) -> SuiteConfig:
    """Hermetic config for unit tests."""
    return replace(
        suite_config,
        superset_url="http://superset:8088",
        username="eval-bot",
        password="not-a-real-password",
        database_id="1",
        schema="public",
        plugin_base_path="/extensions/waxwing/nltosqlandback",
        eval_run_id="unit",
        timing_via="sqllab",
    )


@pytest.fixture
def make_superset_client(unit_config: SuiteConfig):
    def _make(
        session: Optional[requests.Session] = None, **overrides
    ) -> SupersetClient:
        return SupersetClient(
            replace(unit_config, **overrides), session=session
        )

    return _make


@pytest.fixture(scope="session")
def live_superset(suite_config: SuiteConfig) -> SupersetClient:
    def unavailable(msg: str):
        if suite_config.strict_live:
            pytest.fail(msg)
        pytest.skip(msg)

    from judge.model import judge_enabled

    # Every setting still holding its <...>, the judge's too, before the first
    # request, so a missed one is named rather than failing later.
    unresolved = suite_config.unresolved_placeholders()
    if judge_enabled():
        unresolved += unresolved_judge_settings()
    if unresolved:
        unavailable(f"Set real values for: {', '.join(unresolved)}")

    try:
        client = SupersetClient(suite_config)
    except ValueError as e:  # SUPERSET_AUTH or SUPERSET_HEADERS is wrong
        pytest.fail(str(e))
    if not client.health():
        unavailable(f"Superset is not reachable at {suite_config.superset_url}")
    client.login()
    return client


@pytest.fixture(scope="session")
def judge_model():
    """The configured judge, or None when JUDGE_PROVIDER=none. A judge that
    is configured but broken fails the run rather than silently skipping."""
    from judge.model import JudgeConfigError, get_judge_model, judge_enabled

    if not judge_enabled():
        return None
    try:
        return get_judge_model()
    except JudgeConfigError as e:
        pytest.fail(f"{e} (set JUDGE_PROVIDER=none to run without a judge)")
    except Exception as e:  # e.g. DeepEval rejecting a missing API key
        pytest.fail(
            f"Judge model could not be created: {e} Set the provider's API key "
            "in .env.eval, or JUDGE_PROVIDER=none to run without a judge."
        )
