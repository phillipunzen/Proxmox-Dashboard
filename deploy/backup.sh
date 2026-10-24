#!/usr/bin/env bash
# Backs up the dashboard itself: users, sessions, audit log, alert rules, API keys (SQLite),
# the secret key, the SSH key pair, job logs, .env and certs. Optionally the history (TSDB).
#
#   bash deploy/backup.sh                 # -> backups/pve-dashboard-<timestamp>.tar.gz
#   bash deploy/backup.sh --with-history  # + VictoriaMetrics snapshot (can be large)
#
# Run from the project directory while the stack is running. The archive contains secrets
# (TOTP key, SSH private key, API tokens in .env) - it is created with mode 600; store it safely.
set -euo pipefail
cd "$(dirname "$0")/.."

WITH_HISTORY=false
[ "${1:-}" = "--with-history" ] && WITH_HISTORY=true

STAMP=$(date +%Y%m%d-%H%M%S)
OUT_DIR="$PWD/backups"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT_DIR" "$WORK/app-data"
umask 077

PROJECT=$(docker compose config --format json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])' 2>/dev/null || basename "$PWD")

echo "1/4 Konsistente SQLite-Kopie (Online-Backup, keine Downtime) …"
docker compose exec -T dashboard python - <<'PY'
import sqlite3
src = sqlite3.connect("/data/dashboard.db")
dst = sqlite3.connect("/data/dashboard-backup.db")
src.backup(dst)
dst.close(); src.close()
PY

echo "2/4 Daten-Volume sichern (Schlüssel, SSH, Job-Logs) …"
docker run --rm -v "${PROJECT}_app-data:/data:ro" -v "$WORK/app-data:/out" alpine:3.20 \
  sh -c 'cd /data && tar cf - --exclude=./dashboard.db --exclude=./dashboard.db-wal \
         --exclude=./dashboard.db-shm . | tar xf - -C /out'
mv "$WORK/app-data/dashboard-backup.db" "$WORK/app-data/dashboard.db"
docker compose exec -T dashboard rm -f /data/dashboard-backup.db

echo "3/4 Konfiguration (.env, certs) …"
cp .env "$WORK/env" 2>/dev/null || echo "   (keine .env gefunden)"
[ -d certs ] && cp -r certs "$WORK/certs"

if $WITH_HISTORY; then
  echo "4/4 VictoriaMetrics-Snapshot …"
  SNAP=$(docker compose exec -T dashboard python -c \
    'import json,urllib.request; print(json.load(urllib.request.urlopen("http://victoriametrics:8428/snapshot/create"))["snapshot"])')
  docker run --rm -v "${PROJECT}_vm-data:/storage:ro" -v "$WORK:/out" alpine:3.20 \
    sh -c "tar czhf /out/vm-snapshot.tar.gz -C /storage/snapshots/$SNAP ."  # -h: snapshot = symlinks
  docker compose exec -T dashboard python -c \
    "import urllib.request; urllib.request.urlopen('http://victoriametrics:8428/snapshot/delete?snapshot=$SNAP')"
else
  echo "4/4 Historie übersprungen (--with-history für VictoriaMetrics)"
fi

ARCHIVE="$OUT_DIR/pve-dashboard-$STAMP.tar.gz"
tar czf "$ARCHIVE" -C "$WORK" .
chmod 600 "$ARCHIVE"
echo "Fertig: $ARCHIVE ($(du -h --apparent-size "$ARCHIVE" | cut -f1))"
