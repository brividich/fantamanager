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


def privacy_notice(request):
    """L'avviso in cima alle pagine per chi ha un account: testi legali da
    accettare (account nati prima del consenso) o email da confermare."""
    from .services import privacy

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not privacy.legal_required():
        return {}
    notice = {}
    if privacy.acceptance_state(user) != "ok":
        notice["legal"] = True
    if privacy.email_unverified(user):
        from .services import mail

        notice["email"] = user.email
        notice["mail_ready"] = mail.is_ready()
    return {"privacy_notice": notice} if notice else {}
