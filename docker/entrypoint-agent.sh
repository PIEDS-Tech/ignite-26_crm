#!/usr/bin/env bash
# The agent refuses to start unless the API token's owner and the Gmail session
# agree (main.py lifespan). Fail early here with an explanation instead of
# letting uvicorn crash-loop on a RuntimeError.
set -euo pipefail

cd /app

if [ -z "${AGENT_API_TOKEN:-}" ]; then
  cat >&2 <<'MSG'
[entrypoint] AGENT_API_TOKEN is empty.

Issue one against the running CRM, then put it in .env and start this service:

    docker compose exec crm python core_django/manage.py issue_token \
        aarav@pilani.bits-pilani.ac.in --label docker

MSG
  exit 1
fi

echo "[entrypoint] waiting for the CRM at ${AGENT_API_BASE_URL:-unset}..."
for _ in $(seq 1 60); do
  python - <<'PY' && break
import os, sys, urllib.request
url = os.environ["AGENT_API_BASE_URL"].rstrip("/") + "/login/"
try:
    urllib.request.urlopen(url, timeout=2)
except Exception:
    sys.exit(1)
PY
  sleep 1
done

exec "$@"
