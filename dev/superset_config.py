# Dev-only Superset config. Never use these values in production.
import os

SECRET_KEY = "dev-only-not-a-secret"

# Metadata DB: SQLite in the persisted volume (fine for local dev)
SQLALCHEMY_DATABASE_URI = "sqlite:////app/superset_home/superset.db"

FEATURE_FLAGS = {
    "ENABLE_EXTENSIONS": True,
}
ENABLE_EXTENSIONS = True  # older docs reference it as a top-level setting too

# Path *inside the container* (see the volume in docker-compose.yml)
LOCAL_EXTENSIONS = [
    "/app/extensions/nltosqlandback",
]

# --- nltosqlandback extension settings ---
# The key comes from the environment (see .env / docker-compose.yml) so it
# never gets committed to the repo.
_OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

NLSQL_LLM_BASE_URL = "https://openrouter.ai/api/v1"
NLSQL_LLM_MODEL = "google/gemma-4-31b-it"
NLSQL_LLM_API_KEY = _OPENROUTER_API_KEY

NLSQL_EMBEDDING_BASE_URL = "https://openrouter.ai/api/v1"
NLSQL_EMBEDDING_MODEL = "qwen/qwen3-embedding-8b"
NLSQL_EMBEDDING_DOCUMENT_PREFIX = ""
NLSQL_EMBEDDING_QUERY_PREFIX = (
	"Instruct: Given a natural-language database query, retrieve the "
	"database table most relevant to answering it.\nQuery:"
)
NLSQL_EMBEDDING_API_KEY = _OPENROUTER_API_KEY

# The bundled semantic schema describes the example datasets, so use it here. Off by default in a
# real deployment, where there usually isn't one (see docs/architecture.md).
NLSQL_SEMANTIC_SCHEMA_ENABLED = True

# Every LLM call is logged on the "waxwing.nltosqlandback.llm_calls" logger; also keep a JSONL copy.
NLSQL_LLM_LOG_FILE = "/app/superset_home/llm-calls.jsonl"
NLSQL_LLM_LOG_PROMPTS = True

# Arize Phoenix: `docker compose --profile phoenix up` starts one at http://localhost:6006.
# Set NLSQL_PHOENIX_ENDPOINT in .env (http://phoenix:6006 from inside the compose network) to use it.
NLSQL_PHOENIX_ENDPOINT = os.environ.get("NLSQL_PHOENIX_ENDPOINT") or None
NLSQL_PHOENIX_PROJECT = "superset-sql-assistant-dev"

# Superset's own SQL validator for Postgres - the gutter markers in the editor, and one of the checks
# generated SQL has to pass (see backend/src/waxwing/nltosqlandback/sql_checks.py).
SQL_VALIDATORS_BY_ENGINE = {"postgresql": "PostgreSQLValidator"}

# Convenience for local dev
WTF_CSRF_ENABLED = False
TALISMAN_ENABLED = False
