"""Pytest bootstrap.

The actual safety rule -- never run tests against the hosted database -- lives
in core_django/config/settings.py, because pytest-django calls django.setup()
before root conftest files are imported, so an override here would be too late.

This file only reports which database settings.py chose, so a run that somehow
pointed at production would be obvious in the header rather than silent.
"""


def pytest_report_header(config):
    from django.conf import settings

    db = settings.DATABASES["default"]
    where = f"{db.get('HOST') or 'localhost'}:{db.get('PORT') or 5432}/{db.get('NAME')}"

    if not settings.RUNNING_TESTS:          # pragma: no cover - should be impossible
        return f"ignite: WARNING -- test guard did not engage, using {where}"
    return f"ignite: tests pinned to {where} (never the hosted database)"
