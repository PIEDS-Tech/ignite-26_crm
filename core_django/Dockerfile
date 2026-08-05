FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Only the server's dependencies -- the Gmail/agent libraries never run here.
COPY requirements.txt .
RUN pip install --no-cache-dir \
        "Django>=5.1,<6.0" "django-environ>=0.11" "psycopg[binary]>=3.2" \
        gunicorn whitenoise

# `shared/` sits at the repo root and is imported by settings.py, so build
# from the repo root: docker build -f core_django/Dockerfile .
COPY shared/ ./shared/
COPY core_django/ ./core_django/

WORKDIR /app/core_django
RUN python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
