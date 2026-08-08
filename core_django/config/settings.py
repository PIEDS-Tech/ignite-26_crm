import sys
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent      # core_django/
REPO_ROOT = BASE_DIR.parent                            # ignite_crm/

# `shared` lives at the repo root and is imported by both apps.
sys.path.insert(0, str(REPO_ROOT))

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
)
environ.Env.read_env(REPO_ROOT / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-insecure-key-do-not-use-in-prod")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "crm",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves the vendored Basecoat files in production without nginx.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "crm" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --- database -------------------------------------------------------------
# In production this points at Supabase. Two things about that connection are
# load-bearing and easy to get wrong; see docs in README "Supabase".
#
#   1. Use the SESSION pooler (port 5432 on the pooler host). services/mailing.py
#      and services/assignment.py rely on SELECT ... FOR UPDATE, and the
#      transaction-mode pooler on 6543 breaks psycopg3's prepared statements.
#   2. sslmode=require belongs in the URL.

LOCAL_DATABASE_URL = "postgres://ignite:ignite@localhost:5432/ignite_crm"

#: Django's test runner CREATEs and DROPs its database. Once DATABASE_URL points
#: at Supabase -- as it will in everyone's .env -- an ordinary `pytest` would aim
#: that at the hosted instance. Tests therefore always run against local Docker
#: Postgres, regardless of the environment. Override with IGNITE_TEST_DATABASE_URL.
RUNNING_TESTS = "pytest" in sys.modules or "test" in sys.argv

if RUNNING_TESTS:
    DATABASE_URL = env("IGNITE_TEST_DATABASE_URL", default=LOCAL_DATABASE_URL)
else:
    DATABASE_URL = env("DATABASE_URL", default=LOCAL_DATABASE_URL)

DATABASES = {"default": env.db_url_config(DATABASE_URL)}

# Persistent connections are off by default: a pooler has a finite slot count,
# and holding one open per gunicorn worker per request cycle exhausts the free
# tier long before traffic does.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=0)

# Needed only if someone deliberately switches to the transaction-mode pooler
# (:6543), where server-side prepared statements and cursors are not safe.
if env.bool("DB_TRANSACTION_POOLER", default=False):
    DATABASES["default"].setdefault("OPTIONS", {})["prepare_threshold"] = None
    DISABLE_SERVER_SIDE_CURSORS = True

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# Basecoat CSS/JS is vendored once at the repo root and served by BOTH apps, so
# the Django CRM and the local FastAPI agent stay visually identical.
STATICFILES_DIRS = [REPO_ROOT / "shared" / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

# --- production hardening -------------------------------------------------
# Agents authenticate with a bearer token over the public internet, so TLS is
# not optional once DEBUG is off.
if not DEBUG:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        },
    }
    SECURE_SSL_REDIRECT = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    X_FRAME_OPTIONS = "DENY"
    CSRF_TRUSTED_ORIGINS = [
        f"https://{host}" for host in ALLOWED_HOSTS if host not in ("*",)
    ]
