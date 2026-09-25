"""WSGI entrypoint (kept for completeness; Channels uses asgi.py)."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "liveauction.settings")

application = get_wsgi_application()
