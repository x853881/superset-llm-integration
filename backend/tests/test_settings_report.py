from flask import Flask

from waxwing.nltosqlandback import settings_report


def _summary(**config):
    app = Flask(__name__)
    app.config.update(config)
    with app.app_context():
        return settings_report.summary()


def test_names_hosts_and_models_but_never_the_key():
    text = _summary(
        NLSQL_LLM_BASE_URL="https://models.example.com/v1",
        NLSQL_LLM_MODEL="site-model",
        NLSQL_LLM_API_KEY="secret-value",
        NLSQL_EMBEDDING_BASE_URL="https://embed.example.com/v1",
        NLSQL_EMBEDDING_MODEL="site-embedder",
    )
    assert "model site-model at models.example.com, key set" in text
    assert "embeddings site-embedder at embed.example.com" in text
    assert "secret-value" not in text


def test_shows_the_site_choices():
    text = _summary(
        NLSQL_STRUCTURE_READ="per_schema",
        NLSQL_SCHEMA_EMBEDDINGS=True,
        NLSQL_DATA_DICTIONARY={"schemas": {"table": "dataset_library.datasets",
                                           "columns": {"schema": "name", "description": "description"}}},
        NLSQL_TABLE_SELECTION_ALLOWED=["embedding", "embedding-then-model"],
    )
    assert "structure read per_schema" in text
    assert "table embeddings on" in text
    assert "dictionary dataset_library.datasets" in text
    assert "table selection embedding, allowed embedding, embedding-then-model" in text


def test_the_defaults_when_nothing_is_set():
    text = _summary()
    assert "key not set" in text
    assert "database info on" in text
    assert "structure read per_table" in text
    assert "table embeddings off" in text
    assert "dictionary none" in text


def test_a_setting_that_cannot_be_read_is_reported_not_raised():
    text = _summary(NLSQL_TABLE_SELECTION_ALLOWED=5)
    assert "table selection unreadable (TypeError" in text
    assert "database info on" in text
