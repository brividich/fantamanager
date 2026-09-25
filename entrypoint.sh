#!/bin/sh
set -e

# Ensure persistent directories exist
mkdir -p /app/data /app/media /app/logs /app/backups /app/staticfiles

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

# If PostgreSQL is empty and seed_data.json exists, auto-load initial data (leagues, teams, users)
if [ -n "$POSTGRES_DB" ] && [ -f "/app/data/seed_data.json" ]; then
    python - <<END
import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "liveauction.settings")
django.setup()
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.exists():
    print("[Entrypoint] Database PostgreSQL vuoto rilevato. Importazione dati iniziali (leghe, rose, admin)...")
    from django.core.management import call_command
    try:
        call_command("loaddata", "/app/data/seed_data.json")
        print("[Entrypoint] ✓ Dati iniziali importati con successo in PostgreSQL!")
    except Exception as e:
        print(f"[Entrypoint] Warning: impossibile caricare seed_data.json: {e}")
END
fi

# Ensure default admin exists if database has no superuser
python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(is_superuser=True).exists():
    User.objects.create_superuser('admin', 'admin@fantamanager.local', 'admin')
    print('[Entrypoint] Creato superuser di default: admin / admin')
"

# Collect static files for production serving
echo "[Entrypoint] Collecting static files..."
python manage.py collectstatic --noinput

echo "[Entrypoint] Starting FantaManager via Daphne ASGI server..."
exec "$@"
