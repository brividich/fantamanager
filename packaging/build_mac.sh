#!/usr/bin/env bash
#
# build_mac.sh — one-shot macOS build for FantaManager.
#
# Produces dist/FantaManager.dmg (drag-to-Applications installer).
#
# IMPORTANT: must run ON a Mac. PyInstaller cannot cross-compile: a macOS .app
# contains Mach-O binaries for the machine that built it, so this cannot be
# produced from Windows or Linux. If you have no Mac, a free GitHub Actions
# 'macos-latest' runner can run this script (see BUILD.md).
#
# The .app is built for the architecture of the Mac you build on: an Apple
# Silicon Mac produces an arm64 app (runs on Apple Silicon only), an Intel Mac
# produces x86_64 (runs on both, through Rosetta on Apple Silicon).
#
# Usage (from the project root):
#     bash packaging/build_mac.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERRORE: questo script va eseguito su un Mac." >&2
    echo "PyInstaller non fa cross-compiling: da Windows/Linux non si puo'" >&2
    echo "generare un .app/.dmg per macOS." >&2
    exit 1
fi

# Il 'python3' di sistema su macOS e' quello dei Command Line Tools, fermo alla
# 3.9: troppo vecchio per Django 5, e pip ci installerebbe silenziosamente
# Django 4.2 — la build poi muore molto piu' avanti, dentro l'hook Django di
# PyInstaller, con un TypeError che non dice niente a nessuno. Meglio accorgersene
# qui: se non e' stato indicato un interprete, si cerca il piu' recente >= 3.11
# fra quelli installati (Homebrew, python.org o PATH).
MIN_MINOR=11

py_ok() {
    command -v "$1" >/dev/null 2>&1 && "$1" -c \
        "import sys; sys.exit(0 if sys.version_info[:2] >= (3, $MIN_MINOR) else 1)" \
        >/dev/null 2>&1
}

if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
    if ! command -v "$PY" >/dev/null 2>&1; then
        echo "ERRORE: '$PY' non trovato." >&2
        exit 1
    fi
else
    PY=""
    for cand in python3.14 python3.13 python3.12 python3.11 python3 \
        /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 \
        /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
        /usr/local/bin/python3.14 /usr/local/bin/python3.13 \
        /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
        /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/3.11/bin/python3; do
        if py_ok "$cand"; then PY="$cand"; break; fi
    done
    if [ -z "$PY" ]; then
        echo "ERRORE: serve Python 3.$MIN_MINOR o superiore e non ne ho trovato uno." >&2
        echo "  Il 'python3' di sistema di macOS e' la 3.9: non basta." >&2
        echo "  Installalo con:  brew install python@3.12" >&2
        echo "  oppure scarica l'installer 'macOS 64-bit universal2' da python.org." >&2
        echo "  Poi rilancia:    rm -rf .venv-mac && bash packaging/build_mac.sh" >&2
        exit 1
    fi
fi

if ! py_ok "$PY"; then
    echo "ERRORE: '$PY' e' $("$PY" -V 2>&1), serve 3.$MIN_MINOR o superiore." >&2
    echo "  Indica un interprete piu' recente, per esempio:" >&2
    echo "    PYTHON=python3.12 bash packaging/build_mac.sh" >&2
    exit 1
fi
echo "==> Interprete di build: $PY ($("$PY" -V 2>&1))"

# Un ambiente costruito prima con un Python troppo vecchio va rifatto, altrimenti
# si ricicla in silenzio la Django sbagliata che c'e' dentro.
if [ -x ".venv-mac/bin/python" ] && ! py_ok ".venv-mac/bin/python"; then
    echo "==> .venv-mac ha un Python troppo vecchio: lo ricreo."
    rm -rf ".venv-mac"
fi

# A .venv copied over from a Windows checkout has Scripts/ instead of bin/ and
# Windows binaries inside: unusable here. Build in a separate, platform-specific
# folder so both checkouts can coexist on a shared drive.
VENV=".venv-mac"
if [ ! -x "$VENV/bin/python" ]; then
    echo "==> Creo l'ambiente di build in $VENV ..."
    rm -rf "$VENV"
    "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "==> Installo le dipendenze di build..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
pip install --quiet "pyinstaller>=6.0,<7.0"

echo "==> Costruisco il bundle .app (PyInstaller)..."
pyinstaller --clean --noconfirm packaging/fantamanager.spec

APP="dist/FantaManager.app"
DMG="dist/FantaManager.dmg"
if [ ! -d "$APP" ]; then
    echo "ERRORE: $APP non creato." >&2
    exit 1
fi

echo "==> Verifico che il bundle parta..."
# Boot the frozen app briefly and check it answers HTTP. Cheap smoke test that
# catches a missing hidden import before the .dmg is ever handed to anyone.
SMOKE="$(mktemp -d)"
DJANGO_DB_PATH="$SMOKE/db.sqlite3" FANTAMANAGER_MEDIA_ROOT="$SMOKE/media" \
    "$APP/Contents/MacOS/FantaManager" >"$SMOKE/out.log" 2>&1 &
SMOKE_PID=$!
PORT=""
for _ in $(seq 1 45); do
    sleep 1
    PORT="$(sed -n 's|.*http://localhost:\([0-9]*\)/.*|\1|p' "$SMOKE/out.log" | head -1)"
    if [ -n "$PORT" ] && curl -fsS -o /dev/null "http://127.0.0.1:$PORT/admin-auction/"; then
        break
    fi
    PORT=""
done
kill "$SMOKE_PID" 2>/dev/null || true
wait "$SMOKE_PID" 2>/dev/null || true
if [ -z "$PORT" ]; then
    echo "ERRORE: il bundle non ha risposto. Log:" >&2
    cat "$SMOKE/out.log" >&2
    rm -rf "$SMOKE"
    exit 1
fi
rm -rf "$SMOKE"
echo "    ok (ha risposto sulla porta $PORT)"

echo "==> Costruisco il .dmg..."
rm -f "$DMG"
# Stage a folder with the app + an Applications symlink for drag-install,
# plus an uninstall helper (drag-to-Trash can't run code, so this is the only
# way to also offer removing ~/Library/Application Support/FantaManager).
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp packaging/uninstall_mac.command "$STAGE/Disinstalla FantaManager.command"
chmod +x "$STAGE/Disinstalla FantaManager.command"
hdiutil create -volname "FantaManager" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

echo ""
echo "OK -> $DMG  ($(uname -m))"
echo "Nota: l'app non e' firmata. Al primo avvio: tasto destro > Apri, oppure"
echo "Impostazioni di Sistema > Privacy e sicurezza > Apri comunque."
echo "Per fermare l'app usa \"Chiudi FantaManager\" nella regia: il .app non"
echo "apre finestre di terminale."
