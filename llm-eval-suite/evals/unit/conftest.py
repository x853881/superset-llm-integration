"""Fixtures for the unit tests."""

import pytest

from unit.support import EXAMPLE_TABLES


@pytest.fixture(scope="session", autouse=True)
def own_locks(tmp_path_factory):
    """The cross-process locks (suite/locks.py) in a folder of the unit
    tests' own, so they never wait on a live run on this machine, or hold
    one up."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("EVAL_LOCK_DIR", str(tmp_path_factory.mktemp("locks")))
        patch.delenv("EVAL_LOCK_LOG", raising=False)
        yield


@pytest.fixture(scope="module")
def metadata():
    """The example tables, regardless of schema_metadata.json."""
    return {t.name.lower(): t for t in EXAMPLE_TABLES}
