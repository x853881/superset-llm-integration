-- Row counts and match rates for the links between ACLED and the datasets
-- load_linked.py adds. Read-only. Run with:
--
--   docker exec -i superset-db-1 psql -U examples -d examples < test_data/validate_linked.sql
--
-- ACLED here is the aggregated download: weekly rows by admin1, with the
-- region's centre point (centroid_latitude/longitude), so distances are from
-- region centres and dates are ACLED weeks (Saturday to Friday).

\set QUIET on
\pset footer off
SET client_min_messages = warning;
SET statement_timeout = '5min';  -- so a bad plan can't run for hours

CREATE TEMP TABLE checks (n serial, link text, matched bigint, total bigint);

-- Countries -----------------------------------------------------------------

INSERT INTO checks (link, matched, total)
SELECT 'acled.countries -> geo.country_info (iso2)',
       count(ci.iso), count(*)
  FROM acled.countries c LEFT JOIN geo.country_info ci ON ci.iso = c.iso2;

INSERT INTO checks (link, matched, total)
SELECT 'acled.global_events rows -> geo.country_info',
       count(ci.iso), count(*)
  FROM acled.global_events e
  JOIN acled.countries c USING (country)
  LEFT JOIN geo.country_info ci ON ci.iso = c.iso2;

INSERT INTO checks (link, matched, total)
SELECT 'acled.countries -> gw_code (UCDP)',
       count(ci.gw_code), count(*)
  FROM acled.countries c LEFT JOIN geo.country_info ci ON ci.iso = c.iso2;

INSERT INTO checks (link, matched, total)
SELECT 'ucdp.ucdp_ged rows -> geo.country_info (gw_code)',
       count(ci.iso), count(*)
  FROM ucdp.ucdp_ged g LEFT JOIN geo.country_info ci ON ci.gw_code = g.country_id;

INSERT INTO checks (link, matched, total)
SELECT 'oa_airports -> geo.country_info (iso_country)',
       count(ci.iso), count(*)
  FROM ourairports.oa_airports a LEFT JOIN geo.country_info ci ON ci.iso = a.iso_country;

INSERT INTO checks (link, matched, total)
SELECT 'os_target_countries -> geo.country_info (upper)',
       count(ci.iso), count(*)
  FROM opensanctions_public.os_target_countries t
  LEFT JOIN geo.country_info ci ON ci.iso = upper(t.country_code);

INSERT INTO checks (link, matched, total)
SELECT 'wdi.wdi_wide rows -> geo.country_info (iso3)',
       count(ci.iso), count(*)
  FROM wdi.wdi_wide w LEFT JOIN geo.country_info ci ON ci.iso3 = w.country_code;

INSERT INTO checks (link, matched, total)
SELECT 'ACLED country-years (2000-2024) -> WDI population',
       count(w.value), count(*)
  FROM acled.reported_fatalities f
  JOIN acled.countries c USING (country)
  LEFT JOIN wdi.wdi w ON w.iso3 = c.iso3 AND w.year = f.year
                     AND w.indicator_code = 'SP.POP.TOTL'
 WHERE f.year BETWEEN 2000 AND 2024;

-- Places --------------------------------------------------------------------

CREATE TEMP TABLE acled_admin1 AS
SELECT DISTINCT e.country, c.iso2, e.admin1
  FROM acled.global_events e JOIN acled.countries c USING (country)
 WHERE e.admin1 IS NOT NULL;

INSERT INTO checks (link, matched, total)
SELECT 'ACLED admin1 -> gn_admin1: exact name',
       count(*) FILTER (WHERE EXISTS (
         SELECT 1 FROM geo.gn_admin1 g
          WHERE g.country_code = a.iso2 AND a.admin1 IN (g.name, g.asciiname))),
       count(*)
  FROM acled_admin1 a;

INSERT INTO checks (link, matched, total)
SELECT 'ACLED admin1 -> gn_admin1: exact or case-insensitive',
       count(*) FILTER (WHERE EXISTS (
         SELECT 1 FROM geo.gn_admin1 g
          WHERE g.country_code = a.iso2
            AND lower(a.admin1) IN (lower(g.name), lower(g.asciiname)))),
       count(*)
  FROM acled_admin1 a;

CREATE TEMP TABLE acled_centroids AS
SELECT DISTINCT country, admin1, centroid_latitude AS lat, centroid_longitude AS lon
  FROM acled.global_events
 WHERE centroid_latitude IS NOT NULL;

INSERT INTO checks (link, matched, total)
SELECT 'ACLED region centres with a GeoNames city within 25 km',
       count(*) FILTER (WHERE EXISTS (
         SELECT 1 FROM geo.gn_cities g
          WHERE g.latitude BETWEEN a.lat - 0.25 AND a.lat + 0.25
            AND g.longitude BETWEEN a.lon - 0.25 / greatest(cos(radians(a.lat)), 0.01)
                                AND a.lon + 0.25 / greatest(cos(radians(a.lat)), 0.01)
            AND geo.haversine_km(a.lat, a.lon, g.latitude, g.longitude) <= 25)),
       count(*)
  FROM acled_centroids a;

INSERT INTO checks (link, matched, total)
SELECT 'ACLED region centres with a large/medium airport within 50 km',
       count(*) FILTER (WHERE EXISTS (
         SELECT 1 FROM ourairports.oa_airports p
          WHERE p.type IN ('large_airport', 'medium_airport')
            AND p.latitude_deg BETWEEN a.lat - 0.5 AND a.lat + 0.5
            AND p.longitude_deg BETWEEN a.lon - 0.5 / greatest(cos(radians(a.lat)), 0.01)
                                    AND a.lon + 0.5 / greatest(cos(radians(a.lat)), 0.01)
            AND geo.haversine_km(a.lat, a.lon, p.latitude_deg, p.longitude_deg) <= 50)),
       count(*)
  FROM acled_centroids a;

-- Airports and routes -------------------------------------------------------

INSERT INTO checks (link, matched, total)
SELECT 'of_routes: source airport matched', count(source_airport_ref), count(*)
  FROM openflights.of_routes;

INSERT INTO checks (link, matched, total)
SELECT 'of_routes: destination airport matched', count(dest_airport_ref), count(*)
  FROM openflights.of_routes;

INSERT INTO checks (link, matched, total)
SELECT 'of_routes -> of_airlines', count(a.airline_id), count(*)
  FROM openflights.of_routes r LEFT JOIN openflights.of_airlines a USING (airline_id);

INSERT INTO checks (link, matched, total)
SELECT 'oa_runways -> oa_airports', count(a.id), count(*)
  FROM ourairports.oa_runways r LEFT JOIN ourairports.oa_airports a ON a.id = r.airport_ref;

INSERT INTO checks (link, matched, total)
SELECT 'oa_navaids with an associated airport', count(associated_airport), count(*)
  FROM ourairports.oa_navaids;

-- ACLED <-> UCDP ------------------------------------------------------------

-- ACLED weekly rows with fatalities in 2024-2025 (UCDP GED 26.1 ends in
-- 2025) that have a UCDP event in the same country and week, within 50 km
-- of the region's centre.
INSERT INTO checks (link, matched, total)
SELECT 'ACLED fatal weeks 2024-25 -> UCDP event (country, week, 50 km)',
       count(*) FILTER (WHERE EXISTS (
         SELECT 1 FROM ucdp.ucdp_ged g
          WHERE g.country_id = ci.gw_code
            AND g.date_start BETWEEN e.week AND e.week + 6
            AND g.latitude BETWEEN e.centroid_latitude - 0.5 AND e.centroid_latitude + 0.5
            AND geo.haversine_km(e.centroid_latitude, e.centroid_longitude,
                                 g.latitude, g.longitude) <= 50)),
       count(*)
  FROM acled.global_events e
  JOIN acled.countries c USING (country)
  LEFT JOIN geo.country_info ci ON ci.iso = c.iso2
 WHERE e.week >= DATE '2024-01-01' AND e.week < DATE '2025-12-27'
   AND e.fatalities > 0;

-- Names ---------------------------------------------------------------------

CREATE TEMP TABLE ucdp_sides AS
SELECT side_a AS side FROM ucdp.ucdp_ged
UNION
SELECT side_b FROM ucdp.ucdp_ged;

-- Plain PostgreSQL, no pg_trgm: the same name, ignoring case. IN over a
-- subquery is a hashed semi-join, so it reads each table once.
INSERT INTO checks (link, matched, total)
SELECT 'UCDP side names -> os_targets.name (same name, any case)',
       (SELECT count(*) FROM ucdp_sides s WHERE lower(s.side) IN (
          SELECT lower(t.name) FROM opensanctions_public.os_targets t)),
       (SELECT count(*) FROM ucdp_sides);

INSERT INTO checks (link, matched, total)
SELECT 'UCDP side names -> os_target_aliases (same name, any case)',
       (SELECT count(*) FROM ucdp_sides s WHERE lower(s.side) IN (
          SELECT lower(t.alias) FROM opensanctions_public.os_target_aliases t)),
       (SELECT count(*) FROM ucdp_sides);

-- Report --------------------------------------------------------------------

\echo
\echo 'Row counts'
SELECT n.nspname || '.' || c.relname AS "table", c.reltuples::bigint AS "rows (estimate)"
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind IN ('r', 'm')
   AND n.nspname IN ('acled', 'geo', 'ourairports', 'openflights',
                     'opensanctions_public', 'ucdp', 'wdi')
 ORDER BY 1;

\echo 'Link match rates'
SELECT link, matched, total,
       round(100.0 * matched / nullif(total, 0), 1) AS "% matched"
  FROM checks ORDER BY n;

\echo 'ACLED countries with no GeoNames country (no ISO code, or not in GeoNames)'
SELECT c.country, c.iso2
  FROM acled.countries c LEFT JOIN geo.country_info ci ON ci.iso = c.iso2
 WHERE ci.iso IS NULL ORDER BY 1;
