#!/usr/bin/env bash
set -e

superset db upgrade

# The admin, from SUPERSET_USERNAME / SUPERSET_PASSWORD in .env (admin / admin by default). The
# acceptance tests sign in as it to create a fresh analyst for each test (e2e/src/analysts.ts).
superset fab create-admin \
  --username "${ADMIN_USERNAME:-admin}" --password "${ADMIN_PASSWORD:-admin}" \
  --firstname Admin --lastname User \
  --email admin@example.com || true

superset init

# Example dashboards/datasets, loaded once on first startup.
# The data is downloaded at runtime, so nothing is stored in this repo.
# Delete the volume (docker compose down -v) to start fresh.
EXAMPLES_MARKER=/app/superset_home/.examples_loaded
if [ ! -f "$EXAMPLES_MARKER" ] && [ "${LOAD_EXAMPLES:-yes}" = "yes" ]; then
  echo "Loading example data (first run only, takes a few minutes)..."
  superset load_examples && touch "$EXAMPLES_MARKER"
fi

# The "Warehouse" database (the postgres service), for analysts to query in SQL Lab.
python /app/dev/register_warehouse.py

# The acceptance tests use Superset's production server (gunicorn): the Flask dev server sometimes
# holds a response until another request arrives, which leaves SQL Lab loading for ever.
if [ "${SUPERSET_SERVER:-dev}" = "gunicorn" ]; then
  exec /usr/bin/run-server.sh
fi

# Flask dev server with auto-reload instead of gunicorn
exec superset run -h 0.0.0.0 -p 8088 --with-threads --reload --debugger
