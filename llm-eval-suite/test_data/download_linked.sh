#!/usr/bin/env bash
# Download the open datasets that link to ACLED, into test_data/raw/
# (git-ignored). Only the files the loader uses; nothing optional.
#
#   ./test_data/download_linked.sh                      # everything below
#   ./test_data/download_linked.sh --only geo,ucdp       # some datasets
#
# Datasets: geo, ourairports, openflights, ucdp. A file already in raw/ is
# skipped, so re-running is cheap.
#
# Not downloaded here (load_linked.sh reads them from ~/Downloads, or
# LINKED_DOWNLOADS):
#   WDI_CSV.zip         https://databankfiles.worldbank.org/public/ddpext_download/WDI_CSV.zip
#   targets.simple.csv  https://data.opensanctions.org/datasets/latest/default/targets.simple.csv
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RAW="$HERE/raw"
ONLY="geo,ourairports,openflights,ucdp"
if [[ "${1:-}" == "--only" ]]; then ONLY="$2"; fi

GEONAMES=https://download.geonames.org/export/dump
OURAIRPORTS=https://davidmegginson.github.io/ourairports-data
OPENFLIGHTS=https://raw.githubusercontent.com/jpatokal/openflights/master/data
# UCDP GED Global, CSV. Bump the version when UCDP publishes a new one.
UCDP_GED=https://ucdp.uu.se/downloads/ged/ged261-csv.zip

fetch() {  # url dir
  local url="$1" dir="$RAW/$2" file
  file="$dir/$(basename "$url")"
  mkdir -p "$dir"
  if [[ -s "$file" ]]; then
    echo "have  $file ($(du -h "$file" | cut -f1))"
    return
  fi
  curl -fsSL --retry 3 -o "$file.part" "$url"
  mv "$file.part" "$file"
  echo "got   $file ($(du -h "$file" | cut -f1))"
}

for ds in ${ONLY//,/ }; do
  case "$ds" in
    geo)
      for f in countryInfo.txt cities500.zip admin1CodesASCII.txt admin2Codes.txt; do
        fetch "$GEONAMES/$f" geonames
      done ;;
    ourairports)
      for f in airports runways navaids airport-frequencies countries regions; do
        fetch "$OURAIRPORTS/$f.csv" ourairports
      done ;;
    openflights)
      fetch "$OPENFLIGHTS/routes.dat" openflights
      fetch "$OPENFLIGHTS/airlines.dat" openflights ;;
    ucdp)
      fetch "$UCDP_GED" ucdp ;;
    *)
      echo "unknown dataset: $ds" >&2; exit 1 ;;
  esac
done
