#!/usr/bin/env bash
# Load the open datasets that link to ACLED into Superset's examples database
# (see load_linked.py for the tables).
#
#   ./test_data/download_linked.sh   # first: fetches into test_data/raw/
#   ./test_data/load_linked.sh       # everything
#   LINKED_ONLY=geo,ucdp ./test_data/load_linked.sh
#
# WDI_CSV.zip and OpenSanctions' targets.simple.csv are read from
# ~/Downloads, or LINKED_DOWNLOADS. DATABASE_URL, LINKED_ONLY and
# WDI_INDICATORS, if set here, are passed on to load_linked.py.
#
# Runs in a throwaway container on Superset's Docker network, so nothing is
# installed on this machine. Superset must be running. Re-running replaces
# the tables of each dataset loaded.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DOWNLOADS="${LINKED_DOWNLOADS:-$HOME/Downloads}"

docker run --rm \
  --network "${SUPERSET_NETWORK:-superset_default}" \
  -e DATABASE_URL \
  -e LINKED_ONLY \
  -e WDI_INDICATORS \
  -e PYTHONUNBUFFERED=1 \
  -v "$HERE/raw:/raw:ro" \
  -v "$DOWNLOADS:/downloads:ro" \
  -v "$HERE:/app:ro" \
  python:3.11-slim \
  sh -c "pip install -q --root-user-action=ignore psycopg2-binary \
         && python /app/load_linked.py"
