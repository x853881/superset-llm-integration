"""Shared fixtures.

The existing pipeline tests describe the flow with a semantic schema configured (they stub the
schema lookup with `streams.fake_schema_preamble`), so that is what they run with by default; tests
of the schema-less flow - the default in a real deployment - turn it off themselves.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture(autouse=True)
def _semantic_schema_enabled(request):
    if request.node.get_closest_marker("no_semantic_schema"):
        with patch("waxwing.nltosqlandback.nlsql_backend.schema_enabled", return_value=False):
            yield
        return
    with patch("waxwing.nltosqlandback.nlsql_backend.schema_enabled", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _generated_sql_as_written(request):
    """The pipeline tests compare generated SQL with what the fake model wrote, so laying it out
    (see `nlsql_backend.formatted`) is off unless a test asks for it."""
    if request.node.get_closest_marker("formats_sql"):
        yield
        return
    with patch("waxwing.nltosqlandback.nlsql_backend.formatted", side_effect=lambda result: result):
        yield


def pytest_configure(config):
    config.addinivalue_line("markers", "no_semantic_schema: run as a deployment with no semantic schema")
    config.addinivalue_line("markers", "formats_sql: lay generated SQL out, as a deployment does")
