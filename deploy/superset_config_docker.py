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
#   This repository's own docker compose (development and the acceptance tests) - dev/superset_config.py
#   imports this file and adds what only development needs; docker-compose.yaml sets the environment
#   variables below to point the plugin at the LLM stand-in.
#
# Only the API key has to be filled in. Every other NLSQL_* setting is optional; the commented-out ones
# show their defaults. The ones read from the environment fall back to the values shown.
import os

try:
    from superset_config import FEATURE_FLAGS  # noqa: E402
except ImportError:  # loaded on its own (dev/superset_config.py), not from Superset's superset_config
    FEATURE_FLAGS = {}

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
_API_KEY = os.environ.get("NLSQL_LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "sk-or-PUT-YOUR-KEY-HERE")

NLSQL_LLM_BASE_URL = os.environ.get("NLSQL_LLM_BASE_URL", "https://openrouter.ai/api/v1")
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

# --- Embeddings (used with NLSQL_SEMANTIC_SCHEMA_ENABLED or NLSQL_SCHEMA_EMBEDDINGS) ----------------
NLSQL_EMBEDDING_BASE_URL = os.environ.get("NLSQL_EMBEDDING_BASE_URL", "https://openrouter.ai/api/v1")
NLSQL_EMBEDDING_MODEL = "qwen/qwen3-embedding-8b"
NLSQL_EMBEDDING_DOCUMENT_PREFIX = ""
NLSQL_EMBEDDING_QUERY_PREFIX = (
    "Instruct: Given a natural-language database query, retrieve the "
    "database table most relevant to answering it.\nQuery:"
)
NLSQL_EMBEDDING_API_KEY = _API_KEY
# The bundled semantic schema only describes Superset's example datasets. Leave this off for your own
# databases and use Interrogate DB in SQL Lab instead.
NLSQL_SEMANTIC_SCHEMA_ENABLED = os.environ.get("NLSQL_SEMANTIC_SCHEMA_ENABLED", "false").lower() == "true"
# NLSQL_SCHEMA_TOP_K = 8

# --- Interrogate DB ---------------------------------------------------------------------------------
# NLSQL_INTERROGATE_MAX_TABLES = 3000
# NLSQL_INTERROGATE_ROW_ESTIMATES = True       # one EXPLAIN (never run) per 60 tables
# NLSQL_INTERROGATE_DESCRIPTIONS = False       # also have the model describe tables (many more calls)
# NLSQL_INTERROGATE_SCHEMAS = ["public"]       # only these schemas (default: all)
# NLSQL_INTERROGATE_EXCLUDE_SCHEMAS = [...]    # replaces the built-in list (information_schema, SYS, ...)

# --- Database knowledge for big databases -----------------------------------------------------------
# Reading each database's structure and the site's own descriptions, and choosing the tables a prompt
# gets when they don't all fit. The uncommented lines are the choices for a site; the commented ones
# show the defaults. The README's settings table says what each does.
#
# Reading
# NLSQL_DB_INFO = True                         # off: no structure or dictionary read, no database section in prompts
# NLSQL_INTERROGATE_ON_FIRST_USE = True        # first panel open on an unread database reads it in the background
# NLSQL_INTERROGATE_CHECK_SECONDS = 3600       # how often a panel open looks for added and dropped tables
# NLSQL_INTERROGATE_LOCK_SECONDS = 1800        # one read of a database at a time; must outlast the longest
# NLSQL_SYSTEM_USER = "nlsql-system"           # read every database on a schedule as this user, not on panel open
# NLSQL_SYSTEM_READ_SCHEDULE = 3600            # seconds between those rounds; 0 for start-up only
NLSQL_STRUCTURE_READ = "per_schema"            # a few catalog queries a schema (Oracle, Trino); others per table
# NLSQL_STRUCTURE_TIMEOUT_SECONDS = 120        # each catalog query
#
# The site's descriptions: one fixed SELECT of these columns, at each read (see data_dictionary.py).
# The table's theme column isn't read yet.
NLSQL_DATA_DICTIONARY = {
    "schemas": {"table": "dataset_library.datasets", "columns": {"schema": "name", "description": "description"}},
    # "tables": {"table": ..., "columns": {"schema": ..., "table": ..., "description": ...}},
    # "columns": {"table": ..., "columns": {"schema": ..., "table": ..., "column": ..., "description": ...}},
    # "database": "...",                       # the Superset database it's in; default: the one being read
    # "max_rows": 200000,
}
# NLSQL_DERIVED_COLUMN_SUFFIXES = {"_CLEANSED": "cleansed", "_TRANSLITERATED": "transliterated",
#                                  "_TRANSLATED": "translated"}
# NLSQL_HIDE_CLEANSED_ORIGINALS = True         # leave NAME out where there's NAME_CLEANSED
#
# Choosing the tables
NLSQL_SCHEMA_EMBEDDINGS = True                 # embed every table with the model above; a read that can't says so
# NLSQL_SCHEMA_MAX_CHARS = 120000              # how much structure a prompt gets
# NLSQL_TABLE_SELECTION_TOP_N = None           # the most tables the SQL prompt lists, even when more would fit
# NLSQL_TABLE_SELECTION = "embedding"          # the option everyone gets - see table_selection.py
# The options Admins may pick in the panel, to compare them:
NLSQL_TABLE_SELECTION_ALLOWED = ["embedding", "embedding-then-model", "embedding-then-model-tools"]
# NLSQL_TABLE_SELECTION_ROLES = ["Admin"]      # who may pick
# NLSQL_TABLE_SELECTION_SHORTLIST = 40         # tables offered to the picking call
# NLSQL_TABLE_SELECTION_MAX = 12               # the most it may pick
# NLSQL_TABLE_SELECTION_LOOKS = 3              # how often "embedding-then-model-tools" may look further
# NLSQL_TABLE_SELECTION_MAX_CHARS = 60000      # its prompt's room for tables
# NLSQL_LLM_REASONING_ON = {"reasoning": {"enabled": True}}    # the request body for reasoning on or off, for the
# NLSQL_LLM_REASONING_OFF = {"reasoning": {"enabled": False}}  # "-reasoning" options; depends on the model service

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
NLSQL_PHOENIX_ENDPOINT = os.environ.get("NLSQL_PHOENIX_ENDPOINT") or None  # Arize Phoenix or any OTLP/HTTP collector, e.g. http://phoenix:6006
# NLSQL_PHOENIX_PROJECT = "superset-sql-assistant"
# NLSQL_PHOENIX_API_KEY = None
# NLSQL_PHOENIX_HEADERS = {}
# NLSQL_PHOENIX_INCLUDE_PROMPTS = True
