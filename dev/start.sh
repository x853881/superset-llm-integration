#!/usr/bin/env bash
set -e

superset db upgrade

superset fab create-admin \
  --username admin --password admin \
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

# Flask dev server with auto-reload instead of gunicorn
exec superset run -h 0.0.0.0 -p 8088 --with-threads --reload --debugger
