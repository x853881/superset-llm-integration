#!/usr/bin/env bash
# Load the ACLED test tables into Superset's examples database.
#
#   ./test_data/load_acled.sh                    # files in ~/Downloads
#   ACLED_DIR=~/acled ./test_data/load_acled.sh  # files somewhere else
#
# ACLED_SCHEMA and DATABASE_URL, if set here, are passed on to load_acled.py.
#
# Runs in a throwaway container on Superset's Docker network, so nothing is
# installed on this machine. Superset must be running. Re-running replaces
# the tables.
set -euo pipefail

ACLED_DIR="${ACLED_DIR:-$HOME/Downloads}"
HERE="$(cd "$(dirname "$0")" && pwd)"

docker run --rm \
  --network "${SUPERSET_NETWORK:-superset_default}" \
  -e ACLED_SCHEMA \
  -e DATABASE_URL \
  -v "$ACLED_DIR:/data:ro" \
  -v "$HERE:/app:ro" \
  python:3.11-slim \
  sh -c "pip install -q --root-user-action=ignore pandas openpyxl pycountry sqlalchemy psycopg2-binary \
         && python /app/load_acled.py"
