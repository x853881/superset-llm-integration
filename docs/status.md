# Status (2026-10-18)

Work on `wip/frontend-updates`, not yet committed.

## Verified

- Backend: 170 tests (`cd backend && ../.venv/bin/python -m pytest`). Plan readers are tested against
  real Postgres 17, Trino and Oracle 23 `EXPLAIN` output (`backend/tests/fixtures/`).
- The OTLP encoder against the official `opentelemetry-proto` decoder; spans received over HTTP and
  decoded (one trace per request: `CHAIN` → `LLM` / `TOOL`, `user.id` and `session.id` on each).
- Inside a running Superset 6.1: `DatabaseDAO` lookup, Superset's dialect mapping, its Postgres SQL
  validator, and `EXPLAIN` through `Database.execute` on Postgres, Trino and Oracle.
- Accelerate and Break it down end-to-end against OpenRouter (`google/gemma-4-31b-it`): plan fetched,
  rewrite checked and re-planned (estimated cost 671 → 132), log lines with user id, tokens and cost.
- In a browser (throwaway Superset 6.1, SQLite database, fake LLM): opens on Refine; Accelerate card
  with diff and Apply (an undo step); a refinement started in tab 1 finished while tab 2 was on
  screen and landed in tab 1 only; Break it down hover highlighting and click-to-select in the
  editor; Explain the query plan; every call in the JSONL log with the session's user id.
- In the docker Superset 6.1 (`~/git/superset`), with OpenRouter (`google/gemma-4-31b-it`):
  - Interrogate DB, database-wide and structure only (the default), on all three databases:
    Trino (`trino_iceberg_local`: 3 schemas, 24 tables, all 24 sized from one plan, 3.5 s), Oracle
    (`oracle_local`: 27 system schemas left out, 4 with tables, 27 tables, all sized from one plan -
    three of them by union position, Oracle having planned them from their primary-key index, 6.5 s)
    and Postgres (`examples`: 3 schemas, 24 tables, 22 sized; the other two refused with
    "permission denied for schema movie_data", isolated by splitting and reported as problems, 1.8 s).
  - Ask on a Trino tab whose selected schema is `crime_data`, about movies: "All 24 tables in 3
    schemas of trino_iceberg_local", SQL against `movie_data.movies`/`movie_data.ratings`, written by
    the model on one line and placed in the editor laid out over 13.
  - Break it down (bottom tab) on a two-CTE query: Query intent, numbered tinted blocks, cards
    "rated (CTE)" / "popular (CTE)" / "Final query" with line chips, the detailed-explanation switch
    and per-card chevrons.
  - Earlier (per-schema version): descriptions, synonyms and an inferred join kept in Superset's
    key-value store, surviving a restart - the describe path is unchanged, but now off by default
    and not re-run live since.
  - Break it down (bottom tab): the query laid out for Oracle, coloured, sections barred and linked
    on hover, the right-hand column scrolling within the pane; the schema description used.
  - Break it down on a Postgres CTE query over `crime_data.crimes` (8.6M rows): sections streamed in
    one at a time; the prompt held only that table (3,311 characters, from 23,951 for the whole
    database); the CTE's card listed `crime_data.crimes ≈8.6M` and hinted at the unindexed `district`
    filter (not at `occurred_at`, which is indexed). "Use this layout" put the layout in the editor as
    an undo step, after which moving the editor's cursor lit the section it was in. Editing two lines
    showed "Out of date · 2 lines changed" with those lines marked; "Break it down again" showed
    "Updating", the old breakdown hatched and dimmed until the new sections arrived; putting the SQL
    back to an earlier version brought its breakdown back with no call. The side panel's Describe tab
    showed the same chip, and a second "Explain the query plan" kept the first, dimmed, until it landed.
  - Traces: `describe_breakdown` → `llm · describe_breakdown` (with the prompt and reply), and a
    `validate_sql` → `db · validate` trace for each validation (Postgres has a validator configured);
    a validator error still reached the Fix tab through `/validate-sql`.
  - Query Estimator: an Oracle plan as a tree with rows, cost, time and a large-scan warning; an
    invalid query's error shown as such.
  - The side panel following SQL Lab tab switches, including the first visit to a tab after a page
    load (which Superset reports no event for - see `useActiveSqlLabTab`).

## Not verified / known gaps

- Interrogate DB on a big database (thousands of tables: how long the per-table catalog reads take,
  and how a prompt of that size does with the model), and with foreign keys declared (the examples
  databases have none). Postgres enum values are read from the column type but not seen live. With
  `NLSQL_INTERROGATE_DESCRIPTIONS` on, only the unit tests have run the describe step since it went
  database-wide.
- The prompt is the whole structure; at the default `NLSQL_SCHEMA_MAX_CHARS` (120k characters, ~30k
  tokens) that's a few hundred tables with types. Beyond that it falls back to names only, then to
  a word-overlap ranking - embeddings and similarity search are for a later version.
- Generated SQL formatting: Superset's own formatter (sqlglot) with an AST round-trip guard. Seen
  live on Trino; Oracle and Postgres only in unit tests.
- The Query Estimator's cost bands are Postgres-shaped; Oracle and Trino costs are in other units, so
  their "impact" is only roughly comparable.
- The model can still write SQL the database rejects - e.g. it grouped by an Oracle CLOB in testing;
  large-object columns now carry a note saying not to, untested live since.
- Not run against a live Arize Phoenix (only a stub collector) - start one with
  `docker compose --profile phoenix up` and check the traces look right in its UI.
- Trino/Oracle accelerate with a real model, and the MySQL/SQLite plan readers against real
  databases (SQLite only through the fake LLM).
- Superset's `onDidChangeContent` doesn't fire for programmatic `setValue` (found while testing), so
  the panel re-reads the editor after writing to it (`useTabEditor().resync`).
- SQL Lab tab ids change from client ids to server ids across a page reload, so per-tab panel state
  (and Phoenix `session.id`) doesn't survive a reload - as before.
- `NLSQL_DELEGATE_TO_SERVICE` (sql-helper's proxy to sql-helper-service) was deliberately not ported:
  it would move the LLM calls - and their logging - out of this process. (Removed from the docker
  config, where it was still set but read by nothing.)
- Break it down's table chips and hints need the database interrogated; hints are only as good as
  the planner's row estimates and the indexes the catalog reports (no partial or expression-index
  awareness, and only an index's leading column counts). Changed-line marks can't be drawn on a
  laid-out copy of the SQL, only counted.
- With a validator configured, the editor's validation is now a trace per pause in typing - a lot of
  small `validate_sql` traces next to the model calls.
- Writing SQL into the editor twice in quick succession could leave it empty (SQL Lab's store kept
  the blank text between the second write's remove and insert, and its editor component put that
  back); the panel now writes only when the editor doesn't already hold the SQL. Why the write ran
  twice in Superset's dev build wasn't pinned down.

## To deploy

`.venv/bin/superset-extensions bundle`, copy `nltosqlandback-0.1.0.supx` into the deployment's
extensions folder, restart Superset (the backend is loaded at startup). Optional settings are in the
README's Configuration table.
