"""Which of a database's tables a user is shown: only those Superset says they may see, and none
when Superset can't say."""

import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional
from unittest.mock import patch

import flask_appbuilder.api as fab_api
import pytest
import superset_core.rest_api.decorators as superset_decorators
from flask import Flask

superset_decorators.api = lambda **_kwargs: (lambda cls: cls)
fab_api.protect = lambda *_args, **_kwargs: (lambda handler: handler)
fab_api.safe = lambda handler: handler

from streams import final_stage, run  # noqa: E402
from test_db_interrogation import _knowledge  # noqa: E402
from waxwing.nltosqlandback import api as extension_api  # noqa: E402
from waxwing.nltosqlandback import knowledge_reads, nlsql_backend, progress, request_context, schema_knowledge, superset_db  # noqa: E402
from waxwing.nltosqlandback.request_context import CallContext  # noqa: E402
from waxwing.nltosqlandback.superset_db import AccessCheckFailed, DatabaseInfo  # noqa: E402

EXAMPLES = DatabaseInfo(id=7, name="examples", backend="postgresql", dialect="postgres", model=None)

#: What each user may see of `_knowledge()`'s tables (public.table_0, sales.table_1, public.table_2).
ACCESS = {
    "wide": {("public", "table_0"), ("sales", "table_1"), ("public", "table_2")},
    "narrow": {("sales", "table_1")},
}


@dataclass(frozen=True)
class _Table:
    table: str
    schema: Optional[str] = None
    catalog: Optional[str] = None


@contextmanager
def _superset(user: Optional[str] = None, fails: bool = False, missing: bool = False):
    """A stand-in for the host's `superset` package, whose security manager answers for `user`."""
    manager = types.SimpleNamespace()
    if not missing:
        def tables(_database, wanted, _catalog):
            if fails:
                raise RuntimeError("permissions table unreachable")
            return [table for table in wanted if (table.schema, table.table) in ACCESS[user]]

        def schemas(_database, _catalog, wanted):
            if fails:
                raise RuntimeError("permissions table unreachable")
            return {schema for schema in wanted if any(schema == seen for seen, _ in ACCESS[user])}

        manager.get_datasources_accessible_by_user = tables
        manager.get_schemas_accessible_by_user = schemas
    host = types.ModuleType("superset")
    host.security_manager = manager
    sql = types.ModuleType("superset.sql")
    parse = types.ModuleType("superset.sql.parse")
    parse.Table = _Table
    with patch.dict(sys.modules, {"superset": host, "superset.sql": sql, "superset.sql.parse": parse}):
        yield


def _preamble(database: Optional[DatabaseInfo] = EXAMPLES):
    context = CallContext(endpoint="generate_sql", database_id=7)
    with request_context.bound(context), patch.object(nlsql_backend.sql_checks, "current_database", return_value=database):
        steps = progress.Steps(nlsql_backend.with_tables_step([nlsql_backend.TABLES_STEP]))
        return run(nlsql_backend.schema_preamble("orders", steps))


def _db_knowledge():
    app = Flask(__name__)
    with app.test_request_context("/?databaseId=7&full=true"), \
            patch("waxwing.nltosqlandback.superset_db.resolve_database", return_value=EXAMPLES):
        return extension_api.NlToSqlAndBackAPI().db_knowledge()


@pytest.fixture(autouse=True)
def _stored():
    schema_knowledge.store.put(_knowledge())
    yield
    schema_knowledge.store.delete(7, None)


def test_a_failing_table_check_shows_no_tables():
    with _superset(fails=True), pytest.raises(AccessCheckFailed):
        superset_db.accessible_tables(EXAMPLES, None, [("sales", "table_1")])


def test_a_missing_table_check_shows_no_tables():
    with _superset(missing=True), pytest.raises(AccessCheckFailed):
        superset_db.accessible_tables(EXAMPLES, None, [("sales", "table_1")])


def test_a_failing_schema_check_shows_no_schemas():
    with _superset(fails=True), pytest.raises(AccessCheckFailed):
        superset_db.accessible_schemas(EXAMPLES, None, ["sales"])


@pytest.mark.no_semantic_schema
@pytest.mark.parametrize("user", ["wide", "narrow"])
def test_a_prompt_has_only_the_tables_its_user_may_see(user):
    with _superset(user):
        preamble, _events = _preamble()
    for schema, name in ACCESS["wide"]:
        assert (f"{schema}.{name}" in preamble) == ((schema, name) in ACCESS[user])


@pytest.mark.no_semantic_schema
def test_a_prompt_has_no_tables_when_the_check_fails_and_says_so():
    with _superset(fails=True):
        preamble, events = _preamble()
    assert "table_" not in preamble
    assert "no tables were used" in final_stage(events, "tables")["detail"]


@pytest.mark.no_semantic_schema
def test_a_prompt_has_no_tables_when_the_database_is_not_one_the_user_may_use():
    with _superset("wide"):
        preamble, events = _preamble(database=None)
    assert "table_" not in preamble
    assert "no tables were used" in final_stage(events, "tables")["detail"]


@pytest.mark.parametrize("user", ["wide", "narrow"])
def test_the_knowledge_endpoint_lists_only_the_tables_its_user_may_see(user):
    with _superset(user):
        response = _db_knowledge()
    listed = {(table["schema"], table["name"]) for table in response.json["result"]["tables"]}
    assert listed == ACCESS[user]
    assert response.json["result"]["tableCount"] == len(ACCESS[user])


def test_the_knowledge_endpoint_refuses_when_the_check_fails():
    with _superset(fails=True):
        response = _db_knowledge()
    assert response.status_code == 403
    assert "tables" not in (response.json.get("result") or {})


def _forget():
    app = Flask(__name__)
    with app.test_request_context("/", method="POST", json={"databaseId": 7}), \
            patch("waxwing.nltosqlandback.superset_db.resolve_database", return_value=EXAMPLES):
        return extension_api.NlToSqlAndBackAPI().forget_db_knowledge()


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(knowledge_reads, "start", lambda kind, _database_id, _catalog: calls.append(kind) or True)
    return calls


def test_the_knowledge_endpoint_starts_the_first_read_and_says_so(started):
    schema_knowledge.store.delete(7, None)
    with _superset("wide"):
        result = _db_knowledge().json["result"]
    assert result["status"] == "none" and result["reading"]["kind"] == "fill"
    assert started == ["fill"]


def test_after_forgetting_nobody_reads_it_again_until_asked(started):
    with _superset("wide"):
        assert _forget().json["result"]["forgotten"] is True
        result = _db_knowledge().json["result"]
    assert result["status"] == "none" and result["reading"]["state"] == knowledge_reads.FORGOTTEN
    assert started == []


def _interrogate_db():
    app = Flask(__name__)
    with app.test_request_context("/", method="POST", json={"databaseId": 7}):
        return extension_api.NlToSqlAndBackAPI().interrogate_db()


@pytest.mark.no_semantic_schema
def test_without_database_information_nothing_is_read_or_used(started, monkeypatch):
    monkeypatch.setattr(superset_db, "db_info_enabled", lambda: False)
    with _superset("wide"):
        assert _db_knowledge().json["result"] == {"status": "off", "databaseName": "examples"}
        assert _interrogate_db().status_code == 403
        preamble, _events = _preamble()
    assert started == []
    assert "table_" not in preamble


def _shortlist(text="what is in table 1"):
    app = Flask(__name__)
    with app.test_request_context("/", method="POST", json={"databaseId": 7, "text": text}), \
            patch("waxwing.nltosqlandback.superset_db.resolve_database", return_value=EXAMPLES):
        return extension_api.NlToSqlAndBackAPI().table_shortlist()


def test_the_shortlist_is_only_for_those_who_may_choose_how_tables_are_picked():
    with _superset("wide"):
        assert _shortlist().status_code == 403


def test_the_shortlist_ranks_only_the_tables_its_user_may_see(monkeypatch):
    from waxwing.nltosqlandback import table_selection

    monkeypatch.setattr(table_selection, "may_choose", lambda: True)
    with _superset("narrow"):
        result = _shortlist().json["result"]
    assert result == {"status": "ready", "strategy": "words", "total": 1, "tables": ["sales.table_1"]}
    with _superset("wide"):
        assert _shortlist("table_2").json["result"]["tables"][0] == "public.table_2"
