# One image, both apps.
#
# core_django and local_agent share `shared/` and are pinned to the same Python
# and the same dependency set, so building them twice buys nothing. The image
# ships both; docker-compose picks which one a container runs by choosing an
# entrypoint. The security split the README describes is enforced by which
# environment variables each container gets, not by which files it has:
# the agent container is never given DATABASE_URL.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# postgresql-client gives the entrypoint `pg_isready`, so the CRM waits for a
# usable database rather than crash-looping until compose's restart policy
# happens to line up.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY core_django/ ./core_django/
COPY local_agent/ ./local_agent/
COPY conftest.py pytest.ini ./
COPY docker/ ./docker/
RUN chmod +x ./docker/*.sh

# Baked into the image so a container never needs a writable static dir.
# DJANGO_DEBUG is forced off here only so collectstatic picks the hashed
# manifest storage; it does not affect runtime, which reads the real env.
RUN DJANGO_DEBUG=False python core_django/manage.py collectstatic --noinput

EXPOSE 8000 8111

CMD ["./docker/entrypoint-crm.sh"]
