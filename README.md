# Superset LLM Integration

A Superset extension adding an AI assistant panel to SQL Lab: describe, refine, fix, speed up
("Accelerate") and generate SQL with any OpenAI-compatible LLM, beside Superset's own editor. Every
LLM call is logged with the Superset user who made it, and can be sent to Arize Phoenix.

It also adds two tabs under SQL Lab's editor, beside Results and Query history: **Break it down**
(the query laid out and syntax-coloured, explained section by section beside it) and **Query
Estimator** (the planner's view of the query: impact, rows, cost, warnings and the plan tree - the
query is never run). And the assistant reads the structure of every schema in the tab's database -
tables, columns, keys, indexes, and the planner's estimate of each table's size; never the data - so
questions about that database come with it, narrowed to the tables each user may see. The first
person to open the panel on a database starts that read in the background, and it's checked for
added and dropped tables now and then after. SQL the assistant writes is laid out
the way SQL Lab's own Format SQL would before it reaches the editor.

SQL Lab's selected schema is ignored throughout: queries here qualify every table with its schema.

See [docs/architecture.md](docs/architecture.md) and [docs/ui.md](docs/ui.md). Built with the
[Superset Extensions CLI](https://superset.apache.org/developer-docs/extensions/quick-start/).

The acceptance tests are in [e2e/](e2e/). They are Playwright tests that drive SQL Lab against this
repo's docker compose, with an LLM stand-in that replays recorded model answers, so they need no API
key. See [e2e/README.md](e2e/README.md) for setup, the harness's replay, record and pass-through
modes, and how to write tests.

## Contents

- [Two ways to run it: pick one](#two-ways-to-run-it-pick-one)
- [Option 1: Local development (recommended to start)](#option-1-local-development-recommended-to-start)
- [Option 2: Install into your own Superset](#option-2-install-into-your-own-superset)
- [Troubleshooting](#troubleshooting)
- [Configuration](#configuration)

## Two ways to run it: pick one

| | **Option 1: Local development** | **Option 2: Install into your own Superset** |
|---|---|---|
| Use it when | You want to try the extension or work on its code | You already run Superset and want to add the extension to it |
| Superset comes from | This repo's `docker-compose.yaml` (starts its own Superset) | Your own Superset checkout, e.g. `../superset` |
| API key goes in | `.env` in **this** repo | `docker/.env-local` (or `superset_config_docker.py`) in **your Superset checkout** |
| Code changes | Picked up automatically | Rebuild the `.supx` and restart Superset |

> **Don't run both at once.** They are alternatives, and both use port 8088. Keys and config files
> for one are never read by the other.

### What you need

- [Docker Desktop](https://www.docker.com/products/docker-desktop/), running
- Python 3.9+
- An [OpenRouter API key](https://openrouter.ai/keys) (starts with `sk-or-v1-`)

## Option 1: Local development (recommended to start)

Run everything from the root of this repo.

**1. Install the extensions CLI** (one time). It goes into the repo's virtualenv, so it isn't on
your global `PATH`:

```sh
python3 -m venv .venv
.venv/bin/pip install apache-superset-extensions-cli
```

**2. Add your API key** (one time). Create a file called `.env` in the repo root (next to
`docker-compose.yaml`) containing:

```sh
OPENROUTER_API_KEY=sk-or-v1-your-key-here
```

`.env` is gitignored, so the key is never committed. Note that the file name starts with a dot, so
Finder and `ls` hide it (use `ls -a`, or ⌘⇧. in Finder). If PyCharm doesn't show it, use
**File → Reload All from Disk**.

**3. Start the build watcher** in one terminal and leave it running. It rebuilds the extension
whenever you change the code:

```sh
.venv/bin/superset-extensions dev
```

(Or `source .venv/bin/activate` first, then `superset-extensions dev`. The command has a hyphen:
`superset extensions dev` won't work.)

**4. Start Superset** in a second terminal:

```sh
docker compose up
```

The first start takes a few minutes while it loads the example databases. Later starts are quick.

**5. Open http://localhost:8088** and log in as `admin` / `admin`. Open **SQL Lab**, and the
assistant panel is beside the editor.

### Day to day

- **Code changes**: with the watcher running, they are picked up within a minute or two. Refresh
  the browser to see them.
- **Changed `.env`?** Restarting isn't enough. Recreate the container so it reads the new value:
  `docker compose up -d --force-recreate superset`
- **See the logs**: `docker compose logs -f superset`
- **Start over with a clean database**: `docker compose down -v` (deletes the example data, which is
  reloaded on the next start)
- **Run the backend tests**: `cd backend && ../.venv/bin/python -m pytest`

### Optional: trace every LLM call in Arize Phoenix

Add this line to `.env`:

```sh
NLSQL_PHOENIX_ENDPOINT=http://phoenix:6006
```

Then start with `docker compose --profile phoenix up`. Phoenix is at http://localhost:6006.

## Option 2: Install into your own Superset

Use this to add the extension to an existing Superset deployment. Every command after step 1
runs in **your Superset checkout**, not in this repo.

**1. Build the extension** in this repo:

```sh
.venv/bin/superset-extensions bundle
```

This creates `nltosqlandback-0.1.0.supx`.

**2. Get Superset running** (skip this if you already have it). In a folder next to this repo:

```sh
git clone https://github.com/apache/superset.git
cd superset
git checkout 6.1.0
docker compose up
```

Check that it starts and you can log in, then stop it with `docker compose down`.

**3. Install the extension.** Copy `nltosqlandback-0.1.0.supx` into the Superset checkout's
`docker/extensions-installed/` folder.

**4. Add the extension's config.** Copy [deploy/superset_config_docker.py](deploy/superset_config_docker.py)
from this repo to `docker/pythonpath_dev/superset_config_docker.py` in the Superset checkout.
Superset's own `docker/pythonpath_dev/superset_config.py` loads it automatically.

**5. Add your API key**, in one of these places in the Superset checkout:

- **Recommended:** add `OPENROUTER_API_KEY=sk-or-v1-...` to `docker/.env-local`. Superset's compose
  file reads it if it exists, and the key stays out of the config file.
- Or put the key straight into `superset_config_docker.py`.

Every other setting in that file is optional (see [Configuration](#configuration)): the file
names every setting, with the optional ones commented out, so it's the one file to copy and edit.

**6. Start Superset again**, still in the Superset checkout:

```sh
docker compose up
```

To update the extension later, repeat steps 1, 3 and 6. Replacing the `.supx` isn't enough on its
own: Superset only loads the extension's backend when it starts.

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

## Troubleshooting

**`401 Client Error: Unauthorized for url: https://openrouter.ai/api/v1/...`** in the logs.
Superset didn't get a valid API key.

- Option 1: check `.env` exists in this repo's root and contains `OPENROUTER_API_KEY=...`, then run
  `docker compose up -d --force-recreate superset`. A plain restart keeps the old (empty) value.
- Option 2: check the key is in your Superset checkout's `docker/.env-local` or
  `superset_config_docker.py`. Putting it in this repo does nothing for Option 2, and vice versa.
- Still failing? The key may be revoked or out of credit. Check it at https://openrouter.ai/keys.

**`zsh: command not found: superset-extensions`.** The CLI is in the repo's virtualenv. Run
`.venv/bin/superset-extensions`, or `source .venv/bin/activate` first.

**`port is already allocated` / port 8088 in use.** Another Superset is running, probably the
other option. Stop it with `docker compose down` in its folder.

**The panel doesn't appear in SQL Lab.** Work down from the server to the browser:

1. **Did Superset load the extension?** Its log says `Loaded extension 'waxwing.nltosqlandback'`
   at start-up, and `/api/v1/extensions/` in the browser lists it. If not: `ENABLE_EXTENSIONS` is
   off (the config wasn't read), the `.supx` isn't in `EXTENSIONS_PATH`, or Superset wasn't
   restarted after it was copied.
2. **Did the backend read our settings?** The log says `AI Assistant backend loaded:` followed by
   the model, embeddings, structure read, dictionary and table selection in effect (never the
   key). Defaults where you set something mean the config file wasn't read: with Superset's docker
   compose, a failing import of `superset_config_docker.py` falls back to the defaults silently.
3. **Did the panel's code load, and which build?** The browser console on SQL Lab says
   `[AI Assistant] loaded, build <commit>, built <time>`. No line means the browser never got the
   bundle (try a hard refresh); a different commit means an old `.supx`.
4. **Is the panel hidden from you?** The next console line says `panel shown: ...` or
   `panel hidden: ...`, naming your roles. The panel needs a role with a permission on
   `nltosqlandback`. Give one, or build with `NLSQL_IGNORE_ROLES=true` to show it to everyone.
   `window.nltosqlandback` in the console holds the build and the answer.
5. **Is SQL Lab's right sidebar collapsed?** The panel lives there.

**My code changes don't show up.** Check the `superset-extensions dev` watcher is still running,
wait a minute or two, then refresh the browser.

**The panel shows nothing until the answer is complete.** See [Streaming](#streaming): a reverse
proxy may be buffering the response.

## Configuration

All settings are optional. Set them in your Superset config: `dev/superset_config.py` for Option 1, or
`docker/pythonpath_dev/superset_config_docker.py` in your Superset checkout for Option 2.

> Option 1's `dev/superset_config.py` already points the chat and embedding models at OpenRouter
> (`google/gemma-4-31b-it` and `qwen/qwen3-embedding-8b`) and turns on the semantic schema. The
> defaults below apply when a setting isn't set at all.

| Setting | Default | What it does |
|---|---|---|
| `NLSQL_LLM_BASE_URL` / `NLSQL_LLM_MODEL` / `NLSQL_LLM_API_KEY` | local Ollama, `gemma3:4b` | The OpenAI-compatible chat model. |
| `NLSQL_EMBEDDING_BASE_URL` / `NLSQL_EMBEDDING_MODEL` / `NLSQL_EMBEDDING_API_KEY` | local Ollama, `nomic-embed-text` | The OpenAI-compatible embedding model - used with `NLSQL_SEMANTIC_SCHEMA_ENABLED`, and with `NLSQL_SCHEMA_EMBEDDINGS`. |
| `NLSQL_EMBEDDING_DOCUMENT_PREFIX` / `NLSQL_EMBEDDING_QUERY_PREFIX` | `search_document: ` / `search_query: ` | Instruction prefixes put before table text and before the request when embedding them (nomic-embed-text's own; most other models want them blank, or their own). |
| `NLSQL_LLM_TIMEOUT_SECONDS` | `600` | How long to wait for a completion. |
| `NLSQL_LLM_REQUEST_USAGE` | `True` | Ask for token usage on streamed calls (`stream_options.include_usage`) - what puts token counts in the log. Turn off for a server that rejects it. |
| `NLSQL_LLM_USER_HEADER` | off | Send the Superset username with every LLM and embedding call, as this HTTP header (e.g. `"X-Superset-User-Name"`) - for a gateway that tracks usage or sets limits per user. The value is percent-encoded UTF-8 (`José` → `Jos%C3%A9`; plain ASCII is sent as it is), so decode it with `unquote`. Not sent for a call that isn't for a user. A username is personal data: think twice before sending it to a hosted provider. |
| `NLSQL_LLM_CLIENT_CERT` / `NLSQL_LLM_CLIENT_KEY` | none | A client certificate for an HTTPS endpoint that wants one (mutual TLS): paths to PEM files the Superset process can read. The certificate file may hold its key, in which case leave `_CLIENT_KEY` unset. Read again whenever a file changes, so a certificate rotated in place needs no restart. |
| `NLSQL_LLM_CLIENT_KEY_PASSWORD` | none | The key's password, if it's encrypted - in the config or as an environment variable of that name. |
| `NLSQL_LLM_CA_BUNDLE` | the system's | The CAs (a PEM file) to trust for the endpoint's own certificate, in place of the system's - for an endpoint with a certificate from an in-house CA. |
| `NLSQL_EMBEDDING_CLIENT_CERT` / `_CLIENT_KEY` / `_CLIENT_KEY_PASSWORD` / `_CA_BUNDLE` | the `NLSQL_LLM_*` ones | The same for the embedding endpoint. |
| `NLSQL_SEMANTIC_SCHEMA_ENABLED` | `False` | Give prompts a schema section from the bundled semantic schema (embedding search). Needs the `NLSQL_EMBEDDING_*` settings. Off, prompts use the tab's database as interrogated by **Interrogate DB**, if it has been. |
| `NLSQL_DB_INFO` | `True` | Run with database information: read each database's structure and data dictionary, and put them in prompts. Off, neither is read, Interrogate DB is refused, prompts get no database section (nor the bundled semantic schema), and `db-knowledge` answers `{"status": "off"}`, so the panel hides Ask and the knowledge bar. Accelerate and Fix still read the query's plan. |
| `NLSQL_INTERROGATE_MAX_TABLES` | `3000` | The most tables (and views), across all schemas, one Interrogate DB reads. |
| `NLSQL_INTERROGATE_ON_FIRST_USE` | `True` | Start reading a database in the background when someone first opens the panel on it and nothing is known about it - every schema its connection can see. Off, someone has to press "Read the database". |
| `NLSQL_INTERROGATE_CHECK_SECONDS` | `3600` | How old the last check of a database gets before opening the panel starts another: list each schema's tables, read the added ones, drop the gone ones. `0` never checks. |
| `NLSQL_INTERROGATE_LOCK_SECONDS` | `1800` | How long the lock that keeps reads of a database one at a time lasts. It isn't renewed, so it must outlast the longest read; a process that dies holding it blocks reads of that database until it runs out. |
| `NLSQL_SYSTEM_USER` | off | The Superset user that reads every database on a schedule, so what's known is ready before anyone opens the panel. While it's set, opening the panel starts no reads. It reads structure only, as that user, and prompts still show each user only their own tables. See `system_reads.py`. |
| `NLSQL_SYSTEM_READ_SCHEDULE` | `3600` | Seconds between those rounds, after one a minute after start-up; `0` for start-up only. Each round fills databases nothing is known about and checks the rest; one process runs it. |
| `NLSQL_STRUCTURE_READ` | `"per_table"` | How Interrogate DB reads the structure: `"per_table"` (about five inspector calls per table), `"per_schema"` (about six catalog queries per schema) or `"per_database"` (about six catalog queries for all the schemas). Catalog queries exist for Oracle and Trino; other engines are read per table. A dict sets it per Superset database name, with `"default"` for the rest. |
| `NLSQL_INTERROGATE_ROW_ESTIMATES` | `True` | Have the planner estimate each table's row count while interrogating (one `EXPLAIN` per 60 tables, never run; each shows in the user's query history). |
| `NLSQL_INTERROGATE_DESCRIPTIONS` | `False` | Also have the model describe each table and its less obvious columns (one call per ~25 tables) and read table comments. Off, the structure alone is kept - far fewer calls and a much smaller prompt. |
| `NLSQL_INTERROGATE_EXCLUDE_SCHEMAS` | the database's own (`information_schema`, `pg_catalog`, Oracle's `SYS`, `SYSTEM`…) | Schemas never interrogated - a list that replaces the default one. |
| `NLSQL_INTERROGATE_SCHEMAS` | all | Only these schemas, if set. |
| `NLSQL_DATA_DICTIONARY` | none | Where the site describes its schemas, tables and columns: a dict with an optional `"schemas"`, `"tables"` and `"columns"` level, each `{"table": ..., "columns": {...}}` naming its table and that table's `schema`, `table`, `column` and `description` columns as the level needs them, plus `"database"` (the Superset database it's in; default the one being read) and `"max_rows"`. Interrogate DB reads each table with one fixed `SELECT`, its descriptions go into prompts, and the dictionary's tables are left out of what's kept. The result says how much it describes and how many entries matched nothing. See `data_dictionary.py`. |
| `NLSQL_DATA_DICTIONARY_TABLE` | none | The older single-table form, used when `NLSQL_DATA_DICTIONARY` isn't set: one table in the database describing all three levels (e.g. `"metadata.data_dictionary"`). |
| `NLSQL_DATA_DICTIONARY_COLUMNS` | `schema_name`, `table_name`, `column_name`, `description` | That table's columns, as a dict with keys `schema`, `table`, `column` and `description`. A row with no column describes its table; one with no table either describes its schema. |
| `NLSQL_DATA_DICTIONARY_MAX_ROWS` | `200000` | The most rows of it read. |
| `NLSQL_DERIVED_COLUMN_SUFFIXES` | `_CLEANSED`, `_TRANSLITERATED`, `_TRANSLATED` | Column name suffixes that mark a converted copy of another column, as a dict of suffix to `"cleansed"`, `"transliterated"` or `"translated"`. Prompts say which is which and how to choose. |
| `NLSQL_HIDE_CLEANSED_ORIGINALS` | `True` | Leave a cleansed column's original out of prompts (unless it's a key), so the model can't pick the wrong one. |
| `NLSQL_SCHEMA_MAX_CHARS` | `120000` | How much interrogated structure a prompt gets. Past this, with `NLSQL_SCHEMA_EMBEDDINGS` on, the tables closest to the request come first, described, until it's full. Without, column types and indexes go, then the tables sharing fewest words with the request are left out. |
| `NLSQL_SCHEMA_EMBEDDINGS` | `False` | Have Interrogate DB embed every table (its names and descriptions, with the `NLSQL_EMBEDDING_*` model, one call per 32 tables), so a database too big for one prompt gets the tables closest to each request, and the tables they join to. The vectors are kept once for the whole Superset, by a hash of the model, prefix and table text, so only tables not embedded before, in any database, are embedded. Interrogate again after turning it on or changing the embedding model. |
| `NLSQL_TABLE_SELECTION` | `"embedding"` | How the tables for the SQL prompt are picked when they don't all fit: `"embedding"` (the closest, until the prompt is full), `"embedding-reasoning"` (the same, the SQL call with reasoning), `"embedding-then-model"` (the closest `NLSQL_TABLE_SELECTION_SHORTLIST` go to a model call that picks the ones the request needs, and how they join), `"embedding-then-model-reasoning"`, or `"embedding-then-model-tools"` (the picking call may first look up more tables, `NLSQL_TABLE_SELECTION_LOOKS` times). Without embeddings "closest" means sharing the most words. A picking call that fails falls back to the first. See `table_selection.py`. |
| `NLSQL_TABLE_SELECTION_ALLOWED` | the default only | The options a user may choose in the panel, for comparing them on site. |
| `NLSQL_TABLE_SELECTION_ROLES` | `["Admin"]` | The roles that may choose; everyone else gets the default. |
| `NLSQL_TABLE_SELECTION_SHORTLIST` / `_MAX` / `_LOOKS` / `_MAX_CHARS` | `40` / `12` / `3` / `60000` | How many tables the picking call is offered, the most it may pick, how many times it may look further, and how much of its prompt they take before their columns are left out. |
| `NLSQL_TABLE_SELECTION_TOP_N` | none | The most of the closest tables the SQL prompt lists, even when more would fit. |
| `NLSQL_LLM_REASONING_ON` / `NLSQL_LLM_REASONING_OFF` | none | What to add to the SQL call's request body to turn the model's reasoning on or off, for the options with and without it - it depends on the model service (OpenRouter: `{"reasoning": {"enabled": True}}`). Nothing is sent unless set. |
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
create a .env file with your open router key specified (see `.env.example` for the other settings)
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

Superset never calls OpenRouter itself: it talks to the LLM stand-in (`e2e/stand-in/`), which by
default passes every request through to OpenRouter with your key. The acceptance tests switch the
same stand-in to replaying recordings.


subsequent changes should reload through automatically.
they seem to take a minute or 2 and require you to manually refresh the browser

To see every LLM call as a trace in Arize Phoenix as well, add `NLSQL_PHOENIX_ENDPOINT=http://phoenix:6006`
to `.env` and start with `docker compose --profile phoenix up`; Phoenix is then at http://localhost:6006.

Backend tests:

    cd backend && ../.venv/bin/python -m pytest

Acceptance tests (Playwright, against this repo's docker compose, with the LLM stand-in replaying
recordings) live in [e2e/](e2e/); see [e2e/README.md](e2e/README.md). They test the plugin as
`superset-extensions dev` has built it:

    cd e2e && npm install && npx playwright install chromium
    npm run harness:replay && npm test

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
