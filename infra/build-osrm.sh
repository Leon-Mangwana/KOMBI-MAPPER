#!/usr/bin/env bash
# Download the Zimbabwe OSM extract and prepare it for osrm-routed.
#
# This is the one-off (and on refresh, repeated) preparation that README.md
# walks through by hand: download, osrm-extract, osrm-partition, osrm-customize.
# Every step runs through `docker compose run`, so it uses the exact OSRM image
# pinned in docker-compose.yml and writes into the ./data directory the server
# reads. A dataset prepared by any other OSRM version is refused at startup.
#
# Usage, from this directory (Git Bash works on Windows):
#   ./build-osrm.sh            # download if missing, then prepare
#   ./build-osrm.sh --refresh  # re-download first, then prepare
#   ./build-osrm.sh --up       # ...and start the osrm service afterwards
#
# Budget several minutes and at least 6 GB of memory for Docker. Expect a few
# GB in ./data when it is done; the directory is gitignored.
set -euo pipefail

cd "$(dirname "$0")"

EXTRACT_URL="https://download.geofabrik.de/africa/zimbabwe-latest.osm.pbf"
PBF="data/zimbabwe-latest.osm.pbf"
OSRM="/data/zimbabwe-latest.osrm" # the path inside the container

refresh=0
up=0
for arg in "$@"; do
  case "$arg" in
    --refresh) refresh=1 ;;
    --up) up=1 ;;
    -h | --help)
      sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose is not available; start Docker Desktop first." >&2
  exit 1
fi

mkdir -p data

if [[ $refresh -eq 1 || ! -f "$PBF" ]]; then
  echo "==> Downloading $EXTRACT_URL"
  curl -L --fail --progress-bar -o "$PBF" "$EXTRACT_URL"
  # Geofabrik publishes an MD5 beside every extract; a truncated download is
  # the usual reason osrm-extract dies with an unhelpful parse error.
  curl -L --fail --silent -o "$PBF.md5" "$EXTRACT_URL.md5"
  (cd data && md5sum -c "$(basename "$PBF").md5")
else
  echo "==> Using existing $PBF (pass --refresh to re-download)"
fi

# The server must not hold the dataset open while it is rewritten.
docker compose stop osrm >/dev/null 2>&1 || true

echo "==> osrm-extract (car profile; the slow step)"
docker compose run --rm osrm osrm-extract -p /opt/car.lua "/${PBF}"

echo "==> osrm-partition"
docker compose run --rm osrm osrm-partition "$OSRM"

echo "==> osrm-customize"
docker compose run --rm osrm osrm-customize "$OSRM"

echo "==> Done. Dataset in ./data:"
du -sh data

if [[ $up -eq 1 ]]; then
  docker compose up -d osrm
  echo "==> osrm started; follow with: docker compose logs -f osrm"
else
  echo "Start the server with: docker compose up -d osrm"
fi
