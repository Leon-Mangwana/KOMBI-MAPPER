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

# Git Bash on Windows rewrites arguments that look like POSIX paths into
# Windows ones, so `/opt/car.lua` reaches the container as
# `C:/Program Files/Git/opt/car.lua`. These are container paths; leave them.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

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

checksum_ok() {
  (cd data && md5sum -c --quiet "$(basename "$PBF").md5" >/dev/null 2>&1)
}

fetch_checksum() {
  # Fetched once, alongside the download it describes. Geofabrik rebuilds the
  # extract daily, so fetching it again later would describe a different file
  # and condemn a perfectly good one on disk.
  curl -L --fail --silent --retry 5 --retry-all-errors -o "$PBF.md5" "$EXTRACT_URL.md5"
}

if [[ $refresh -eq 1 ]]; then
  rm -f "$PBF" "$PBF.md5"
fi

if [[ -f "$PBF" && -f "$PBF.md5" ]] && checksum_ok; then
  echo "==> Using existing $PBF (matches its checksum; pass --refresh for today's extract)"
else
  if [[ ! -f "$PBF.md5" || ! -f "$PBF" ]]; then
    fetch_checksum
  fi
  echo "==> Downloading $EXTRACT_URL"
  # -C - resumes a partial file. A download that still fails after the retries
  # is left in place: re-running resumes it rather than starting the 180 MB over.
  if ! curl -L --fail --progress-bar -C - --retry 5 --retry-all-errors --retry-delay 5     -o "$PBF" "$EXTRACT_URL"; then
    echo "download interrupted at $(stat -c %s "$PBF" 2>/dev/null || echo 0) bytes; run this again to resume" >&2
    exit 1
  fi
  if ! checksum_ok; then
    # The download completed but does not match: the extract was rebuilt
    # between the checksum fetch and now. Start over once with a fresh pair.
    echo "==> Downloaded file does not match its checksum; the extract was rebuilt. Starting over."
    rm -f "$PBF" "$PBF.md5"
    fetch_checksum
    curl -L --fail --progress-bar --retry 5 --retry-all-errors --retry-delay 5 -o "$PBF" "$EXTRACT_URL"
    checksum_ok || {
      echo "checksum still does not match; refusing to build from a bad extract" >&2
      exit 1
    }
  fi
fi

# The server must not hold the dataset open while it is rewritten.
docker compose stop osrm >/dev/null 2>&1 || true

echo "==> osrm-extract (car profile; the slow step)"
if ! docker compose run --rm osrm osrm-extract -p /opt/car.lua "/${PBF}"; then
  cat >&2 <<MSG

osrm-extract failed. If the log above says "Cannot allocate memory", the Docker
VM is too small for the whole-country extract: it needs roughly 4 GB free while
it runs. Docker currently has $(docker info --format '{{.MemTotal}}' | awk '{printf "%.1f GB", $1/1024/1024/1024}') in total, shared with every other
running container (docker stats --no-stream). Either stop the others for the
duration, or raise the limit (Docker Desktop > Settings > Resources, or
%UserProfile%\.wslconfig on Windows) and run this again; it resumes from the
downloaded extract.
MSG
  exit 1
fi

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
