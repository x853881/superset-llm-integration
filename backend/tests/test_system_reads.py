"""The scheduled read of every database as a system account: off until configured, one round per
schedule across processes, the full read for unknown databases and the check for the rest."""

from types import SimpleNamespace

import pytest
from flask import Flask

from test_db_interrogation import _knowledge
from waxwing.nltosqlandback import knowledge_reads, schema_knowledge, system_reads

SYSTEM = SimpleNamespace(id=99, username="nlsql-system")


@pytest.fixture
def site(monkeypatch):
    """A Superset with databases 7 (read before) and 8 (never read), and the reads it would run."""
    ran = []
    monkeypatch.setattr(system_reads, "_user", lambda name: SYSTEM if name == SYSTEM.username else None)
    monkeypatch.setattr(system_reads, "_databases", lambda user: [7, 8])

    def read(_app, kind, database_id, catalog, user_id, _username):
        ran.append((kind, database_id, user_id))
        knowledge_reads._running.discard((database_id, catalog or ""))

    monkeypatch.setattr(knowledge_reads, "_background", read)
    schema_knowledge.store.put(_knowledge())
    for database_id in (7, 8):
        schema_knowledge.store.delete_record(knowledge_reads._status_name(database_id, None))
    schema_knowledge.store.delete_record(system_reads._RECORD)
    yield ran
    schema_knowledge.store.delete(7, None)
    schema_knowledge.store.delete_record(system_reads._RECORD)


def _app(**config):
    app = Flask(__name__)
    app.config.update(config)
    return app


def test_nothing_runs_until_a_system_user_is_configured(site):
    assert system_reads.run_round(_app()) == {"ran": False, "why": "off"}
    with _app().app_context():
        assert system_reads.start_timer() is False
    assert site == []


def test_a_round_reads_unknown_databases_and_checks_the_rest_as_the_system_user(site):
    result = system_reads.run_round(_app(NLSQL_SYSTEM_USER=SYSTEM.username))
    assert result == {"ran": True, "databases": {"7": "check", "8": "fill"}}
    assert site == [("check", 7, 99), ("fill", 8, 99)]
    with _app().app_context():
        assert system_reads.last_round()["databases"] == {"7": "check", "8": "fill"}


def test_a_second_process_within_the_schedule_skips_the_round(site):
    app = _app(NLSQL_SYSTEM_USER=SYSTEM.username)
    system_reads.run_round(app)
    assert system_reads.run_round(app) == {"ran": False, "why": "recent"}
    assert len(site) == 2


def test_a_round_held_elsewhere_is_skipped(site):
    app = _app(NLSQL_SYSTEM_USER=SYSTEM.username)
    with app.app_context(), knowledge_reads.held(system_reads._LOCK_NAMESPACE, 60, "busy", round="all"):
        assert system_reads.run_round(app) == {"ran": False, "why": "busy"}
    assert site == []


def test_a_forgotten_database_waits_for_someone_to_ask(site):
    app = _app(NLSQL_SYSTEM_USER=SYSTEM.username)
    with app.app_context():
        knowledge_reads.mark_forgotten(8, None, "alice")
    assert system_reads.run_round(app)["databases"] == {"7": "check", "8": "forgotten"}


def test_an_unknown_system_user_reads_nothing(site):
    assert system_reads.run_round(_app(NLSQL_SYSTEM_USER="nobody")) == {"ran": False, "why": "no such user"}
    assert site == []


def test_with_a_system_user_opening_the_panel_starts_no_read(site, monkeypatch):
    started = []
    monkeypatch.setattr(knowledge_reads, "start", lambda kind, *_args: started.append(kind) or True)
    with _app(NLSQL_SYSTEM_USER=SYSTEM.username).app_context():
        assert knowledge_reads.for_panel(8, None, None) is None
    with _app().app_context():
        knowledge_reads.for_panel(8, None, None)
    assert started == ["fill"]
