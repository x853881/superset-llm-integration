"""Filling a database's knowledge on first use and keeping it current: every schema the connection
can see is read, merged into what's kept, one read at a time, and narrowed to each user when used."""

import json
import re
import threading
import time
from unittest.mock import patch

import pytest
from flask import Flask

from streams import final_stage, run
from test_db_interrogation import FakeModel, FakeType, _with_table
from waxwing.nltosqlandback import db_interrogation, knowledge_reads, request_context, schema_knowledge, superset_db
from waxwing.nltosqlandback.request_context import CallContext
from waxwing.nltosqlandback.superset_db import DatabaseInfo

NAMES = re.compile(r"\b(public|archive|orders|customers|old_orders|order_summary|returns)\b")


class ChangingModel(FakeModel):
    """`FakeModel` whose tables can be added and dropped, and whose schemas and tables can fail."""

    def __init__(self):
        super().__init__()
        self.tables = {"public": {"orders": "table", "customers": "table", "order_summary": "view"}, "archive": {"old_orders": "table"}}
        self.columns["returns"] = [{"column_name": "id", "type": FakeType("INTEGER"), "nullable": False}]
        self.unlistable: set[str] = set()
        self.unreadable: set[str] = set()
        self.read: list[str] = []

    def get_all_table_names_in_schema(self, catalog, schema, force=False):
        if schema in self.unlistable:
            raise RuntimeError("ORA-01031: insufficient privileges\n[SQL: SELECT owner FROM secret_schema]")
        return {(name, schema, catalog) for name, kind in self.tables.get(schema, {}).items() if kind == "table"}

    def get_all_view_names_in_schema(self, catalog, schema, force=False):
        return {(name, schema, catalog) for name, kind in self.tables.get(schema, {}).items() if kind == "view"}

    def get_columns(self, table):
        self.read.append(table.table)
        if table.table in self.unreadable:
            raise RuntimeError("the connection went away")
        return self.columns[table.table]


@pytest.fixture(autouse=True)
def _clean():
    schema_knowledge.store.clear_memory()
    yield
    schema_knowledge.store.clear_memory()


@pytest.fixture
def model(monkeypatch):
    _with_table(monkeypatch)
    monkeypatch.setattr(db_interrogation, "_config", lambda name, default: False if name == "NLSQL_INTERROGATE_ROW_ESTIMATES" else default)
    return ChangingModel()


def _database(model):
    return DatabaseInfo(id=7, name="examples", backend="postgresql", dialect="postgres", model=model)


def _read(model, changes_only=False, user="alice", sees=None):
    """Reads the database as `user`, who may see only the schemas in `sees` (all, when None)."""
    context = CallContext(endpoint="interrogate_db", database_id=7, username=user)

    def accessible(_database, _catalog, tables):
        return [table for table in tables if sees is None or table[0] in sees]

    with request_context.bound(context), \
            patch.object(superset_db, "resolve_database", lambda _id: _database(model)), \
            patch.object(superset_db, "accessible_tables", accessible):
        return run(db_interrogation.interrogate(changes_only=changes_only))


def _kept():
    return schema_knowledge.store.get(7, None, fresh=True)


def _names(knowledge):
    return sorted(f"{table.schema}.{table.name}" for table in knowledge.tables)


ALL = ["archive.old_orders", "public.customers", "public.order_summary", "public.orders"]


def test_every_schema_the_connection_sees_is_read_but_the_reader_is_told_only_of_their_own(model):
    summary, _events = _read(model, sees={"archive"})
    assert _names(_kept()) == ALL
    assert summary["tableCount"] == 1 and [schema["name"] for schema in summary["schemas"]] == ["archive"]


def test_the_progress_of_a_read_names_no_schema_or_table(model):
    model.unreadable = {"customers"}
    _summary, events = _read(model)
    assert not NAMES.search(json.dumps(events))


def test_a_problem_is_shown_only_to_users_who_may_see_what_it_is_about(model):
    model.unreadable = {"customers"}
    _read(model)
    kept = _kept()
    assert any("public.customers" in problem for problem in kept.summary()["problems"])
    assert not any("customers" in problem for problem in kept.summary({("archive", "old_orders")})["problems"])
    assert not any("customers" in problem for problem in kept.summary({("public", "orders")})["problems"])
    assert any("customers" in problem for problem in kept.summary({("public", "customers")})["problems"])


def test_a_schema_that_cannot_be_listed_keeps_what_was_read_of_it(model):
    _read(model)
    model.unlistable = {"archive"}
    summary, _events = _read(model)
    assert _names(_kept()) == ALL
    problem = next(problem for problem in summary["problems"] if "archive" in problem)
    assert "couldn't list its tables" in problem and "SQL" not in problem and "secret" not in problem


def test_a_table_that_cannot_be_read_keeps_what_was_read_of_it(model):
    _read(model)
    model.unreadable = {"orders"}
    _read(model)
    orders = next(table for table in _kept().tables if table.name == "orders")
    assert [column.name for column in orders.columns] == ["id", "cust_id", "status", "amt"]


def test_reading_again_drops_the_tables_that_have_gone(model):
    _read(model)
    del model.tables["public"]["customers"]
    _read(model)
    assert "public.customers" not in _names(_kept())


def test_the_check_reads_only_the_added_tables_and_drops_the_gone_ones(model):
    _read(model)
    first = _kept()
    model.tables["public"]["returns"] = "table"
    del model.tables["public"]["customers"]
    model.read.clear()
    time.sleep(0.01)
    _summary, events = _read(model, changes_only=True)
    assert model.read == ["returns"]
    kept = _kept()
    assert _names(kept) == ["archive.old_orders", "public.order_summary", "public.orders", "public.returns"]
    assert kept.created_at == first.created_at and kept.changed_at > first.created_at
    assert final_stage(events, "read")["detail"] == "1 added, 1 dropped"


def test_the_check_reads_nothing_when_nothing_has_changed(model):
    _read(model)
    first = _kept()
    model.read.clear()
    time.sleep(0.01)
    _summary, events = _read(model, changes_only=True)
    assert model.read == []
    assert final_stage(events, "read")["detail"] == "No tables added or dropped"
    kept = _kept()
    assert kept.checked_at > first.checked_at and kept.version == first.version


def test_the_check_reads_everything_when_nothing_is_kept(model):
    _read(model, changes_only=True)
    assert _names(_kept()) == ALL


# --- One read at a time -------------------------------------------------------------------------


def _endless():
    yield {"type": "stage", "key": "read", "label": "Reading", "detail": "Schema 1 of 2"}
    yield {"type": "stage", "key": "read", "label": "Reading", "detail": "Schema 2 of 2"}
    return {"done": True}


@pytest.fixture
def bound():
    with request_context.bound(CallContext(endpoint="interrogate_db", database_id=7, username="alice")):
        yield


def test_a_second_read_of_a_database_is_refused_while_one_runs(bound):
    first = knowledge_reads.tracked("read", _endless())
    next(first)
    assert knowledge_reads.status(7, None)["state"] == knowledge_reads.READING
    with pytest.raises(knowledge_reads.Busy, match="being read already by alice"):
        next(knowledge_reads.tracked("read", _endless()))
    assert run(first)[0] == {"done": True}
    assert knowledge_reads.status(7, None)["state"] == knowledge_reads.DONE
    assert run(knowledge_reads.tracked("read", _endless()))[0] == {"done": True}


def test_a_read_says_how_far_it_has_got(bound, monkeypatch):
    monkeypatch.setattr(knowledge_reads, "STATUS_EVERY_SECONDS", 0)
    stream = knowledge_reads.tracked("read", _endless())
    next(stream)
    assert knowledge_reads.status(7, None)["detail"] == "Reading: Schema 1 of 2"


def test_a_read_that_fails_or_is_stopped_says_so_and_lets_go(bound):
    def failing():
        yield {"type": "stage", "key": "read", "label": "Reading", "detail": None}
        raise RuntimeError("ORA-00942\n[SQL: SELECT * FROM all_tab_columns WHERE owner IN ('HR')]")

    with pytest.raises(RuntimeError):
        run(knowledge_reads.tracked("read", failing()))
    failed = knowledge_reads.status(7, None)
    assert failed["state"] == knowledge_reads.FAILED and failed["error"] == "ORA-00942"

    stopped = knowledge_reads.tracked("read", _endless())
    next(stopped)
    stopped.close()
    assert knowledge_reads.status(7, None)["error"] == "Stopped before it finished."
    assert run(knowledge_reads.tracked("read", _endless()))[0] == {"done": True}


def test_a_read_whose_process_died_is_reported_as_failed():
    knowledge_reads._put_status(7, None, {"state": knowledge_reads.READING, "updatedAt": time.time() - knowledge_reads.DEFAULT_LOCK_SECONDS - 1})
    assert knowledge_reads.status(7, None)["state"] == knowledge_reads.FAILED


# --- When reads start ---------------------------------------------------------------------------


@pytest.fixture
def started(monkeypatch):
    calls = []

    def start(kind, database_id, catalog):
        calls.append(kind)
        return True

    monkeypatch.setattr(knowledge_reads, "start", start)
    return calls


def _knowledge(checked_ago):
    knowledge = schema_knowledge.DatabaseKnowledge(database_id=7, database_name="examples", catalog=None, tables=[])
    knowledge.created_at = knowledge.checked_at = time.time() - checked_ago
    return knowledge


def test_the_first_user_of_a_database_starts_it_being_read(started):
    reading = knowledge_reads.for_panel(7, None, None)
    assert started == ["fill"] and reading["state"] == knowledge_reads.READING and reading["kind"] == "fill"


def test_a_running_read_is_reported_not_started_again(started, bound):
    stream = knowledge_reads.tracked("read", _endless())
    next(stream)
    assert knowledge_reads.for_panel(7, None, None)["by"] == "alice"
    assert started == []


def test_a_forgotten_database_is_not_read_until_someone_asks(started):
    knowledge_reads.mark_forgotten(7, None, "bob")
    assert knowledge_reads.for_panel(7, None, None)["state"] == knowledge_reads.FORGOTTEN
    assert started == []


def test_a_failed_read_is_not_started_again_at_once(started, monkeypatch):
    knowledge_reads._put_status(7, None, {"state": knowledge_reads.FAILED, "error": "no tables", "updatedAt": time.time()})
    assert knowledge_reads.for_panel(7, None, None)["error"] == "no tables"
    assert started == []
    monkeypatch.setattr(knowledge_reads, "RETRY_SECONDS", 0)
    knowledge_reads.for_panel(7, None, None)
    assert started == ["fill"]


def test_no_read_starts_on_first_use_when_that_is_turned_off(started, monkeypatch):
    monkeypatch.setattr(knowledge_reads, "_first_use", lambda: False)
    assert knowledge_reads.for_panel(7, None, None) is None
    assert started == []


def test_the_check_starts_once_the_last_is_old_enough(started, monkeypatch):
    assert knowledge_reads.for_panel(7, None, _knowledge(checked_ago=60)) is None
    assert knowledge_reads.for_panel(7, None, _knowledge(checked_ago=4000))["kind"] == "check"
    monkeypatch.setattr(knowledge_reads, "_check_seconds", lambda: 0)
    knowledge_reads.for_panel(7, None, _knowledge(checked_ago=99_999))
    assert started == ["check"]


def test_a_background_read_fills_the_knowledge(model):
    app = Flask(__name__)
    with app.app_context(), patch.object(superset_db, "resolve_database", lambda _id: _database(model)):
        knowledge_reads._running.add((7, ""))
        assert not knowledge_reads.start("fill", 7, None)  # this process is reading it already
        knowledge_reads._running.clear()
        assert knowledge_reads.start("fill", 7, None)
        for thread in threading.enumerate():
            if thread.name == "nltosqlandback-fill-7":
                thread.join(10)
    assert _names(_kept()) == ALL
    assert knowledge_reads.status(7, None)["state"] == knowledge_reads.DONE
    assert knowledge_reads._running == set()
