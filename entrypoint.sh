#!/bin/sh
set -e

# Ensure persistent directories exist
mkdir -p /app/data /app/media /app/logs /app/backups /app/staticfiles

# A server other people reach runs with DEBUG off unless told otherwise.
export DJANGO_DEBUG="${DJANGO_DEBUG:-False}"
# Maxischermo and team links need their token: without it anybody on the
# internet could watch an auction or join one by typing a name.
export PUBLIC_TOKENS_REQUIRED="${PUBLIC_TOKENS_REQUIRED:-True}"

# The secret key signs sessions and password-reset links: it must be secret
# and stable across restarts. Without one from the environment, generate it
# once and keep it in the persistent data volume.
if [ -z "$DJANGO_SECRET_KEY" ]; then
    KEY_FILE=/app/data/.secret_key
    if [ ! -s "$KEY_FILE" ]; then
        python -c "import secrets; print(secrets.token_urlsafe(50))" > "$KEY_FILE"
        chmod 600 "$KEY_FILE"
        echo "[Entrypoint] Generata una nuova DJANGO_SECRET_KEY in $KEY_FILE"
    fi
    DJANGO_SECRET_KEY="$(cat "$KEY_FILE")"
    export DJANGO_SECRET_KEY
fi

# If PostgreSQL is configured, verify driver and wait for DB
if [ -n "$POSTGRES_DB" ]; then
    echo "[Entrypoint] Verifying PostgreSQL driver..."
    if ! python -c "import psycopg" 2>/dev/null && ! python -c "import psycopg2" 2>/dev/null; then
        echo "[Entrypoint] Driver PostgreSQL non trovato nell'immagine Docker in uso. Installazione automatica in corso..."
        pip install --no-cache-dir "psycopg[binary]>=3.1,<4.0"
    fi

    echo "[Entrypoint] Waiting for PostgreSQL database ($POSTGRES_HOST:${POSTGRES_PORT:-5432})..."
    python - <<END
import os, sys, time, socket

host = os.getenv("POSTGRES_HOST", "db")
port = int(os.getenv("POSTGRES_PORT", "5432"))
for i in range(45):
    try:
        with socket.create_connection((host, port), timeout=2):
            print(f"[Entrypoint] PostgreSQL is accepting connections on {host}:{port}")
            sys.exit(0)
    except OSError:
        time.sleep(1)
print(f"[Entrypoint] ERROR: Timeout waiting for PostgreSQL on {host}:{port}", file=sys.stderr)
sys.exit(1)
END
# If database volume is empty, initialize with repository db.sqlite3 if available
elif [ -n "$DJANGO_DB_PATH" ] && [ ! -f "$DJANGO_DB_PATH" ] && [ -f "/app/db.sqlite3" ]; then
    echo "[Entrypoint] Initializing persistent database at $DJANGO_DB_PATH from seed db.sqlite3..."
    cp /app/db.sqlite3 "$DJANGO_DB_PATH"
fi

# Run database migrations
echo "[Entrypoint] Applying database migrations..."
python manage.py migrate --noinput

# Demo data (leagues, teams, players; no accounts) only when asked for: a new
# installation for other people starts empty.
if [ "${FANTAMANAGER_LOAD_SEED:-0}" = "1" ] && [ -n "$POSTGRES_DB" ] && [ -f "/app/data/seed_data.json" ]; then
    python - <<END
import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "liveauction.settings")
django.setup()
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.exists():
    print("[Entrypoint] Database PostgreSQL vuoto rilevato. Importazione dati dimostrativi (leghe, rose)...")
    from django.core.management import call_command
    try:
        call_command("loaddata", "/app/data/seed_data.json")
        print("[Entrypoint] ✓ Dati iniziali importati con successo in PostgreSQL!")
    except Exception as e:
        print(f"[Entrypoint] Warning: impossibile caricare seed_data.json: {e}")
END
fi

# First superadmin. Never a default password: either the credentials come from
# DJANGO_SUPERUSER_USERNAME / DJANGO_SUPERUSER_PASSWORD (/ DJANGO_SUPERUSER_EMAIL),
# or a random password is generated and shown once, here in the container log.
python manage.py shell -c "
import os, secrets
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(is_superuser=True).exists():
    username = os.environ.get('DJANGO_SUPERUSER_USERNAME') or 'admin'
    password = os.environ.get('DJANGO_SUPERUSER_PASSWORD')
    email = os.environ.get('DJANGO_SUPERUSER_EMAIL', '')
    generated = not password
    if generated:
        password = secrets.token_urlsafe(12)
    User.objects.create_superuser(username, email, password)
    if generated:
        print('[Entrypoint] ============================================================')
        print(f'[Entrypoint] Creato il superadmin \\'{username}\\' con password: {password}')
        print('[Entrypoint] Cambiala al primo accesso. Non verra\\' mostrata di nuovo.')
        print('[Entrypoint] ============================================================')
    else:
        print(f'[Entrypoint] Creato il superadmin \\'{username}\\' da DJANGO_SUPERUSER_*')
"

# Collect static files for production serving
echo "[Entrypoint] Collecting static files..."
python manage.py collectstatic --noinput

echo "[Entrypoint] Starting FantaManager via Daphne ASGI server..."
exec "$@"
