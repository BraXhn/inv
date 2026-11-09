#!/bin/sh
# Startar som root bara för att rätta ägarskap på /data, kör sedan appen som PUID:PGID (Unraid: 99:100).
set -e
PUID="${PUID:-99}"
PGID="${PGID:-100}"
mkdir -p "$DATA_DIR"
chown -R "$PUID:$PGID" "$DATA_DIR"

# En worker är avsiktligt: SQLite, rate limiting i minnet och backup-tråden förutsätter en process.
# Trådar ger samtidighet, mer än nog för en verkstad.
exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups \
  gunicorn --chdir /app --workers 1 --threads 8 --timeout 60 \
  --bind "0.0.0.0:${PORT:-8080}" --access-logfile - --error-logfile - \
  app:app
