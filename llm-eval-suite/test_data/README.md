# Test data

The LLM-quality suite runs against tables in Superset's examples database,
one schema per source:

| Schema | Source | How it gets there |
|---|---|---|
| `acled` | ACLED conflict data, rebuilt from the original downloads | `./test_data/load_acled.sh` |
| `opensanctions` | OpenSanctions targets, the cleaned and normalised Foundry dataset | Exported from Foundry, then loaded (below) |
| `airports` | Our airports and flights data, the cleaned and normalised Foundry datasets | Exported from Foundry, then loaded (below) |
| `geo`, `ourairports`, `openflights`, `opensanctions_public`, `ucdp`, `wdi` | Open datasets that link to ACLED: country codes, places, airports and routes, sanctions targets, UCDP conflict events, World Bank indicators | `./test_data/download_linked.sh`, then `./test_data/load_linked.sh` ([below](#linked-open-datasets)) |

OpenSanctions and the airports data won't be queried through the plugin in
production. They're here to test the plugin on many tables, large volumes,
and joins across sources whose keys don't match.

Keep all of this data out of the repo: `.gitignore` excludes data files in
this folder, and the airports data is internal.

## How the sources join

ACLED names countries in words ("Kenya", "Ivory Coast"). The other sources
use ISO codes, so `acled.countries` carries both, and is the bridge:

| From | Join on | Watch out for |
|---|---|---|
| `acled.countries` | `iso2` (e.g. `KE`), `iso3` (e.g. `KEN`) | Upper case. Seas and the UK's bases on Cyprus have no code. Kosovo is `XK` / `XKX`, the code most datasets use, though it isn't official ISO |
| OpenSanctions | its country codes | Check the export: OpenSanctions writes ISO-2 codes in lower case, and an entity can have several in one column, separated by `;`, so `= 'ru'` misses an entity listed as `ru;by` |
| Airports | the airport's country code | Check whether it's ISO-2 or ISO-3 |
| Airports and `acled.global_events` | latitude and longitude | ACLED gives each region's centre point (`centroid_latitude`, `centroid_longitude`), not where events happened |
| Flights and `acled.global_events` | departure time and `week` | ACLED weeks start on a Saturday; Postgres's `date_trunc('week', …)` starts on a Monday |

Table names must be unique across schemas, because the suite's metrics match
tables by name without the schema. Don't name a new table `countries`, for
example.

## 1. Export the datasets from Foundry

Use the cleaned and normalised outputs of each pipeline, not the raw ingest
datasets. For each dataset, note its path, its RID (from the dataset page)
and the branch (normally `master`).

Export each one once, and keep the files: the reference SQL in the goldens
gives fixed answers, so everyone must load the same snapshot. Put the
exports on the team drive, and record the date and RID in the table at the
bottom of this file.

**Small datasets** (OpenSanctions targets, airports): download them from the
dataset's page in Foundry as CSV, if your permissions allow it.

**Large datasets** (flights): a browser download may be too big or slow.
Read the dataset through Foundry's API instead, with a personal token
(Foundry account settings), and save it as Parquet. Using the Foundry
Platform SDK (`pip install foundry-platform-sdk`), the outline is:

```python
import os
import foundry_sdk

client = foundry_sdk.FoundryClient(
    auth=foundry_sdk.UserTokenAuth(os.environ["FOUNDRY_TOKEN"]),
    hostname="YOUR_STACK.palantirfoundry.com",
)
table = client.datasets.Dataset.read_table("ri.foundry.main.dataset.…", format="ARROW")
table.to_pandas().to_parquet("flights.parquet")
```

The SDK's names have changed between versions, so check the call against
the API documentation in your stack's Developer Console. Keep the token in
your environment, never in a file in this repo.

Save the files outside the repo, for example `~/foundry-exports/opensanctions/`
and `~/foundry-exports/airports/`.

## 2. Load them into the database

Each source needs a loader like `load_acled.py`, run the same way: in a
throwaway container on Superset's Docker network, so nothing is installed
on your machine. A loader should:

1. Create the source's schema (`opensanctions`, `airports`) in the examples
   database (`postgresql://examples:examples@db:5432/examples` on Superset's
   network) and replace its tables on every run.
2. Lowercase the column names, so SQL doesn't need quoted names.
3. Give columns real types: dates as `date`, flight times as `timestamptz`,
   latitude and longitude as `double precision`, counts as integers.
4. Leave out Foundry's audit columns (`_ingested_at`, `_ingestion_run_id`,
   `_record_hash`), as `load_acled.py` does.
5. Bulk-load with `COPY`; row-by-row inserts are far too slow for flights.
6. Add primary keys, foreign keys where the data supports them, and indexes
   on the columns joins and filters use: country codes, airport codes,
   departure times. Then run `ANALYZE`, so the planner has statistics.

Re-run `./test_data/load_acled.sh` too, so `acled.countries` has the ISO codes.

## 3. Describe the new tables

The suite's metrics and the judge rely on `evals/schema_metadata.json`,
and the plugin relies on its own schema descriptions. Both need the new tables.

1. Generate the suite's notes once per schema. Each run adds or updates that
   schema's tables and keeps the rest of the file, including descriptions
   already written:

   ```bash
   docker run --rm --env-file .env.eval -e SUPERSET_SCHEMA=opensanctions \
     -v "$PWD/evals:/suite/evals" --entrypoint python \
     llm-eval-suite:dev evals/generate_schema_metadata.py
   ```

   Then repeat with `SUPERSET_SCHEMA=airports`.
2. Write a `description` for each new table. Say what a row is, and how the
   table joins to the others, including the traps in the table above.
3. Check the plugin's table search can see the new schemas, and that its
   schema descriptions cover them.

## 4. Write goldens

New goldens go in `evals/goldens/`, in the file named after their endpoint,
like the others (see the main README). For these sources:

- Qualify every table with its schema in the reference SQL (`airports.flights`).
- State distances, time windows and tie-breaks in the question ("within
  100 km of the region's centre, in the same ACLED week"), so there's one
  right answer.
- Use `"execute": false` for any reference query too slow to run on every
  evaluation.

## Exports in use

| Source | Foundry dataset (path, RID) | Exported | Rows |
|---|---|---|---|
| ACLED | Rebuilt from downloads, see `load_acled.py` | – | – |
| OpenSanctions targets | | | |
| Airports | | | |
| Flights | | | |

## Linked open datasets

Public datasets that join to ACLED and to each other, for questions that
need multi-hop joins through code crosswalks, distance and date-window
joins, fuzzy name matching, unnesting `;`-separated columns, recursive
queries over a route graph, and window functions over country-year panels.

```bash
./test_data/download_linked.sh   # ~75 MB into test_data/raw/ (git-ignored)
./test_data/load_linked.sh       # about a minute, with indexes
docker exec -i superset-db-1 psql -U examples -d examples < test_data/validate_linked.sql
```

Two files aren't downloaded by the script: put them in `~/Downloads` (or
set `LINKED_DOWNLOADS`).

- `WDI_CSV.zip` (~280 MB): https://databankfiles.worldbank.org/public/ddpext_download/WDI_CSV.zip
- `targets.simple.csv` (~470 MB): https://data.opensanctions.org/datasets/latest/default/targets.simple.csv

Re-running either script is safe: downloads skip files already in `raw/`,
and the loader drops and rebuilds each dataset's schema. Load some datasets
only with `LINKED_ONLY=geo,ucdp ./test_data/load_linked.sh`. The loader
also creates `geo.haversine_km()`, an ordinary SQL function. It uses plain
PostgreSQL only, with no extensions (no `pg_trgm`, no PostGIS): the plugin
can't assume a database has any.

| Schema | Tables | Source | Rows | Licence |
|---|---|---|---|---|
| `geo` | `country_info`, `gn_cities`, `gn_admin1`, `gn_admin2` | [GeoNames](https://download.geonames.org/export/dump/) | 252 countries, 237k places | CC BY 4.0 |
| `ourairports` | `oa_airports`, `oa_runways`, `oa_navaids`, `oa_airport_frequencies`, `oa_countries`, `oa_regions` | [OurAirports](https://davidmegginson.github.io/ourairports-data/) | 86k airports | Public domain |
| `openflights` | `of_routes`, `of_airlines` | [OpenFlights](https://openflights.org/data) (2014 snapshot) | 68k routes | ODbL |
| `opensanctions_public` | `os_targets`, `os_target_countries`, `os_target_aliases` | [OpenSanctions](https://www.opensanctions.org/datasets/default/) targets | 1.2M targets | **CC BY-NC 4.0: non-commercial use only** without a commercial licence |
| `ucdp` | `ucdp_ged` | [UCDP GED](https://ucdp.uu.se/downloads/) 26.1 | 418k events, 1989–2025 | CC BY 4.0 |
| `wdi` | `wdi_wide`, `wdi`, `wdi_indicators` | [World Bank WDI](https://datacatalog.worldbank.org/search/dataset/0037712) | 398k country-indicator rows | CC BY 4.0 |

`opensanctions_public` holds the public download, separate from the
`opensanctions` schema planned for the Foundry export. The `airports` schema
works the same way, and table names are prefixed (`oa_`, `of_`, `os_`) so
they stay unique across schemas.

### How they link

ACLED here is the aggregated download, so links go through
`acled.countries`, admin1 names, region centres and weeks, not through
event-level columns (actors, exact coordinates, event dates).

| Link | Join on | Matches |
|---|---|---|
| ACLED → `geo.country_info` | `acled.countries.iso2 = country_info.iso` | 244 of 251 countries (the rest are seas and the UK's bases on Cyprus) |
| `country_info` → UCDP | `gw_code = ucdp_ged.country_id` (Gleditsch-Ward), a foreign key on `ucdp_ged` | all UCDP events; only the 126 countries with a UCDP event have a `gw_code` |
| ACLED → UCDP events | same country, `date_start BETWEEN week AND week + 6`, within 50 km of the region's centre | 21% of fatal ACLED weeks in 2024–25 |
| Airports → countries | `oa_airports.iso_country = country_info.iso` | 100% |
| ACLED → airports | region centre within 50 km of a large or medium airport | 55% of regions |
| Routes → airports | `of_routes.source_airport_ref` / `dest_airport_ref = oa_airports.id` (resolved from IATA, then ICAO, then ident) | 99% |
| OpenSanctions → countries | `upper(os_target_countries.country_code) = country_info.iso` | 99% |
| ACLED admin1 → `gn_admin1` | same country, then name: exact / case-insensitive | 54% / 54% |
| ACLED region centre → nearest place | `gn_cities` within 25 km | 82% |
| UCDP actors → OpenSanctions | `side_a` / `side_b` the same as `os_targets.name` / `os_target_aliases.alias`, ignoring case | 3% / 9% of actor names (48 / 145 of 1,677) |
| ACLED → WDI | `acled.countries.iso3 = wdi.iso3 AND wdi.year = year` | 94% of country-years 2000–2024 have population |

There's no PostGIS in the examples database. Distance joins filter a
latitude/longitude box first (so the indexes on the coordinates help), then
use `geo.haversine_km(lat1, lon1, lat2, lon2)`.

### Optimised copies

The loader also builds faster copies of what the raw tables give only
through a big scan, a distance calculation or unnesting. Goldens test
whether the plugin reads them when they give the same answer (`must_use` or
`must_not_use` on the raw table), and keeps away from them when they don't.

| Copy | Of | What it saves | When it's the wrong answer |
|---|---|---|---|
| `ucdp.ucdp_country_year` (materialized view) | `ucdp_ged` | Totals per country, year and type of violence: ~4,000 rows instead of 418,000 | Anything below a year, or by place or actor |
| `openflights.of_route_legs` | `of_routes` + `oa_airports` | One row per airport pair, with both countries and `distance_km` | Per-airline questions |
| `opensanctions_public.os_country_counts` | `os_target_countries` + `os_targets` | Targets per country and schema | Distinct targets across several countries: a target is counted once per country, so sums over-count |
| `geo.acled_region_links` | ACLED regions + GeoNames + `oa_airports` | Each region's matching `gn_admin1` area, nearest place and nearest large or medium airport | The nearest airport *in the same country*: it's the nearest anywhere, across borders |
| `wdi.wdi` | `wdi_wide` | The `WDI_INDICATORS` keyed on (iso3, indicator_code, year) | Indicators not in `WDI_INDICATORS` |

Each copy is rebuilt whenever one of its sources is loaded. Their entries
in `schema_metadata.json` carry `rollup_of`, so the judge is told to prefer
them, and the static check warns when the raw table is read instead. The
plugin sees them as tables (and the materialized view as a view) once the
database is interrogated again.

`wdi.wdi` holds only the indicators in `WDI_INDICATORS` (population, GDP,
GDP per capita, military expenditure, refugees, urban population); set it,
comma-separated, to load others. `wdi_wide` has all 1,498 indicators, one
column per year.
