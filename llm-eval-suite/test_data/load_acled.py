"""Load the ACLED test tables into Superset's examples database, in their own
`acled` schema.

Rebuilds what the Foundry pipeline (raw ingest + normalise transforms) makes,
from the original ACLED downloads, so the test database doesn't depend on
exporting from Foundry:

    global_events                       the six regional weekly files, combined
    demonstration_events_country        country-year
    events_targeting_civilians_country  country-year
    political_violence_country_month   country-month-year (month names)
    political_violence_year             country-year
    reported_civilian_fatalities        country-year
    reported_fatalities                 country-year
    countries                           every country, with its ACLED region
                                        and ISO 3166 codes (iso2, iso3)

ACLED names countries in words. The ISO codes in `countries` are what other
sources (OpenSanctions, airports) use, so it is the bridge for joining them.
They are upper case, as ISO writes them; a source that lowercases its codes
needs UPPER() to join.

Same cleaning as the Foundry transforms (trimmed text, numeric columns cast,
values that aren't numbers become NULL), except:
- column names are lowercased, so SQL doesn't need "QUOTED" names;
- week is a date, where Foundry leaves it as text;
- Foundry's audit columns (_ingested_at, _ingestion_run_id, _record_hash)
  are left out.

Then adds primary keys, foreign keys to `countries`, NOT NULL on the
global_events columns every row has, indexes on global_events, and runs
ANALYZE. A key the data doesn't satisfy is reported
and skipped rather than stopping the load.

Run with test_data/load_acled.sh, which runs this in a throwaway container on
Superset's Docker network.
"""

import glob
import io
import os
import sys

import pandas as pd
import pycountry
import sqlalchemy as sa

DATA_DIR = os.getenv("ACLED_DIR", "/data")
DATABASE_URL = os.getenv(
    # psycopg2 named explicitly: newer SQLAlchemy defaults to psycopg 3, and
    # the bulk load below uses psycopg2's copy_expert.
    "DATABASE_URL",
    "postgresql+psycopg2://examples:examples@db:5432/examples",
)
SCHEMA = os.getenv("ACLED_SCHEMA", "acled")

# Table -> filename patterns in DATA_DIR. All are Excel files, including the
# fatalities download that ACLED names .csv.
SOURCES = {
    "global_events": ["*_aggregated_data_up_to_week_of-*.xlsx"],
    "demonstration_events_country": [
        "number_of_demonstration_events_by_country-year_*"
    ],
    "events_targeting_civilians_country": [
        "number_of_events_targeting_civilians_by_country-year_*"
    ],
    "political_violence_country_month": [
        "number_of_political_violence_events_by_country-month-year_*"
    ],
    "political_violence_year": [
        "number_of_political_violence_events_by_country-year_*"
    ],
    "reported_civilian_fatalities": [
        "number_of_reported_civilian_fatalities_by_country-year_*"
    ],
    "reported_fatalities": ["number_of_reported_fatalities_by_country-year_*"],
}

INT_COLS = {"events", "year", "fatalities", "population_exposure", "id"}
FLOAT_COLS = {"centroid_latitude", "centroid_longitude"}
DATE_COLS = {"week"}

COLUMN_TYPES = {
    "week": sa.Date(),
    "events": sa.Integer(),
    "year": sa.Integer(),
    "fatalities": sa.Integer(),
    "population_exposure": sa.BigInteger(),
    "id": sa.Integer(),
    "centroid_latitude": sa.Float(),
    "centroid_longitude": sa.Float(),
}

# ISO 3166 alpha-2 codes for ACLED names that pycountry doesn't recognise, or
# would match to the wrong country by fuzzy search (Curacao to the
# Netherlands, Kosovo to Serbia). None means there is no ISO code: ACLED
# records events at sea, and on the UK's sovereign bases in Cyprus. Kosovo has
# no official code; XK / XKX is what the EU and most datasets use.
ISO_OVERRIDES = {
    "Akrotiri and Dhekelia": None,
    "Arctic Ocean": None,
    "Atlantic Ocean": None,
    "Bailiwick of Guernsey": "GG",
    "Bailiwick of Jersey": "JE",
    "Brunei": "BN",
    "Cape Verde": "CV",
    "Caribbean Netherlands": "BQ",
    "Curacao": "CW",
    "Democratic Republic of Congo": "CD",
    "East Timor": "TL",
    "Falkland Islands": "FK",
    "French Southern and Antarctic Lands": "TF",
    "Indian Ocean": None,
    "Ivory Coast": "CI",
    "Kosovo": "XK",
    "Mediterranean Sea": None,
    "Micronesia": "FM",
    "Pacific Ocean": None,
    "Palestine": "PS",
    "Republic of Congo": "CG",
    "Reunion": "RE",
    "Russia": "RU",
    "Saint-Barthelemy": "BL",
    "Saint-Martin": "MF",
    "Sint Maarten": "SX",
    "Southern Ocean": None,
    "Turkey": "TR",
    "Vatican City": "VA",
}

COUNTRY_YEAR_TABLES = [
    "demonstration_events_country",
    "events_targeting_civilians_country",
    "political_violence_year",
    "reported_civilian_fatalities",
    "reported_fatalities",
]

# Applied in order after loading; each runs on its own so one failure doesn't
# undo the rest.
KEYS = (
    [
        f"ALTER TABLE {SCHEMA}.countries ADD PRIMARY KEY (country)",
        # Unique where present; seas have no code, so iso2 can be NULL.
        f"CREATE UNIQUE INDEX ON {SCHEMA}.countries (iso2)",
        f"ALTER TABLE {SCHEMA}.global_events "
        "ADD COLUMN event_row_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
        # Every weekly row has these. Declared, so the judge and the plugin know
        # a NULL can't occur there; admin1 stays nullable, as a few rows have none.
        f"ALTER TABLE {SCHEMA}.global_events ALTER COLUMN country SET NOT NULL, "
        "ALTER COLUMN week SET NOT NULL, ALTER COLUMN event_type SET NOT NULL, "
        "ALTER COLUMN events SET NOT NULL, ALTER COLUMN fatalities SET NOT NULL",
        f"ALTER TABLE {SCHEMA}.political_violence_country_month "
        "ADD PRIMARY KEY (country, year, month)",
    ]
    + [f"ALTER TABLE {SCHEMA}.{t} ADD PRIMARY KEY (country, year)" for t in COUNTRY_YEAR_TABLES]
    # Every row of the yearly and monthly tables has its count, too.
    + [
        f"ALTER TABLE {SCHEMA}.{t} ALTER COLUMN {column} SET NOT NULL"
        for t, column in [
            ("political_violence_country_month", "events"),
            ("demonstration_events_country", "events"),
            ("events_targeting_civilians_country", "events"),
            ("political_violence_year", "events"),
            ("reported_civilian_fatalities", "fatalities"),
            ("reported_fatalities", "fatalities"),
        ]
    ]
    + [
        f"ALTER TABLE {SCHEMA}.{t} ADD FOREIGN KEY (country) "
        f"REFERENCES {SCHEMA}.countries (country)"
        for t in ["global_events", "political_violence_country_month"]
        + COUNTRY_YEAR_TABLES
    ]
    + [
        # The monthly counts roll up into the yearly ones.
        f"ALTER TABLE {SCHEMA}.political_violence_country_month "
        "ADD FOREIGN KEY (country, year) "
        f"REFERENCES {SCHEMA}.political_violence_year (country, year)",
        f"CREATE INDEX ON {SCHEMA}.global_events (week)",
        f"CREATE INDEX ON {SCHEMA}.global_events (country, week)",
        f"CREATE INDEX ON {SCHEMA}.global_events (event_type)",
        f"CREATE INDEX ON {SCHEMA}.global_events (admin1)",
    ]
    # Only the tables just loaded, not the whole examples database.
    + [f"ANALYZE {SCHEMA}.{t}" for t in ["countries"] + list(SOURCES)]
)


def _files(patterns):
    found = sorted({f for p in patterns for f in glob.glob(os.path.join(DATA_DIR, p))})
    if not found:
        sys.exit(f"No file matching {patterns} in {DATA_DIR}")
    return found


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [str(c).strip().replace("\n", " ").lower() for c in df.columns]
    for col in df.columns:
        if col in DATE_COLS:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
        elif col in INT_COLS:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
        elif col in FLOAT_COLS:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Float64")
        else:
            df[col] = df[col].astype("string").str.strip()
    return df


def _iso_codes(name: str):
    """(alpha-2, alpha-3) for an ACLED country name, or (None, None)."""
    if name in ISO_OVERRIDES:
        alpha2 = ISO_OVERRIDES[name]
        if alpha2 is None:
            return None, None
        if alpha2 == "XK":
            return "XK", "XKX"
        return alpha2, pycountry.countries.get(alpha_2=alpha2).alpha_3
    try:
        match = pycountry.countries.lookup(name)
    except LookupError:
        print(f"  no ISO code for {name!r}: add it to ISO_OVERRIDES")
        return None, None
    return match.alpha_2, match.alpha_3


def _load(engine, table: str, df: pd.DataFrame) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text(f'DROP TABLE IF EXISTS {SCHEMA}."{table}" CASCADE'))
    df.head(0).to_sql(
        table,
        engine,
        schema=SCHEMA,
        index=False,
        dtype={c: t for c, t in COLUMN_TYPES.items() if c in df.columns},
    )
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False)
    buf.seek(0)
    conn = engine.raw_connection()
    try:
        conn.cursor().copy_expert(
            f'COPY {SCHEMA}."{table}" FROM STDIN WITH (FORMAT csv)', buf
        )
        conn.commit()
    finally:
        conn.close()
    print(f"  {table}: {len(df):,} rows")


def main() -> None:
    engine = sa.create_engine(DATABASE_URL)
    tables = {}
    for table, patterns in SOURCES.items():
        files = _files(patterns)
        print(f"Reading {table} from {len(files)} file(s)...", flush=True)
        tables[table] = _clean(
            pd.concat(
                [pd.read_excel(f, engine="openpyxl") for f in files],
                ignore_index=True,
            )
        )

    # Every country in any table, with its region from the weekly data.
    regions = (
        tables["global_events"]
        .dropna(subset=["country"])
        .groupby("country")["region"]
        .agg(lambda r: r.mode().iat[0] if not r.mode().empty else pd.NA)
    )
    names = sorted(pd.concat([t["country"] for t in tables.values()]).dropna().unique())
    codes = [_iso_codes(n) for n in names]
    tables["countries"] = pd.DataFrame(
        {
            "country": names,
            "region": [regions.get(n, pd.NA) for n in names],
            "iso2": [c[0] for c in codes],
            "iso3": [c[1] for c in codes],
        }
    ).astype("string")

    print("Loading...")
    with engine.begin() as conn:
        conn.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
    for table in ["countries"] + list(SOURCES):
        _load(engine, table, tables[table])

    print("Adding keys and indexes...")
    for statement in KEYS:
        try:
            with engine.begin() as conn:
                conn.execute(sa.text(statement))
        except sa.exc.DBAPIError as e:
            print(f"  SKIPPED: {statement}\n    {e.orig}".rstrip())
    print("Done.")


if __name__ == "__main__":
    main()
