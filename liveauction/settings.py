"""
Django settings for the Live Auction Server project.

MVP-oriented: SQLite, in-memory Channels layer (no Redis needed), and a
single-process ASGI server (daphne via `runserver`). Suitable for LAN use.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from a .env file if present.
load_dotenv(BASE_DIR / ".env")

# --- Core security settings -------------------------------------------------
SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "dev-insecure-change-me-before-anything-public",
)

DEBUG = os.getenv("DJANGO_DEBUG", "True").lower() in ("1", "true", "yes")

# A deployment with DEBUG off is one other people reach: it must not run on a
# key anybody can read. These placeholders are in this (public) repository.
_PUBLIC_PLACEHOLDER_KEYS = {
    "",
    "dev-insecure-change-me-before-anything-public",
    "fantamanager-secret-key-production-change-me",
}
if not DEBUG and SECRET_KEY in _PUBLIC_PLACEHOLDER_KEYS:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY non impostata, o uguale a un valore pubblico del "
        "repository. Con DEBUG spento serve una chiave vera, per esempio: "
        'python -c "import secrets; print(secrets.token_urlsafe(50))"'
    )

# Set by the desktop launcher (run_app.py): one user, on the machine that runs
# the server. What acts on that machine exists only there: the "Esci" button,
# the first-login superadmin.
DESKTOP_APP = os.getenv("FANTAMANAGER_DESKTOP", "").lower() in ("1", "true", "yes")

# For LAN use we accept any host by default. Lock this down in production.
ALLOWED_HOSTS = [h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()]
# The container healthcheck asks http://localhost:8000/healthz/ from inside:
# a host list locked to the public domain must still let it in.
if "*" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS += [h for h in ("localhost", "127.0.0.1") if h not in ALLOWED_HOSTS]

# Trust the local network origins and DDNS domains for CSRF over WebSocket/forms if needed.
_csrf = os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").strip()
if _csrf:
    CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf.split(",") if o.strip()]
else:
    CSRF_TRUSTED_ORIGINS = [
        "https://*.synology.me",
        "http://*.synology.me",
        "https://*.direct.quickconnect.to",
        "http://*.direct.quickconnect.to",
        "https://*.local",
        "http://*.local",
        "http://localhost:8000",
        "http://localhost:8088",
        "http://127.0.0.1:8000",
        "http://127.0.0.1:8088",
    ]

# Trust TLS-terminating reverse proxy (e.g. Synology Reverse Proxy, Nginx, Caddy)
if os.getenv("DJANGO_BEHIND_PROXY", "True").lower() in ("1", "true", "yes"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# --- Application definition -------------------------------------------------
INSTALLED_APPS = [
    # `daphne` must come first so it overrides the `runserver` command and
    # serves the project through the ASGI/Channels stack during development.
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "auctions",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Uploads over FM_MAX_REQUEST_BYTES are refused before anything parses them.
    "auctions.middleware.UploadSizeLimit",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Device routing: smartphone -> app, PC -> web
    "auctions.middleware.DeviceRoutingMiddleware",
    # Keeps debug tracebacks off the public tunnel (the desktop app runs with
    # DEBUG on so one process can also serve static/media). No-op locally.
    "auctions.middleware.RemoteErrorShield",
    # Plain-text refusals (403, 405...) get a real page when a browser asks.
    "auctions.middleware.FriendlyErrorPages",
]

# WhiteNoise serves static files efficiently when running behind a single ASGI
# process in production. It is optional: only wired in when the package is
# installed, so local/dev installs without it keep working unchanged.
try:
    import whitenoise  # noqa: F401
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
    _HAS_WHITENOISE = True
except ImportError:
    _HAS_WHITENOISE = False

ROOT_URLCONF = "liveauction.urls"

_FROZEN_TEMPLATE_DIRS = []
if getattr(sys, "frozen", False):
    _bundle = Path(getattr(sys, "_MEIPASS", BASE_DIR))
    for _candidate in (
        _bundle / "auctions" / "templates",
        _bundle.parent / "Resources" / "auctions" / "templates",
    ):
        if _candidate.is_dir():
            _FROZEN_TEMPLATE_DIRS.append(_candidate)

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": _FROZEN_TEMPLATE_DIRS,
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "auctions.context_processors.app_version",
            ],
        },
    },
]

# WSGI is kept for completeness, but Channels drives the app through ASGI.
WSGI_APPLICATION = "liveauction.wsgi.application"
ASGI_APPLICATION = "liveauction.asgi.application"

# --- Channels ---------------------------------------------------------------
# In-memory layer keeps the MVP dependency-free (no Redis) and works within a
# single process — exactly how we run on a LAN host. Set REDIS_URL (and install
# channels-redis) to scale across multiple workers/processes in production.
_REDIS_URL = os.getenv("REDIS_URL", "").strip()
if _REDIS_URL:
    try:
        import channels_redis  # noqa: F401
        CHANNEL_LAYERS = {
            "default": {
                "BACKEND": "channels_redis.core.RedisChannelLayer",
                "CONFIG": {"hosts": [_REDIS_URL]},
            },
        }
    except ImportError:
        raise RuntimeError(
            "REDIS_URL is set but 'channels-redis' is not installed. "
            "Run: pip install channels-redis"
        )
else:
    CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
    }

# --- Database ---------------------------------------------------------------
# SQLite by default (zero-config). Set POSTGRES_DB (+ host/user/password) to use
# PostgreSQL in production — requires the 'psycopg' driver at runtime.
if os.getenv("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB"),
            "USER": os.getenv("POSTGRES_USER", "postgres"),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
            "HOST": os.getenv("POSTGRES_HOST", "127.0.0.1"),
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": int(os.getenv("DJANGO_DB_CONN_MAX_AGE", "60")),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.getenv("DJANGO_DB_PATH") or (BASE_DIR / "db.sqlite3"),
            # An auction night is concurrent by nature: Daphne serves the
            # console, every manager's page and the websocket consumers from
            # the same process, so a write (rebuilding a 500-player queue, for
            # instance) overlaps with plenty of reads. Default SQLite settings
            # answer that with "database is locked".
            #   WAL            -> readers never block the writer, and vice versa
            #   busy timeout   -> wait for the lock instead of failing at once
            #   IMMEDIATE      -> take the write lock at BEGIN, so a read-then-
            #                     write transaction cannot deadlock on upgrade
            "OPTIONS": {
                "timeout": 30,
                "transaction_mode": "IMMEDIATE",
                "init_command": (
                    "PRAGMA journal_mode=WAL;"
                    "PRAGMA synchronous=NORMAL;"
                    "PRAGMA busy_timeout=30000;"
                ),
            },
        }
    }

# --- Password validation ----------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Internationalization ---------------------------------------------------
LANGUAGE_CODE = "it-it"
# The database stores UTC (USE_TZ); this is the zone the admins live in: the
# one a typed "20:00" means and the one pages show times in. With UTC here a
# market session set for 20:00 opened at 22:00 Italian (summer) time.
TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "Europe/Rome")
USE_I18N = True
USE_TZ = True

# --- Static files -----------------------------------------------------------
STATIC_URL = "static/"
STATICFILES_DIRS = list(dict.fromkeys(
    _dir for _dir in (
        BASE_DIR / "static",
        Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "static",
        Path(getattr(sys, "_MEIPASS", BASE_DIR)).parent / "Resources" / "static",
    ) if _dir.is_dir()
))
# Destination for `collectstatic` in production (served by WhiteNoise/proxy).
STATIC_ROOT = os.getenv("DJANGO_STATIC_ROOT") or (BASE_DIR / "staticfiles")
if _HAS_WHITENOISE:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }

# --- Media files (logo uploads) ---------------------------------------------
# MEDIA_ROOT is overridable so the packaged (PyInstaller) app can write uploads
# to a user-writable data folder instead of the read-only bundle.
MEDIA_URL  = "media/"
# Largest request body accepted (uploads included): 25 MB by default.
FM_MAX_REQUEST_BYTES = int(os.getenv("FM_MAX_REQUEST_BYTES", str(25 * 1024 * 1024)))
MEDIA_ROOT = Path(os.getenv("FANTAMANAGER_MEDIA_ROOT") or (BASE_DIR / "media"))

# PostgreSQL dumps (./backups in docker-compose, shared with its backup service).
BACKUP_DIR = Path(os.getenv("FANTAMANAGER_BACKUP_DIR") or (BASE_DIR / "backups"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Auth redirects ---------------------------------------------------------
LOGIN_URL = "admin:login"

# --- Auction MVP tunables ---------------------------------------------------
# Simple anti-spam: minimum interval (ms) between two bids from one participant.
BID_MIN_INTERVAL_MS = int(os.getenv("BID_MIN_INTERVAL_MS", "300"))
# How often (seconds) the server pushes a timer sync over WebSocket.
TIMER_SYNC_INTERVAL_SECONDS = float(os.getenv("TIMER_SYNC_INTERVAL_SECONDS", "2"))
# NB: the pause between two lots used to live here as a global constant. It is
# now per-auction (``Auction.cycle_break_seconds``, same default of 4s), set from
# the console under Impostazioni, so a slow league and a fast one can differ.

# When True, public surfaces (read-only TV screen, bidder join) must carry the
# matching unguessable token. Defaults to off for trusted LAN use; turn on for
# internet-facing deployments. See auctions.views screen()/bid_page()/join().
PUBLIC_TOKENS_REQUIRED = os.getenv("PUBLIC_TOKENS_REQUIRED", "False").lower() in ("1", "true", "yes")

# --- Dati dei calciatori -----------------------------------------------------
# Statistiche di stagione applicate da sole a ogni import del listone: un file
# che l'admin del server possiede o ha in licenza (l'app non ne include uno).
FANTAMANAGER_STATS_FILE = os.getenv("FANTAMANAGER_STATS_FILE", "").strip()
FANTAMANAGER_STATS_SEASON = os.getenv("FANTAMANAGER_STATS_SEASON", "").strip()

# --- Outgoing email -----------------------------------------------------------
# The provider is normally set from the console (Impostazioni → Posta). These
# env vars are the fallback used while that page is switched off.
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() in ("1", "true", "yes")
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "False").lower() in ("1", "true", "yes")
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "15"))
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "FantaManager <noreply@fantamanager.local>")

# --- Logging ----------------------------------------------------------------
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE_PATH = LOGS_DIR / "fantamanager.log"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "simple": {
            "format": "%(levelname)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
        "file": {
            "level": "DEBUG",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_FILE_PATH),
            "maxBytes": 5 * 1024 * 1024,  # 5 MB
            "backupCount": 5,
            "formatter": "standard",
            "encoding": "utf-8",
        },
    },
    "loggers": {
        "auctions": {
            "handlers": ["console", "file"],
            "level": "DEBUG",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console", "file"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

# --- Production hardening ----------------------------------------------------
# Only engaged when DEBUG is off, so development stays frictionless. Each toggle
# can still be overridden via env for proxies/edge setups.
if not DEBUG:
    _is_true = lambda v, d="True": os.getenv(v, d).lower() in ("1", "true", "yes")

    SECURE_SSL_REDIRECT     = _is_true("DJANGO_SECURE_SSL_REDIRECT", "False")
    SESSION_COOKIE_SECURE   = _is_true("DJANGO_SESSION_COOKIE_SECURE", "False")
    CSRF_COOKIE_SECURE      = _is_true("DJANGO_CSRF_COOKIE_SECURE", "False")
    SECURE_HSTS_SECONDS     = int(os.getenv("DJANGO_SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = _is_true("DJANGO_HSTS_INCLUDE_SUBDOMAINS", "False")
    SECURE_HSTS_PRELOAD     = _is_true("DJANGO_HSTS_PRELOAD", "False")
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS         = "DENY"

    # Behind a TLS-terminating proxy (Synology/nginx/Heroku/Render), trust its header.
    if _is_true("DJANGO_BEHIND_PROXY", "True"):
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


    # Fail fast if the install forgot to set a real secret in production.
    if SECRET_KEY == "dev-insecure-change-me-before-anything-public":
        import warnings
        warnings.warn(
            "DJANGO_SECRET_KEY is unset in a non-DEBUG environment — set a real "
            "random secret before exposing this server.", RuntimeWarning,
        )
