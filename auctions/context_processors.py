"""Template context available on every page, app-wide."""
from django.conf import settings
from django.db import connection

from liveauction import __version__


def app_version(request):
    """Exposes {{ app_version }} everywhere — see liveauction/__init__.py —
    {{ desktop_app }}: only the desktop build may offer "Chiudi" — and
    {{ db_engine }}: PostgreSQL on the server, SQLite on the desktop."""
    return {
        "app_version": __version__,
        "desktop_app": settings.DESKTOP_APP,
        "db_engine": "PostgreSQL" if connection.vendor == "postgresql" else "SQLite",
    }
