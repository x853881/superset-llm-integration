"""Load open datasets that link to ACLED into Superset's examples database,
one schema per source:

    geo                   country_info   GeoNames countries: the code crosswalk
                                         (iso2, iso3, iso_numeric, fips, and
                                         gw_code, Gleditsch-Ward, for UCDP)
                          gn_cities      GeoNames places with 500+ people
                          gn_admin1      GeoNames first-level admin areas
                          gn_admin2      GeoNames second-level admin areas
                          haversine_km() great-circle distance, in km
    ourairports           oa_airports, oa_runways, oa_navaids,
                          oa_airport_frequencies, oa_countries, oa_regions
    openflights           of_routes (airport-to-airport graph), of_airlines
    opensanctions_public  os_targets, os_target_countries, os_target_aliases
    ucdp                  ucdp_ged       UCDP georeferenced conflict events
    wdi                   wdi_wide       World Bank WDI, one column per year
                          wdi            the same, one row per country,
                                         indicator and year (WDI_INDICATORS only)
                          wdi_indicators indicator metadata

and optimised copies of the slow ones (see SUMMARIES): ucdp.ucdp_country_year
(a materialized view), openflights.of_route_legs,
opensanctions_public.os_country_counts and geo.acled_region_links.

The ACLED tables in the test database are ACLED's aggregated downloads
(weekly, by admin1, with the region's centre point), so these link to ACLED
through acled.countries (iso2, iso3), admin1 names, centroid coordinates and
weeks, not through event-level columns.

Each file goes into an all-text staging table with COPY, then into its typed
table, so a value that doesn't fit its type stops the load and says where.
Columns are lowercased; empty strings become NULL; semicolon-separated columns
keep their raw value and get a child table with one row per value.

Each dataset's schema is dropped and rebuilt, so re-running is safe. Then
adds primary keys, foreign keys within each schema and join indexes, and runs
ANALYZE. Plain PostgreSQL only: no extensions, as the plugin can't assume
any. A key the data doesn't satisfy is reported and skipped rather than
stopping the load.

Run with test_data/load_linked.sh, which runs this in a throwaway container
on Superset's Docker network.
"""

import csv
import io
import os
import sys
import time
import zipfile

import psycopg2

RAW_DIR = os.getenv("RAW_DIR", "/raw")
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "/downloads")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://examples:examples@db:5432/examples"
).replace("postgresql+psycopg2://", "postgresql://")

ALL = ["geo", "ourairports", "openflights", "opensanctions", "ucdp", "wdi"]
ONLY = [d for d in os.getenv("LINKED_ONLY", ",".join(ALL)).split(",") if d]

# Indicators unpivoted into wdi.wdi; wdi_wide has them all.
WDI_INDICATORS = os.getenv(
    "WDI_INDICATORS",
    "SP.POP.TOTL,"  # population
    "NY.GDP.MKTP.CD,"  # GDP, current US$
    "NY.GDP.PCAP.CD,"  # GDP per capita, current US$
    "MS.MIL.XPND.CD,"  # military expenditure, current US$
    "MS.MIL.XPND.GD.P,"  # military expenditure, % of GDP
    "SM.POP.REFG,"  # refugees by country of asylum
    "SM.POP.REFG.OR,"  # refugees by country of origin
    "SP.URB.TOTL.IN.ZS",  # urban population, % of total
).split(",")

# COPY options for each file format.
CSV_HEADER = "FORMAT csv, HEADER true"
# GeoNames: tab-separated, no header, no quoting (\x01 never appears).
TSV = "FORMAT csv, DELIMITER E'\\t', QUOTE E'\\x01'"
# OpenFlights: CSV, no header, \N for NULL.
OPENFLIGHTS = "FORMAT csv, NULL '\\N'"

# How each type is cast from the staging text.
CASTS = {
    "text": "NULLIF({c}, '')",
    "int": "NULLIF(trim({c}), '')::integer",
    "bigint": "NULLIF(trim({c}), '')::bigint",
    "float": "NULLIF(trim({c}), '')::double precision",
    "date": "left(NULLIF(trim({c}), ''), 10)::date",
    "timestamp": "NULLIF(trim({c}), '')::timestamp",
    "bool": "NULLIF(trim({c}), '')::boolean",
    "yes": "(NULLIF(trim({c}), '') = 'yes')",
    "Y": "(NULLIF(trim({c}), '') IS NOT NULL)",
}

# UCDP names its countries in its own way (often with a former name in
# brackets). These are GeoNames' names for the ones that differ once the
# brackets are removed.
UCDP_TO_GEONAMES = {
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Cambodia (Kampuchea)": "Cambodia",
    "DR Congo (Zaire)": "Democratic Republic of the Congo",
    "Congo": "Republic of the Congo",
    "Kingdom of eSwatini (Swaziland)": "Eswatini",
    "Macedonia, FYR": "North Macedonia",
    "Madagascar (Malagasy)": "Madagascar",
    "Myanmar (Burma)": "Myanmar",
    "Russia (Soviet Union)": "Russia",
    "Serbia (Yugoslavia)": "Serbia",
    "Yemen (North Yemen)": "Yemen",
    "Zimbabwe (Rhodesia)": "Zimbabwe",
    "Vietnam (North Vietnam)": "Vietnam",
    "Burkina Faso (Upper Volta)": "Burkina Faso",
    "Netherlands": "The Netherlands",
    "United States of America": "United States",
}


def snake(name):
    out = "".join(ch.lower() if ch.isalnum() else "_" for ch in name.strip())
    out = "_".join(p for p in out.split("_") if p)
    return f"y{out}" if out[:1].isdigit() else out


def csv_header(stream):
    """Column names from the first line of a CSV, then rewind."""
    line = stream.readline().decode("utf-8-sig")
    stream.seek(0)
    return [snake(h) for h in next(csv.reader([line]))]


class Loader:
    def __init__(self, conn):
        self.conn = conn
        self.cur = conn.cursor()
        self.skipped = []

    def sql(self, statement, *args):
        self.cur.execute(statement, args or None)

    def one(self, statement):
        self.cur.execute(statement)
        return self.cur.fetchone()[0]

    def fresh_schema(self, schema):
        self.sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        self.sql(f"CREATE SCHEMA {schema}")

    def load(self, schema, table, cols, stream, copy_opts, types=None, extra=""):
        """COPY stream into a text staging table, then build schema.table
        with each column cast to its type (default text). `extra` adds
        computed columns to the SELECT."""
        types = types or {}
        stage = f"{schema}._stage_{table}"
        self.sql(f"DROP TABLE IF EXISTS {stage}")
        self.sql(
            f"CREATE UNLOGGED TABLE {stage} ({', '.join(f'{c} text' for c in cols)})"
        )
        self.cur.copy_expert(f"COPY {stage} FROM STDIN WITH ({copy_opts})", stream)
        select = ", ".join(
            f"{CASTS[types.get(c, 'text')].format(c=c)} AS {c}" for c in cols
        )
        self.sql(
            f"CREATE TABLE {schema}.{table} AS SELECT {select}{extra} FROM {stage}"
        )
        self.sql(f"DROP TABLE {stage}")
        self.conn.commit()
        rows = self.one(f"SELECT count(*) FROM {schema}.{table}")
        print(f"  {schema}.{table}: {rows:,} rows")

    def try_all(self, statements):
        """Run each on its own; report and skip any the data doesn't allow."""
        for s in statements:
            try:
                self.sql(s)
                self.conn.commit()
            except psycopg2.Error as e:
                self.conn.rollback()
                msg = (e.pgerror or str(e)).strip().splitlines()[0]
                self.skipped.append((s, msg))
                print(f"  skipped: {s}\n    {msg}")


def raw(*parts):
    path = os.path.join(RAW_DIR, *parts)
    if not os.path.exists(path):
        sys.exit(f"Missing {path}: run test_data/download_linked.sh first.")
    return path


def download(name):
    path = os.path.join(DOWNLOADS_DIR, name)
    if not os.path.exists(path):
        sys.exit(f"Missing {path}: download it to ~/Downloads (or LINKED_DOWNLOADS).")
    return path


def zip_member(path, name):
    return zipfile.ZipFile(path).open(name)


# --------------------------------------------------------------------------
# Datasets


def load_geo(db):
    print("geo (GeoNames)")
    db.fresh_schema("geo")

    # countryInfo.txt: comment lines start with #; the column names are in the
    # last one, but these are cleaner.
    lines = [
        ln
        for ln in open(raw("geonames", "countryInfo.txt"), "rb")
        if not ln.startswith(b"#")
    ]
    cols = [
        "iso", "iso3", "iso_numeric", "fips", "country", "capital",
        "area_sq_km", "population", "continent", "tld", "currency_code",
        "currency_name", "phone", "postal_code_format", "postal_code_regex",
        "languages", "geonameid", "neighbours", "equivalent_fips_code",
    ]  # fmt: skip
    db.load(
        "geo", "country_info", cols, io.BytesIO(b"".join(lines)), TSV,
        {"area_sq_km": "float", "population": "bigint", "geonameid": "int"},
        # Filled from UCDP once both are loaded (link_gw_codes).
        extra=", NULL::integer AS gw_code",
    )  # fmt: skip

    # Column lists from https://download.geonames.org/export/dump/readme.txt
    cols = [
        "geonameid", "name", "asciiname", "alternatenames", "latitude",
        "longitude", "feature_class", "feature_code", "country_code", "cc2",
        "admin1_code", "admin2_code", "admin3_code", "admin4_code",
        "population", "elevation", "dem", "timezone", "modification_date",
    ]  # fmt: skip
    db.load(
        "geo", "gn_cities", cols,
        zip_member(raw("geonames", "cities500.zip"), "cities500.txt"), TSV,
        {"geonameid": "int", "latitude": "float", "longitude": "float",
         "population": "bigint", "elevation": "int", "dem": "int",
         "modification_date": "date"},
    )  # fmt: skip

    for table, file, parts in [
        ("gn_admin1", "admin1CodesASCII.txt", ["country_code", "admin1_code"]),
        ("gn_admin2", "admin2Codes.txt", ["country_code", "admin1_code", "admin2_code"]),
    ]:
        # code is e.g. NG.05 (admin1) or NG.05.123 (admin2); split it so the
        # parts join to gn_cities.
        split = "".join(
            f", split_part(code, '.', {i + 1}) AS {p}" for i, p in enumerate(parts)
        )
        db.load(
            "geo", table, ["code", "name", "asciiname", "geonameid"],
            open(raw("geonames", file), "rb"), TSV, {"geonameid": "int"},
            extra=split,
        )  # fmt: skip

    # No PostGIS in the examples database: distance joins pre-filter on a
    # latitude/longitude box, then use this.
    db.sql("""
        CREATE FUNCTION geo.haversine_km(
            lat1 double precision, lon1 double precision,
            lat2 double precision, lon2 double precision)
        RETURNS double precision LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
          SELECT 2 * 6371.0088 * asin(sqrt(
            power(sin(radians(lat2 - lat1) / 2), 2)
            + cos(radians(lat1)) * cos(radians(lat2))
              * power(sin(radians(lon2 - lon1) / 2), 2)))
        $$
    """)
    db.sql(
        "COMMENT ON FUNCTION geo.haversine_km IS "
        "'Great-circle distance in km between two latitude/longitude points'"
    )
    db.conn.commit()


def load_ourairports(db):
    print("ourairports")
    db.fresh_schema("ourairports")
    files = {
        "oa_airports": ("airports.csv", {
            "id": "int", "latitude_deg": "float", "longitude_deg": "float",
            "elevation_ft": "int", "scheduled_service": "yes"}),
        "oa_runways": ("runways.csv", {
            "id": "int", "airport_ref": "int", "length_ft": "int",
            "width_ft": "int", "lighted": "bool", "closed": "bool",
            "le_latitude_deg": "float", "le_longitude_deg": "float",
            "le_elevation_ft": "int", "le_heading_degt": "float",
            "le_displaced_threshold_ft": "int", "he_latitude_deg": "float",
            "he_longitude_deg": "float", "he_elevation_ft": "int",
            "he_heading_degt": "float", "he_displaced_threshold_ft": "int"}),
        "oa_navaids": ("navaids.csv", {
            "id": "int", "frequency_khz": "float", "latitude_deg": "float",
            "longitude_deg": "float", "elevation_ft": "int",
            "dme_frequency_khz": "float", "dme_latitude_deg": "float",
            "dme_longitude_deg": "float", "dme_elevation_ft": "int",
            "slaved_variation_deg": "float", "magnetic_variation_deg": "float"}),
        "oa_airport_frequencies": ("airport-frequencies.csv", {
            "id": "int", "airport_ref": "int", "frequency_mhz": "float"}),
        "oa_countries": ("countries.csv", {"id": "int"}),
        "oa_regions": ("regions.csv", {"id": "int"}),
    }  # fmt: skip
    for table, (file, types) in files.items():
        f = open(raw("ourairports", file), "rb")
        db.load("ourairports", table, csv_header(f), f, CSV_HEADER, types)


def load_openflights(db):
    print("openflights")
    db.fresh_schema("openflights")
    db.load(
        "openflights", "of_airlines",
        ["airline_id", "name", "alias", "iata", "icao", "callsign", "country", "active"],
        open(raw("openflights", "airlines.dat"), "rb"), OPENFLIGHTS,
        {"airline_id": "int"},
    )  # fmt: skip
    db.load(
        "openflights", "of_routes",
        ["airline", "airline_id", "source_airport", "source_airport_id",
         "dest_airport", "dest_airport_id", "codeshare", "stops", "equipment"],
        open(raw("openflights", "routes.dat"), "rb"), OPENFLIGHTS,
        {"airline_id": "int", "source_airport_id": "int",
         "dest_airport_id": "int", "stops": "int", "codeshare": "Y"},
    )  # fmt: skip
    db.sql("ALTER TABLE openflights.of_routes ADD COLUMN route_id integer "
           "GENERATED ALWAYS AS IDENTITY PRIMARY KEY")  # fmt: skip
    db.conn.commit()


def link_routes_to_airports(db):
    """of_routes.source_airport_ref / dest_airport_ref: the OurAirports id of
    each end. Routes give IATA codes (3 letters), sometimes ICAO (4). Several
    airports can share a code (closed ones keep it), so the biggest open
    airport wins."""
    print("openflights -> ourairports")
    # An IATA match beats an ICAO one, which beats a match on OurAirports'
    # own ident (some small airports' idents look like IATA codes).
    db.sql("""
        CREATE TEMP TABLE airport_codes AS
        SELECT DISTINCT ON (code) code, id FROM (
          SELECT iata_code AS code, 1 AS via, id, type FROM ourairports.oa_airports
          UNION ALL
          SELECT icao_code, 2, id, type FROM ourairports.oa_airports
          UNION ALL
          SELECT ident, 3, id, type FROM ourairports.oa_airports
        ) c
        WHERE code IS NOT NULL
        ORDER BY code, via, array_position(ARRAY['large_airport',
          'medium_airport', 'small_airport', 'seaplane_base', 'heliport',
          'balloonport', 'closed'], type), id
    """)
    for end in ("source", "dest"):
        db.sql(f"ALTER TABLE openflights.of_routes ADD COLUMN {end}_airport_ref integer")
        db.sql(f"""
            UPDATE openflights.of_routes r SET {end}_airport_ref = a.id
              FROM airport_codes a WHERE a.code = r.{end}_airport
        """)
    db.sql("DROP TABLE airport_codes")
    db.conn.commit()


def load_opensanctions(db):
    print("opensanctions_public (OpenSanctions targets)")
    db.fresh_schema("opensanctions_public")
    f = open(download("targets.simple.csv"), "rb")
    db.load(
        "opensanctions_public", "os_targets", csv_header(f), f, CSV_HEADER,
        {"first_seen": "timestamp", "last_seen": "timestamp",
         "last_change": "timestamp"},
    )  # fmt: skip
    # One row per value of the ;-separated columns. The raw columns stay in
    # os_targets, so unnesting at query time can be tested too.
    for table, col, as_col in [
        ("os_target_countries", "countries", "country_code"),
        ("os_target_aliases", "aliases", "alias"),
    ]:
        db.sql(f"""
            CREATE TABLE opensanctions_public.{table} AS
            SELECT DISTINCT t.id AS target_id, trim(v) AS {as_col}
              FROM opensanctions_public.os_targets t,
                   unnest(string_to_array(t.{col}, ';')) v
             WHERE trim(v) <> ''
        """)
        db.conn.commit()
        rows = db.one(f"SELECT count(*) FROM opensanctions_public.{table}")
        print(f"  opensanctions_public.{table}: {rows:,} rows")


def load_ucdp(db):
    print("ucdp (UCDP GED)")
    db.fresh_schema("ucdp")
    path = raw("ucdp", "ged261-csv.zip")
    member = next(n for n in zipfile.ZipFile(path).namelist() if n.endswith(".csv"))
    f = zip_member(path, member)
    head = csv_header(io.BytesIO(f.readline()))
    f = zip_member(path, member)
    ints = [
        "id", "year", "type_of_violence", "conflict_dset_id", "conflict_new_id",
        "dyad_dset_id", "dyad_new_id", "side_a_dset_id", "side_a_new_id",
        "side_b_dset_id", "side_b_new_id", "number_of_sources", "where_prec",
        "priogrid_gid", "country_id", "event_clarity", "date_prec", "deaths_a",
        "deaths_b", "deaths_civilians", "deaths_unknown", "best", "high", "low",
    ]  # fmt: skip
    types = {c: "int" for c in ints}
    types.update(
        active_year="bool", latitude="float", longitude="float",
        date_start="date", date_end="date",
    )  # fmt: skip
    db.load("ucdp", "ucdp_ged", head, f, CSV_HEADER, types)


def link_gw_codes(db):
    """geo.country_info.gw_code from UCDP's country / country_id pairs
    (country_id is the Gleditsch-Ward code). Only countries with an event in
    UCDP get one. Where a GeoNames country has several (Yemen as North Yemen
    and Yemen), the one on most events wins."""
    print("geo.country_info.gw_code from ucdp")
    db.sql("CREATE TEMP TABLE ucdp_names (ucdp text, geonames text)")
    db.cur.executemany("INSERT INTO ucdp_names VALUES (%s, %s)", UCDP_TO_GEONAMES.items())
    db.sql("""
        UPDATE geo.country_info ci SET gw_code = m.country_id
          FROM (
            SELECT DISTINCT ON (geonames) geonames, country_id
              FROM (
                SELECT coalesce(n.geonames, trim(regexp_replace(g.country, '\\(.*\\)', ''))) AS geonames,
                       g.country_id, count(*) AS events
                  FROM ucdp.ucdp_ged g LEFT JOIN ucdp_names n ON n.ucdp = g.country
                 GROUP BY 1, 2
              ) c
             ORDER BY geonames, events DESC
          ) m
         WHERE lower(ci.country) = lower(m.geonames)
    """)
    db.sql("DROP TABLE ucdp_names")
    db.conn.commit()
    db.sql("""
        SELECT DISTINCT country, country_id FROM ucdp.ucdp_ged
         WHERE country_id NOT IN (SELECT gw_code FROM geo.country_info
                                   WHERE gw_code IS NOT NULL)
         ORDER BY 1
    """)
    missing = db.cur.fetchall()
    matched = db.one("SELECT count(*) FROM geo.country_info WHERE gw_code IS NOT NULL")
    print(f"  {matched} countries have a gw_code; UCDP countries with no match: "
          f"{missing or 'none'}")  # fmt: skip


def load_wdi(db):
    print("wdi (World Bank WDI)")
    db.fresh_schema("wdi")
    path = download("WDI_CSV.zip")
    f = zip_member(path, "WDICSV.csv")
    cols = csv_header(io.BytesIO(f.readline()))
    f = zip_member(path, "WDICSV.csv")
    years = [c for c in cols if c.startswith("y") and c[1:].isdigit()]
    db.load("wdi", "wdi_wide", cols, f, CSV_HEADER, {y: "float" for y in years})

    f = zip_member(path, "WDISeries.csv")
    cols = csv_header(io.BytesIO(f.readline()))
    db.load("wdi", "wdi_indicators", cols, zip_member(path, "WDISeries.csv"), CSV_HEADER)

    values = ", ".join(f"({y[1:]}, {y})" for y in years)
    db.sql(f"""
        CREATE TABLE wdi.wdi AS
        SELECT w.country_code AS iso3, w.indicator_code, v.year, v.value
          FROM wdi.wdi_wide w
         CROSS JOIN LATERAL (VALUES {values}) AS v (year, value)
         WHERE w.indicator_code = ANY(%s) AND v.value IS NOT NULL
    """, WDI_INDICATORS)  # fmt: skip
    db.conn.commit()
    rows = db.one("SELECT count(*) FROM wdi.wdi")
    print(f"  wdi.wdi: {rows:,} rows ({len(WDI_INDICATORS)} indicators)")


# --------------------------------------------------------------------------
# Keys and indexes

KEYS = {
    "geo": [
        "ALTER TABLE geo.country_info ADD PRIMARY KEY (iso)",
        "CREATE UNIQUE INDEX ON geo.country_info (iso3)",
        "CREATE UNIQUE INDEX ON geo.country_info (iso_numeric)",
        "CREATE UNIQUE INDEX ON geo.country_info (gw_code)",
        "ALTER TABLE geo.gn_cities ADD PRIMARY KEY (geonameid)",
        "CREATE INDEX ON geo.gn_cities (country_code, admin1_code, admin2_code)",
        "CREATE INDEX ON geo.gn_cities (latitude, longitude)",
        "CREATE INDEX ON geo.gn_cities (lower(name))",
        "ALTER TABLE geo.gn_admin1 ADD PRIMARY KEY (code)",
        "CREATE UNIQUE INDEX ON geo.gn_admin1 (country_code, admin1_code)",
        "CREATE INDEX ON geo.gn_admin1 (lower(name))",
        "ALTER TABLE geo.gn_admin2 ADD PRIMARY KEY (code)",
        "CREATE INDEX ON geo.gn_admin2 (country_code, admin1_code)",
        "ALTER TABLE geo.gn_admin1 ADD FOREIGN KEY (country_code) REFERENCES geo.country_info (iso)",
        "ALTER TABLE geo.gn_admin2 ADD FOREIGN KEY (country_code, admin1_code) "
        "REFERENCES geo.gn_admin1 (country_code, admin1_code)",
        "ALTER TABLE geo.gn_cities ADD FOREIGN KEY (country_code) REFERENCES geo.country_info (iso)",
    ],
    "ourairports": [
        "ALTER TABLE ourairports.oa_countries ADD PRIMARY KEY (id)",
        "CREATE UNIQUE INDEX ON ourairports.oa_countries (code)",
        "ALTER TABLE ourairports.oa_regions ADD PRIMARY KEY (id)",
        "CREATE UNIQUE INDEX ON ourairports.oa_regions (code)",
        "ALTER TABLE ourairports.oa_airports ADD PRIMARY KEY (id)",
        "CREATE UNIQUE INDEX ON ourairports.oa_airports (ident)",
        "CREATE INDEX ON ourairports.oa_airports (iata_code)",
        "CREATE INDEX ON ourairports.oa_airports (icao_code)",
        "CREATE INDEX ON ourairports.oa_airports (iso_country)",
        "CREATE INDEX ON ourairports.oa_airports (iso_region)",
        "CREATE INDEX ON ourairports.oa_airports (latitude_deg, longitude_deg)",
        "ALTER TABLE ourairports.oa_airports ADD FOREIGN KEY (iso_country) "
        "REFERENCES ourairports.oa_countries (code)",
        "ALTER TABLE ourairports.oa_airports ADD FOREIGN KEY (iso_region) "
        "REFERENCES ourairports.oa_regions (code)",
        "ALTER TABLE ourairports.oa_runways ADD PRIMARY KEY (id)",
        "CREATE INDEX ON ourairports.oa_runways (airport_ref)",
        "ALTER TABLE ourairports.oa_runways ADD FOREIGN KEY (airport_ref) "
        "REFERENCES ourairports.oa_airports (id)",
        "ALTER TABLE ourairports.oa_airport_frequencies ADD PRIMARY KEY (id)",
        "CREATE INDEX ON ourairports.oa_airport_frequencies (airport_ref)",
        "ALTER TABLE ourairports.oa_airport_frequencies ADD FOREIGN KEY (airport_ref) "
        "REFERENCES ourairports.oa_airports (id)",
        "ALTER TABLE ourairports.oa_navaids ADD PRIMARY KEY (id)",
        "CREATE INDEX ON ourairports.oa_navaids (associated_airport)",
        "ALTER TABLE ourairports.oa_navaids ADD FOREIGN KEY (associated_airport) "
        "REFERENCES ourairports.oa_airports (ident)",
    ],
    "openflights": [
        "ALTER TABLE openflights.of_airlines ADD PRIMARY KEY (airline_id)",
        "CREATE INDEX ON openflights.of_routes (source_airport)",
        "CREATE INDEX ON openflights.of_routes (dest_airport)",
        "CREATE INDEX ON openflights.of_routes (source_airport_ref)",
        "CREATE INDEX ON openflights.of_routes (dest_airport_ref)",
        "ALTER TABLE openflights.of_routes ADD FOREIGN KEY (airline_id) "
        "REFERENCES openflights.of_airlines (airline_id)",
    ],
    "opensanctions": [
        "ALTER TABLE opensanctions_public.os_targets ADD PRIMARY KEY (id)",
        "CREATE INDEX ON opensanctions_public.os_targets (schema)",
        "ALTER TABLE opensanctions_public.os_target_countries ADD PRIMARY KEY (target_id, country_code)",
        "CREATE INDEX ON opensanctions_public.os_target_countries (country_code)",
        "ALTER TABLE opensanctions_public.os_target_countries ADD FOREIGN KEY (target_id) "
        "REFERENCES opensanctions_public.os_targets (id)",
        "CREATE INDEX ON opensanctions_public.os_target_aliases (target_id)",
        "ALTER TABLE opensanctions_public.os_target_aliases ADD FOREIGN KEY (target_id) "
        "REFERENCES opensanctions_public.os_targets (id)",
    ],
    "ucdp": [
        "ALTER TABLE ucdp.ucdp_ged ADD PRIMARY KEY (id)",
        "CREATE INDEX ON ucdp.ucdp_ged (country_id, date_start)",
        "CREATE INDEX ON ucdp.ucdp_ged (date_start)",
        "CREATE INDEX ON ucdp.ucdp_ged (latitude, longitude)",
        "CREATE INDEX ON ucdp.ucdp_ged (conflict_new_id)",
    ],
    "wdi": [
        "ALTER TABLE wdi.wdi_wide ADD PRIMARY KEY (country_code, indicator_code)",
        "ALTER TABLE wdi.wdi_indicators ADD PRIMARY KEY (series_code)",
        "ALTER TABLE wdi.wdi ADD PRIMARY KEY (iso3, indicator_code, year)",
        "CREATE INDEX ON wdi.wdi (indicator_code, year)",
    ],
}

# Indexes for the goldens' joins and filters beyond the keys above.
KEYS["openflights"].append("CREATE INDEX ON openflights.of_routes (airline_id)")
KEYS["opensanctions"].append("CREATE INDEX ON opensanctions_public.os_targets (lower(name))")
KEYS["ucdp"].append("CREATE INDEX ON ucdp.ucdp_ged (type_of_violence, year)")


# --------------------------------------------------------------------------
# Optimised copies
#
# Pre-computed tables (and one materialized view) holding what the raw tables
# give only through a large scan, a distance calculation or unnesting. The
# goldens test whether the plugin reads these instead of the slow table when
# they give the same answer, and keeps away from them when they don't. Each
# is rebuilt whenever one of its sources is loaded: (name, sources, SQL).

SUMMARIES = [
    ("ucdp.ucdp_country_year", {"ucdp"}, [
        "DROP MATERIALIZED VIEW IF EXISTS ucdp.ucdp_country_year",
        """CREATE MATERIALIZED VIEW ucdp.ucdp_country_year AS
           SELECT country_id, country, region, year, type_of_violence,
                  count(*)::integer AS events,
                  sum(best)::integer AS deaths_best,
                  sum(low)::integer AS deaths_low,
                  sum(high)::integer AS deaths_high,
                  sum(deaths_civilians)::integer AS deaths_civilians
             FROM ucdp.ucdp_ged
            GROUP BY country_id, country, region, year, type_of_violence""",
        "CREATE UNIQUE INDEX ON ucdp.ucdp_country_year (country_id, year, type_of_violence)",
        "CREATE INDEX ON ucdp.ucdp_country_year (year)",
        "COMMENT ON MATERIALIZED VIEW ucdp.ucdp_country_year IS 'ucdp_ged totalled by "
        "country, year and type of violence: the same figures from about 6,000 rows "
        "instead of 418,000'",
    ]),
    ("openflights.of_route_legs", {"openflights", "ourairports"}, [
        "DROP TABLE IF EXISTS openflights.of_route_legs",
        """CREATE TABLE openflights.of_route_legs AS
           SELECT r.source_airport_ref, r.dest_airport_ref,
                  s.iata_code AS source_iata, d.iata_code AS dest_iata,
                  s.iso_country AS source_country, d.iso_country AS dest_country,
                  geo.haversine_km(s.latitude_deg, s.longitude_deg,
                         d.latitude_deg, d.longitude_deg) AS distance_km,
                  count(*)::integer AS routes,
                  count(DISTINCT r.airline_id)::integer AS airlines
             FROM openflights.of_routes r
             JOIN ourairports.oa_airports s ON s.id = r.source_airport_ref
             JOIN ourairports.oa_airports d ON d.id = r.dest_airport_ref
            GROUP BY 1, 2, 3, 4, 5, 6, 7""",
        "ALTER TABLE openflights.of_route_legs ADD PRIMARY KEY (source_airport_ref, dest_airport_ref)",
        "CREATE INDEX ON openflights.of_route_legs (dest_airport_ref)",
        "CREATE INDEX ON openflights.of_route_legs (source_country, dest_country)",
        "COMMENT ON TABLE openflights.of_route_legs IS 'of_routes collapsed to one row per "
        "airport pair (source to destination), with both ends'' countries and the "
        "great-circle distance already worked out'",
        "COMMENT ON COLUMN openflights.of_route_legs.routes IS 'rows of of_routes for this pair "
        "(one per airline and codeshare)'",
    ]),
    ("opensanctions_public.os_country_counts", {"opensanctions"}, [
        "DROP TABLE IF EXISTS opensanctions_public.os_country_counts",
        """CREATE TABLE opensanctions_public.os_country_counts AS
           SELECT c.country_code, t.schema, count(*)::integer AS targets
             FROM opensanctions_public.os_target_countries c
             JOIN opensanctions_public.os_targets t ON t.id = c.target_id
            GROUP BY 1, 2""",
        "ALTER TABLE opensanctions_public.os_country_counts ADD PRIMARY KEY (country_code, schema)",
        "COMMENT ON TABLE opensanctions_public.os_country_counts IS 'Targets per country code "
        "and schema (Person, Company, ...). A target linked to several countries is "
        "counted once under each, so these don''t add up across countries'",
    ]),
    ("geo.acled_region_links", {"geo", "ourairports"}, [
        "DROP TABLE IF EXISTS geo.acled_region_links",
        # ACLED regions are global_events.id: one per country and admin1.
        """CREATE TABLE geo.acled_region_links AS
           WITH r AS (
             SELECT DISTINCT e.id AS region_id, e.country, e.admin1, c.iso2,
                    e.centroid_latitude AS latitude, e.centroid_longitude AS longitude
               FROM acled.global_events e JOIN acled.countries c USING (country)
           )
           SELECT r.*, a1.code AS gn_admin1_code,
                  city.geonameid AS nearest_city_id, city.name AS nearest_city,
                  city.km AS nearest_city_km,
                  ap.id AS nearest_airport_id, ap.ident AS nearest_airport_ident,
                  ap.km AS nearest_airport_km
             FROM r
             LEFT JOIN LATERAL (
               SELECT g.code FROM geo.gn_admin1 g
                WHERE g.country_code = r.iso2
                  AND lower(r.admin1) IN (lower(g.name), lower(g.asciiname))
                ORDER BY r.admin1 IN (g.name, g.asciiname) DESC, g.code
                LIMIT 1) a1 ON true
             LEFT JOIN LATERAL (
               SELECT g.geonameid, g.name,
                      geo.haversine_km(r.latitude, r.longitude, g.latitude, g.longitude) AS km
                 FROM geo.gn_cities g
                WHERE g.latitude BETWEEN r.latitude - 1 AND r.latitude + 1
                  AND g.longitude BETWEEN r.longitude - 1 / greatest(cos(radians(r.latitude)), 0.01)
                                      AND r.longitude + 1 / greatest(cos(radians(r.latitude)), 0.01)
                ORDER BY 3, 1 LIMIT 1) city ON true
             LEFT JOIN LATERAL (
               SELECT p.id, p.ident,
                      geo.haversine_km(r.latitude, r.longitude, p.latitude_deg, p.longitude_deg) AS km
                 FROM ourairports.oa_airports p
                WHERE p.type IN ('large_airport', 'medium_airport')
                ORDER BY 3, 1 LIMIT 1) ap ON true""",
        "ALTER TABLE geo.acled_region_links ADD PRIMARY KEY (region_id)",
        "CREATE UNIQUE INDEX ON geo.acled_region_links (country, admin1)",
        "CREATE INDEX ON geo.acled_region_links (nearest_airport_id)",
        "COMMENT ON TABLE geo.acled_region_links IS 'One row per ACLED region (acled.global_events.id, "
        "a country and admin1) with its links worked out: the GeoNames admin1 area its name "
        "matches, and the nearest place and large or medium airport to its centre point'",
        "COMMENT ON COLUMN geo.acled_region_links.nearest_city_km IS 'km from the region''s centre "
        "to the nearest GeoNames place; NULL when none is within about 110 km'",
        "COMMENT ON COLUMN geo.acled_region_links.nearest_airport_km IS 'km from the region''s centre "
        "to the nearest large or medium airport (OurAirports), anywhere'",
    ]),
]


def build_summaries(db):
    print("optimised copies")
    for name, sources, statements in SUMMARIES:
        schemas = [SCHEMAS[s] for s in sources] + [name.split(".")[0]]
        if not sources & set(ONLY) or not all(schema_exists(db, s) for s in schemas):
            continue
        start = time.time()
        db.try_all(statements)
        if db.one(f"SELECT to_regclass('{name}') IS NOT NULL"):
            rows = db.one(f"SELECT count(*) FROM {name}")
            print(f"  {name}: {rows:,} rows ({time.time() - start:.0f}s)")


LOADERS = {
    "geo": load_geo,
    "ourairports": load_ourairports,
    "openflights": load_openflights,
    "opensanctions": load_opensanctions,
    "ucdp": load_ucdp,
    "wdi": load_wdi,
}

SCHEMAS = {d: d for d in ALL} | {"opensanctions": "opensanctions_public"}


def schema_exists(db, schema):
    return db.one(f"SELECT to_regnamespace('{schema}') IS NOT NULL")


def main():
    unknown = set(ONLY) - set(ALL)
    if unknown:
        sys.exit(f"Unknown datasets: {', '.join(unknown)}. Choose from {', '.join(ALL)}.")
    db = Loader(psycopg2.connect(DATABASE_URL))

    for ds in ALL:
        if ds in ONLY:
            start = time.time()
            LOADERS[ds](db)
            print(f"  ({time.time() - start:.0f}s)")

    # Links between datasets, redone whenever either side was reloaded.
    if {"openflights", "ourairports"} & set(ONLY) and all(
        schema_exists(db, s) for s in ("openflights", "ourairports")
    ):
        link_routes_to_airports(db)
    link_ucdp = {"geo", "ucdp"} & set(ONLY) and all(
        schema_exists(db, s) for s in ("geo", "ucdp")
    )
    if link_ucdp:
        link_gw_codes(db)

    print("keys and indexes")
    for ds in ALL:
        if ds in ONLY:
            db.try_all(KEYS[ds])
    # Reloading either side drops this key (DROP SCHEMA ... CASCADE), so it's
    # added after both sides' keys, including the unique index on gw_code.
    if link_ucdp:
        db.try_all([
            "ALTER TABLE ucdp.ucdp_ged ADD FOREIGN KEY (country_id) "
            "REFERENCES geo.country_info (gw_code)",
        ])
    build_summaries(db)
    db.sql("ANALYZE")
    db.conn.commit()
    print(f"Done. {len(db.skipped)} keys or indexes skipped.")


if __name__ == "__main__":
    main()
