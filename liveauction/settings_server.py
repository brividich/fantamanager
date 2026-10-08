"""Settings for a hosted server on the internet (a public service, not a NAS
on the home LAN or the desktop app).

    DJANGO_SETTINGS_MODULE=liveauction.settings_server

``settings`` stays permissive on purpose: on a LAN everybody in the room
plays, and the desktop app needs DEBUG to serve files from one process.
This profile starts from it and refuses to boot with what is only safe at
home: DEBUG on, any host, the repository's example database password,
anonymous teams on /join/. It also turns off what only the desktop app
needs (the cloudflared tunnel, the first-login superadmin) and the scraping
of third-party standings pages, and logs to stdout for the container.
"""
import os

os.environ.setdefault("DJANGO_DEBUG", "False")

from django.core.exceptions import ImproperlyConfigured  # noqa: E402

from .settings import *  # noqa: E402,F401,F403
from .settings import ALLOWED_HOSTS, DATABASES, DEBUG, DESKTOP_APP, LOGGING  # noqa: E402

_problems = []
if DEBUG:
    _problems.append("DJANGO_DEBUG deve essere False")
if DESKTOP_APP:
    _problems.append("FANTAMANAGER_DESKTOP non va impostata su un server")
if not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS:
    _problems.append("DJANGO_ALLOWED_HOSTS deve elencare i domini del servizio (niente '*')")
_db = DATABASES["default"]
if "postgresql" not in _db["ENGINE"]:
    _problems.append("serve PostgreSQL (POSTGRES_DB, POSTGRES_HOST, POSTGRES_USER, POSTGRES_PASSWORD)")
elif _db.get("PASSWORD") in ("", "fantamanager_secret_pass"):
    _problems.append("POSTGRES_PASSWORD vuota o uguale a quella d'esempio del repository")
if _problems:
    raise ImproperlyConfigured("Profilo server: " + "; ".join(_problems) + ".")

# Only invited teams: no anonymous team from a typed name, no public lists.
PUBLIC_TOKENS_REQUIRED = True
FM_REMOTE_TUNNEL = False
FM_REMOTE_STANDINGS = os.getenv("FM_REMOTE_STANDINGS", "False").lower() in ("1", "true", "yes")

# HTTPS only (TLS ends at the proxy in front, which must be declared).
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = os.getenv("DJANGO_SECURE_SSL_REDIRECT", "True").lower() in ("1", "true", "yes")
SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]        # the container's own healthcheck speaks plain HTTP
SESSION_COOKIE_HTTPONLY = True

# Shared state for more than one process: Redis for the throttle counters and
# the auction rooms (channels-redis is already wired by REDIS_URL).
_REDIS_URL = os.getenv("REDIS_URL", "").strip()
if _REDIS_URL:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": _REDIS_URL}}

# Logs go to stdout: the container runtime collects them.
LOGGING = {**LOGGING, "loggers": {
    name: {**cfg, "handlers": ["console"]} for name, cfg in LOGGING["loggers"].items()
}}
LOGGING["handlers"] = {k: v for k, v in LOGGING["handlers"].items() if k != "file"}
