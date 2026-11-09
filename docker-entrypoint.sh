#!/bin/sh
set -e

DB_PATH="${SQLITE_PATH:-/app/data/db.sqlite3}"
mkdir -p "$(dirname "$DB_PATH")" "${MEDIA_ROOT:-/app/media}"

# Seed the database from the image on first run only; never overwrite live data.
if [ ! -f "$DB_PATH" ]; then
    echo "No database at $DB_PATH, seeding from /app/seed/db.sqlite3"
    cp /app/seed/db.sqlite3 "$DB_PATH"
fi

python manage.py migrate --noinput

exec "$@"
