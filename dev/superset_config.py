# Dev-only Superset config. Never use these values in production.
#
# The plugin's settings are deploy/superset_config_docker.py's, the same file a real deployment uses,
# mounted on Superset's PYTHONPATH (see docker-compose.yaml). docker-compose.yaml's environment
# points it at the LLM stand-in instead of OpenRouter. This file only adds what development needs.
from superset_config_docker import *  # noqa: F403

SECRET_KEY = "dev-only-not-a-secret"

# Metadata DB: SQLite in the persisted volume (fine for local dev)
SQLALCHEMY_DATABASE_URI = "sqlite:////app/superset_home/superset.db"

ENABLE_EXTENSIONS = True  # older docs reference it as a top-level setting too

# This repo, loaded from disk instead of a .supx. Path *inside the container* (see the volume in
# docker-compose.yaml).
LOCAL_EXTENSIONS = [
    "/app/extensions/nltosqlandback",
]
EXTENSIONS_PATH = None

# Every LLM call is logged on the "waxwing.nltosqlandback.llm_calls" logger; also keep a JSONL copy.
NLSQL_LLM_LOG_FILE = "/app/superset_home/llm-calls.jsonl"
NLSQL_LLM_LOG_PROMPTS = True

# Arize Phoenix: `docker compose --profile phoenix up` starts one at http://localhost:6006.
# Set NLSQL_PHOENIX_ENDPOINT in .env (http://phoenix:6006 from inside the compose network) to use it.
NLSQL_PHOENIX_PROJECT = "superset-sql-assistant-dev"

# Superset's own SQL validator for Postgres - the gutter markers in the editor, and one of the checks
# generated SQL has to pass (see backend/src/waxwing/nltosqlandback/sql_checks.py).
SQL_VALIDATORS_BY_ENGINE = {"postgresql": "PostgreSQLValidator"}

# Convenience for local dev
WTF_CSRF_ENABLED = False
TALISMAN_ENABLED = False
