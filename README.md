# Superset LLM Integration

A Superset extension adding an AI assistant panel to SQL Lab: describe, refine, fix, speed up
("Accelerate") and generate SQL with any OpenAI-compatible LLM, beside Superset's own editor. Every
LLM call is logged with the Superset user who made it, and can be sent to Arize Phoenix.

It also adds two tabs under SQL Lab's editor, beside Results and Query history: **Break it down**
(the query laid out and syntax-coloured, explained section by section beside it) and **Query
Estimator** (the planner's view of the query: impact, rows, cost, warnings and the plan tree - the
query is never run). And **Interrogate DB** reads the structure of every schema in the tab's
database - tables, columns, keys, indexes, and the planner's estimate of each table's size; never the
data - so questions about that database come with all of it. SQL the assistant writes is laid out
the way SQL Lab's own Format SQL would before it reaches the editor.

SQL Lab's selected schema is ignored throughout: queries here qualify every table with its schema.

See [docs/architecture.md](docs/architecture.md) and [docs/ui.md](docs/ui.md).

This extension has been built using the Superset Extensions CLI.


## Instructions for development and deploypment

The quick start guide [here](https://superset.apache.org/developer-docs/extensions/quick-start/) contains information about developing and deploying Superset extensions created using the Superset extension CLI.

Install the superset extensions cli tools and build the extension

    pip install apache-superset-extensions-cli
    superset-extensions bundle

The simplest way to get Superset running on a maching with docker is (in a folder alongside the extension):

    git clone https://github.com/apache/superset.git
    cd superset
    git checkout 6.1.0
    docker compose up

Check that runs OK then bring it down with `docker compose down`

Copy the above generated nltosqlandback-0.1.0.supx into your deployment's extensions folder `docker/extensions-installed`

Copy [deploy/superset_config_docker.py](deploy/superset_config_docker.py) to `docker/pythonpath_dev/superset_config_docker.py`
in your Superset checkout (Superset's own `docker/pythonpath_dev/superset_config.py` imports it last). Give it a real
OpenRouter API key - either in the file, or, so it never sits in a file, as `OPENROUTER_API_KEY=...` in
`docker/.env-local`, which Superset's compose file reads if it's there. Every other setting in it is optional (see below):
the file names every setting, the optional ones commented out, so on site it's the one file to copy and edit.

Then restart your Superset deployment with `docker compose up`

### An endpoint that wants a client certificate

If the model endpoint on site wants mutual TLS, the certificate and key go in as files, never in
the `.supx` or the config file. With Superset's compose setup, put them in `docker/certs/` in the
Superset checkout: the compose file already mounts `docker/` at `/app/docker`. Make them readable
only by you (`chmod 600`), and point the config at them:

    NLSQL_LLM_CLIENT_CERT = "/app/docker/certs/client.pem"
    NLSQL_LLM_CLIENT_KEY = "/app/docker/certs/client.key"
    NLSQL_LLM_CA_BUNDLE = "/app/docker/certs/ca.pem"    # if its certificate is from an in-house CA

On Kubernetes, mount a Secret as files the same way. A certificate that comes as a `.p12` or
`.pfx` needs converting to PEM first:

    openssl pkcs12 -in client.p12 -clcerts -nokeys -out client.pem
    openssl pkcs12 -in client.p12 -nocerts -out client.key

An encrypted key needs `NLSQL_LLM_CLIENT_KEY_PASSWORD`, best set in `docker/.env-local` rather than
in a file. The embedding calls use the same certificate unless they're given their own (see below).

## Configuration

All optional, in `superset_config.py`:

| Setting | Default | What it does |
|---|---|---|
| `NLSQL_LLM_BASE_URL` / `NLSQL_LLM_MODEL` / `NLSQL_LLM_API_KEY` | local Ollama, `gemma3:4b` | The OpenAI-compatible chat model. |
| `NLSQL_EMBEDDING_BASE_URL` / `NLSQL_EMBEDDING_MODEL` / `NLSQL_EMBEDDING_API_KEY` | local Ollama, `nomic-embed-text` | The OpenAI-compatible embedding model - only used with `NLSQL_SEMANTIC_SCHEMA_ENABLED`. |
| `NLSQL_EMBEDDING_DOCUMENT_PREFIX` / `NLSQL_EMBEDDING_QUERY_PREFIX` | `search_document: ` / `search_query: ` | Instruction prefixes put before table text and before the request when embedding them (nomic-embed-text's own; most other models want them blank, or their own). |
| `NLSQL_LLM_TIMEOUT_SECONDS` | `600` | How long to wait for a completion. |
| `NLSQL_LLM_REQUEST_USAGE` | `True` | Ask for token usage on streamed calls (`stream_options.include_usage`) - what puts token counts in the log. Turn off for a server that rejects it. |
| `NLSQL_LLM_USER_HEADER` | off | Send the Superset username with every LLM and embedding call, as this HTTP header (e.g. `"X-Superset-User-Name"`) - for a gateway that tracks usage or sets limits per user. The value is percent-encoded UTF-8 (`José` → `Jos%C3%A9`; plain ASCII is sent as it is), so decode it with `unquote`. Not sent for a call that isn't for a user. A username is personal data: think twice before sending it to a hosted provider. |
| `NLSQL_LLM_CLIENT_CERT` / `NLSQL_LLM_CLIENT_KEY` | none | A client certificate for an HTTPS endpoint that wants one (mutual TLS): paths to PEM files the Superset process can read. The certificate file may hold its key, in which case leave `_CLIENT_KEY` unset. Read again whenever a file changes, so a certificate rotated in place needs no restart. |
| `NLSQL_LLM_CLIENT_KEY_PASSWORD` | none | The key's password, if it's encrypted - in the config or as an environment variable of that name. |
| `NLSQL_LLM_CA_BUNDLE` | the system's | The CAs (a PEM file) to trust for the endpoint's own certificate, in place of the system's - for an endpoint with a certificate from an in-house CA. |
| `NLSQL_EMBEDDING_CLIENT_CERT` / `_CLIENT_KEY` / `_CLIENT_KEY_PASSWORD` / `_CA_BUNDLE` | the `NLSQL_LLM_*` ones | The same for the embedding endpoint. |
| `NLSQL_SEMANTIC_SCHEMA_ENABLED` | `False` | Give prompts a schema section from the bundled semantic schema (embedding search). Needs the `NLSQL_EMBEDDING_*` settings. Off, prompts use the tab's database as interrogated by **Interrogate DB**, if it has been. |
| `NLSQL_INTERROGATE_MAX_TABLES` | `3000` | The most tables (and views), across all schemas, one Interrogate DB reads. |
| `NLSQL_INTERROGATE_ROW_ESTIMATES` | `True` | Have the planner estimate each table's row count while interrogating (one `EXPLAIN` per 60 tables, never run; each shows in the user's query history). |
| `NLSQL_INTERROGATE_DESCRIPTIONS` | `False` | Also have the model describe each table and its less obvious columns (one call per ~25 tables) and read table comments. Off, the structure alone is kept - far fewer calls and a much smaller prompt. |
| `NLSQL_INTERROGATE_EXCLUDE_SCHEMAS` | the database's own (`information_schema`, `pg_catalog`, Oracle's `SYS`, `SYSTEM`…) | Schemas never interrogated - a list that replaces the default one. |
| `NLSQL_INTERROGATE_SCHEMAS` | all | Only these schemas, if set. |
| `NLSQL_SCHEMA_MAX_CHARS` | `120000` | How much interrogated structure a prompt gets: past this, column types and indexes go, then the tables sharing fewest words with the request are left out. |
| `NLSQL_FORMAT_GENERATED_SQL` | `True` | Lay generated SQL out one clause per line (Superset's own formatter, i.e. sqlglot) before it reaches the editor. |
| `NLSQL_MARK_TERMS` | `True` | Have the model mark terms a SQL user may not know ("hash join", "O(n log n)") in its explanations; the panel highlights them and shows their meaning on hover. |
| `NLSQL_BREAKDOWN_LARGE_TABLE_ROWS` | `1000000` | Break it down hints at a join or filter on a table at least this big (planner estimate) on a column no index starts with. |
| `NLSQL_SCHEMA_TOP_K` | `8` | How many tables of semantic schema a prompt gets. |
| `NLSQL_VALIDATE_WITH_EXPLAIN` | `True` | Have the database plan (`EXPLAIN`, never run) every query the model writes, as a validity check. |
| `NLSQL_EXPLAIN_TIMEOUT_SECONDS` | `30` | Timeout passed to Superset's `Database.execute` for a plan. |
| `NLSQL_LLM_LOG_ENABLED` | `True` | One `llm_call {json}` line per model call on the `waxwing.nltosqlandback.llm_calls` logger (plus `nlsql_request` and `tool_call` lines). |
| `NLSQL_LLM_LOG_PROMPTS` | `False` | Include prompt and response text in those lines. |
| `NLSQL_LLM_LOG_FILE` | none | Also append them, as JSONL, to this file. |
| `NLSQL_PHOENIX_ENDPOINT` | none | Arize Phoenix (or any OTLP/HTTP collector) base URL, e.g. `http://phoenix:6006`; spans go to `<this>/v1/traces`. |
| `NLSQL_PHOENIX_PROJECT` | `superset-sql-assistant` | The Phoenix project the spans land in. |
| `NLSQL_PHOENIX_API_KEY` / `NLSQL_PHOENIX_HEADERS` | none | Auth for the collector (`Authorization: Bearer …`) / any other headers. |
| `NLSQL_PHOENIX_INCLUDE_PROMPTS` | `True` | Prompt and response text on the spans. |
| `SQL_VALIDATORS_BY_ENGINE` | Superset's | Superset's own setting; when it names a validator for the engine, generated SQL must pass it too. |

Every log line and span carries the Superset user's id and username (`user.id`, `user.name`), read
from the session, along with the request id, database, schema and dialect. A log line looks like:

    llm_call {"callId": "114774af535e9d3a", "endpoint": "accelerate", "user.id": 1, "user.name": "admin",
      "requestId": "live-1", "databaseId": 1, "dialect": "postgres", "traceId": "ccf3…", "purpose": "accelerate",
      "attempt": 1, "gen_ai.system": "openrouter", "gen_ai.request.model": "google/gemma-4-31b-it",
      "gen_ai.usage.input_tokens": 627, "gen_ai.usage.output_tokens": 249, "totalTokens": 876,
      "costUsd": 0.00014736, "timeToFirstTokenMs": 1114, "durationMs": 12982, "outcome": "ok", ...}

The extension never runs SQL for data. The only thing it asks a database to do is plan a query
(`EXPLAIN`), through Superset's own `Database.execute` and permission checks - see
[docs/architecture.md](docs/architecture.md).

## Running with hot reload
create a .env file with your open router key specified
```
OPENROUTER_API_KEY=<your-key>
```

run the following in one terminal. This watches the source files and rebuilds the plugin code
```
superset-extensions dev
```
then run docker compose in another terminal
```
docker compose up
```
it takes a little while on the first run as it is populating the example databae


subsequent changes should reload through automatically.
they seem to take a minute or 2 and require you to manually refresh the browser

To see every LLM call as a trace in Arize Phoenix as well, add `NLSQL_PHOENIX_ENDPOINT=http://phoenix:6006`
to `.env` and start with `docker compose --profile phoenix up`; Phoenix is then at http://localhost:6006.

Backend tests:

    cd backend && ../.venv/bin/python -m pytest

## Explaining SQL errors

The panel offers to explain - and correct - the errors the editor is showing against a query. It
takes them from two places:

- **Failed runs.** Whatever the database said when the query last ran. Always available.
- **Superset's SQL validator.** The same `validate_sql` call that draws the error markers in the
  editor's gutter, which only answers when the deployment has a validator configured for that
  database's engine. That is off by default; to turn it on, set `SQL_VALIDATORS_BY_ENGINE` in your
  Superset config, e.g.

        SQL_VALIDATORS_BY_ENGINE = {"postgresql": "PostgreSQLValidator", "presto": "PrestoDBSQLValidator"}

  Without it the panel simply never has gutter errors to talk about, and falls back to failed runs.

## Streaming

Every LLM-backed endpoint answers with a server-sent event stream: the steps the call plans to take,
each one's outcome and duration as it happens, the model's own reasoning as it thinks (for a model
that reports any), and the answer as it is written. Nothing needs configuring for this, but two
things can hide it:

- **The extension has to be reloaded.** The backend is loaded when Superset starts, so replacing the
  `.supx` is not enough - restart Superset. To check which one is running, look at the request your
  LLM server logs: the current build sends `"stream": true`.
- **A reverse proxy must not buffer the response**, or the panel shows nothing until each call
  finishes. The responses set `X-Accel-Buffering: no` and `Cache-Control: no-cache, no-transform`,
  which is enough for nginx's defaults.

## What takes the time, on a local model

A call against a local model is mostly two waits, and the panel names both as they happen:

- **Reading the prompt.** The schema section dominates it - a request sent with 8 tables of schema
  runs to ~9,000 tokens, which a 12B model on a laptop takes over a minute just to read, before it
  writes anything. `NLSQL_SCHEMA_TOP_K` (default 8) is the lever: fewer tables means a much shorter
  prompt, at the cost of the model not seeing a table it might have needed.
- **Thinking.** A reasoning model can spend minutes here. Its thinking is streamed into the panel as
  it writes it, so the wait is at least legible, but a non-reasoning model of the same size will
  answer far sooner.
