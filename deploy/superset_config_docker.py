# Superset config for the nltosqlandback (AI SQL assistant) extension.
#
# Where this goes:
#   Superset's docker compose setup - copy this file to docker/pythonpath_dev/superset_config_docker.py
#   in your Superset checkout (Superset's own docker/pythonpath_dev/superset_config.py imports it last),
#   and put nltosqlandback-0.1.0.supx in docker/extensions-installed/. Then `docker compose up`.
#
#   Any other deployment - add everything below FEATURE_FLAGS to the superset_config.py that
#   SUPERSET_CONFIG_PATH points at, merge "ENABLE_EXTENSIONS": True into your own FEATURE_FLAGS, and set
#   EXTENSIONS_PATH to the folder holding the .supx.
#
# Only the API key has to be filled in. Every other NLSQL_* setting is optional; the commented-out ones
# show their defaults.
import os

from superset_config import FEATURE_FLAGS  # noqa: E402

FEATURE_FLAGS = {
    **FEATURE_FLAGS,
    "ENABLE_EXTENSIONS": True,
}

# The folder Superset loads .supx files from (inside the container).
EXTENSIONS_PATH = "/app/docker/extensions-installed"

# --- The model --------------------------------------------------------------------------------------
# Any OpenAI-compatible endpoint works (OpenRouter, OpenAI, a local Ollama at http://host:11434/v1, ...).
# Put the key here, or leave this line as it is and set OPENROUTER_API_KEY in the container's
# environment (e.g. docker/.env-local) so it never sits in a file.
_API_KEY = os.environ.get("OPENROUTER_API_KEY", "sk-or-PUT-YOUR-KEY-HERE")

NLSQL_LLM_BASE_URL = "https://openrouter.ai/api/v1"
NLSQL_LLM_MODEL = "google/gemma-4-31b-it"
NLSQL_LLM_API_KEY = _API_KEY
# NLSQL_LLM_TIMEOUT_SECONDS = 600
# NLSQL_LLM_REQUEST_USAGE = True      # turn off if your endpoint rejects stream_options.include_usage
# NLSQL_LLM_USER_HEADER = "X-Superset-User-Name"  # send the Superset username with LLM/embedding calls
# For an HTTPS endpoint that wants a client certificate (mutual TLS) and/or uses an in-house CA.
# Paths inside the container: Superset's compose file mounts its docker/ folder at /app/docker, so
# files put in docker/certs/ in the Superset checkout are at /app/docker/certs/. The embedding calls
# use the same ones unless NLSQL_EMBEDDING_CLIENT_CERT etc. are set.
# NLSQL_LLM_CLIENT_CERT = "/app/docker/certs/client.pem"   # PEM; may hold the key too
# NLSQL_LLM_CLIENT_KEY = "/app/docker/certs/client.key"    # the key, if it's a file of its own
# NLSQL_LLM_CLIENT_KEY_PASSWORD = os.environ.get("NLSQL_LLM_CLIENT_KEY_PASSWORD")  # only for an encrypted key
# NLSQL_LLM_CA_BUNDLE = "/app/docker/certs/ca.pem"         # trust only these CAs for the endpoint

# --- Embeddings (only used with NLSQL_SEMANTIC_SCHEMA_ENABLED = True) -------------------------------
NLSQL_EMBEDDING_BASE_URL = "https://openrouter.ai/api/v1"
NLSQL_EMBEDDING_MODEL = "qwen/qwen3-embedding-8b"
NLSQL_EMBEDDING_DOCUMENT_PREFIX = ""
NLSQL_EMBEDDING_QUERY_PREFIX = (
    "Instruct: Given a natural-language database query, retrieve the "
    "database table most relevant to answering it.\nQuery:"
)
NLSQL_EMBEDDING_API_KEY = _API_KEY
# The bundled semantic schema only describes Superset's example datasets. Leave this off for your own
# databases and use Interrogate DB in SQL Lab instead.
# NLSQL_SEMANTIC_SCHEMA_ENABLED = False
# NLSQL_SCHEMA_TOP_K = 8

# --- Interrogate DB ---------------------------------------------------------------------------------
# NLSQL_INTERROGATE_MAX_TABLES = 3000
# NLSQL_INTERROGATE_ROW_ESTIMATES = True       # one EXPLAIN (never run) per 60 tables
# NLSQL_INTERROGATE_DESCRIPTIONS = False       # also have the model describe tables (many more calls)
# NLSQL_INTERROGATE_SCHEMAS = ["public"]       # only these schemas (default: all)
# NLSQL_INTERROGATE_EXCLUDE_SCHEMAS = [...]    # replaces the built-in list (information_schema, SYS, ...)
# NLSQL_SCHEMA_MAX_CHARS = 120000              # how much structure a prompt gets

# --- Generated SQL ----------------------------------------------------------------------------------
# NLSQL_VALIDATE_WITH_EXPLAIN = True           # plan (never run) every query the model writes
# NLSQL_EXPLAIN_TIMEOUT_SECONDS = 30
# NLSQL_FORMAT_GENERATED_SQL = True
# NLSQL_BREAKDOWN_LARGE_TABLE_ROWS = 1000000
# NLSQL_MARK_TERMS = True                     # highlight hard terms in explanations, meaning on hover
# Superset's own validator, which generated SQL must also pass when one is set for the engine:
# SQL_VALIDATORS_BY_ENGINE = {"postgresql": "PostgreSQLValidator"}

# --- Logging and tracing ----------------------------------------------------------------------------
# NLSQL_LLM_LOG_ENABLED = True                 # llm_call lines on the waxwing.nltosqlandback.llm_calls logger
# NLSQL_LLM_LOG_PROMPTS = False                # include prompt and response text in them
# NLSQL_LLM_LOG_FILE = "/app/superset_home/llm-calls.jsonl"
# NLSQL_PHOENIX_ENDPOINT = "http://phoenix:6006"   # Arize Phoenix or any OTLP/HTTP collector
# NLSQL_PHOENIX_PROJECT = "superset-sql-assistant"
# NLSQL_PHOENIX_API_KEY = None
# NLSQL_PHOENIX_HEADERS = {}
# NLSQL_PHOENIX_INCLUDE_PROMPTS = True
