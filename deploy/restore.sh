#!/usr/bin/env bash
# Restores a backup created by deploy/backup.sh.
#
#   bash deploy/restore.sh backups/pve-dashboard-20261024-120000.tar.gz
#
# Stops the dashboard, replaces the data volume (and the history if the archive contains it),
# restores .env/certs only if they don't exist yet (never overwrites your current config
# silently), then starts the stack again.
set -euo pipefail
cd "$(dirname "$0")/.."

ARCHIVE="${1:?Pfad zum Backup-Archiv angeben}"
[ -f "$ARCHIVE" ] || { echo "Nicht gefunden: $ARCHIVE" >&2; exit 1; }
ARCHIVE=$(readlink -f "$ARCHIVE")

PROJECT=$(docker compose config --format json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])' 2>/dev/null || basename "$PWD")
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
tar xzf "$ARCHIVE" -C "$WORK"

read -r -p "Dashboard stoppen und Daten aus $(basename "$ARCHIVE") wiederherstellen? [j/N] " ok
[ "$ok" = "j" ] || [ "$ok" = "J" ] || exit 1

docker compose stop dashboard
docker volume create "${PROJECT}_app-data" >/dev/null
docker run --rm -v "${PROJECT}_app-data:/data" -v "$WORK/app-data:/in:ro" alpine:3.20 \
  sh -c 'rm -rf /data/* /data/.[!.]* 2>/dev/null; cp -a /in/. /data/ && chown -R 10001:10001 /data'

if [ -f "$WORK/vm-snapshot.tar.gz" ]; then
  echo "Historie wiederherstellen …"
  docker compose stop victoriametrics
  docker run --rm -v "${PROJECT}_vm-data:/storage" -v "$WORK:/in:ro" alpine:3.20 \
    sh -c 'rm -rf /storage/* && tar xzf /in/vm-snapshot.tar.gz -C /storage'
fi

[ -f .env ] || { [ -f "$WORK/env" ] && cp "$WORK/env" .env && chmod 600 .env && echo ".env wiederhergestellt"; }
[ -d certs ] || { [ -d "$WORK/certs" ] && cp -r "$WORK/certs" certs; }

docker compose up -d
echo "Wiederhergestellt. Prüfen mit: docker compose logs -f dashboard"
