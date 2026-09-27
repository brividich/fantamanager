"""Template context available on every page, app-wide."""
from django.conf import settings

from liveauction import __version__


def app_version(request):
    """Exposes {{ app_version }} everywhere — see liveauction/__init__.py —
    and {{ desktop_app }}: only the desktop build may offer "Chiudi"."""
    return {"app_version": __version__, "desktop_app": settings.DESKTOP_APP}
