"""Template context available on every page, app-wide."""
import os
from datetime import datetime

from django.conf import settings
from django.db import connection

from liveauction import __version__


def build_info():
    """Il commit e la data della build (``FM_BUILD``, ``FM_BUILD_DATE``, scritti
    nell'immagine Docker dal workflow di pubblicazione): ``("abc1234",
    "12/11/2026")``, vuoti fuori dall'immagine (sviluppo, app del PC)."""
    sha = (os.environ.get("FM_BUILD") or "").strip()[:7]
    raw = (os.environ.get("FM_BUILD_DATE") or "").strip()
    date = ""
    if raw:
        try:
            date = datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%d/%m/%Y")
        except ValueError:
            date = ""
    return sha, date


def app_version(request):
    """Exposes {{ app_version }} everywhere — see liveauction/__init__.py —
    {{ app_build }} / {{ app_build_date }}: the commit of the Docker image —
    {{ desktop_app }}: only the desktop build may offer "Chiudi" — and
    {{ db_engine }}: PostgreSQL on the server, SQLite on the desktop."""
    sha, date = build_info()
    return {
        "app_version": __version__,
        "app_build": sha,
        "app_build_date": date,
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
