#!/usr/bin/env bash
# Bring the CRM up: wait for Postgres, build the schema, prove the constraints
# actually landed, then serve.
#
# Migrating and seeding are decided by WHERE the database is, not by a flag
# somebody has to remember. Against a throwaway local Postgres both are what you
# want on every boot; against one database the whole team shares, a laptop doing
# either on startup is a way to damage everyone's data. Set RUN_MIGRATIONS or
# SEED_DEV explicitly to override.
set -euo pipefail

cd /app/core_django

DB_HOST=$(python - <<'PY'
import os, urllib.parse as u
print((u.urlsplit(os.environ.get("DATABASE_URL", "")).hostname or "localhost").lower())
PY
)

case "$DB_HOST" in
  localhost|127.0.0.1|::1|postgres) DB_IS_LOCAL=true ;;
  *)                                DB_IS_LOCAL=false ;;
esac

# Empty counts as unset here, which is what compose passes for an unset var.
RUN_MIGRATIONS="${RUN_MIGRATIONS:-$DB_IS_LOCAL}"
SEED_DEV="${SEED_DEV:-$DB_IS_LOCAL}"

echo "[entrypoint] database: ${DB_HOST} (local=${DB_IS_LOCAL})"

echo "[entrypoint] waiting for postgres..."
for _ in $(seq 1 60); do
  pg_isready -d "${DATABASE_URL:-}" -q && break
  sleep 1
done
if ! pg_isready -d "${DATABASE_URL:-}" -q; then
  echo "[entrypoint] cannot reach postgres at ${DB_HOST}." >&2
  if [ "$DB_IS_LOCAL" = "true" ]; then
    echo "[entrypoint] the local Postgres container is behind a profile. Add" >&2
    echo "[entrypoint]   COMPOSE_PROFILES=localdb" >&2
    echo "[entrypoint] to your .env, or run: docker compose --profile localdb up" >&2
  fi
  exit 1
fi

if [ "$RUN_MIGRATIONS" = "true" ]; then
  echo "[entrypoint] migrate"
  python manage.py migrate --noinput
else
  # Not an error: on a shared database exactly one machine should own the
  # schema. check_db below still fails the boot if that machine hasn't run yet.
  echo "[entrypoint] migrate SKIPPED (shared database; set RUN_MIGRATIONS=true to apply)"
fi

# The whole no-double-mail guarantee is one unique index. Failing the boot is
# the correct response to it being absent -- see README section 3.1. This runs
# unconditionally, which is what makes skipping migrations safe.
echo "[entrypoint] check_db"
python manage.py check_db

if [ "$SEED_DEV" = "true" ]; then
  echo "[entrypoint] seed_dev"
  python manage.py seed_dev
else
  echo "[entrypoint] seed_dev SKIPPED (not a local database)"
fi

exec "$@"
