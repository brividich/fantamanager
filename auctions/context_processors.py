"""Template context available on every page, app-wide."""
from liveauction import __version__


def app_version(request):
    """Exposes {{ app_version }} everywhere — see liveauction/__init__.py."""
    return {"app_version": __version__}
