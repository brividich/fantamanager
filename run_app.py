"""FantaManager launcher.

Single entry point used both for local runs (`python run_app.py`) and for the
packaged desktop builds (PyInstaller → .exe on Windows, .app/.dmg on macOS).

It boots the Django + Channels (daphne) ASGI stack through Django's own
`runserver` command — which serves HTTP, WebSocket, static and media in one
process — after making sure the database and uploads live in a user-writable
data folder. The default browser is opened on the dashboard automatically.

The packaged build runs **without a console window**: a black cmd box is an
alarming thing to hand a paying customer, and everything it used to show (the
wifi address, the join links) already lives in the app itself. What the window
did provide was a way to stop the auction, so that moves to an icon next to the
clock — open the console, read the address, quit — and everything the process
prints goes to `fantamanager.log` in the data folder instead of nowhere.
"""
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Keep console output UTF-8 so accents/symbols don't garble on Windows cmd.exe.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def _capture_output(data_dir):
    """Send stdout/stderr to a log file when the build has no console.

    A windowed PyInstaller build leaves ``sys.stdout`` as ``None``: any library
    that prints — Django's own startup banner, every request line — would then
    raise on a missing ``write``. Pointing both streams at a file fixes that and
    leaves something to read when a customer says "non funziona".

    Truncated at each start: this is the log of the evening in progress, not an
    archive, and nobody should have to think about a file that grows forever.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    path = data_dir / "fantamanager.log"
    try:
        stream = open(path, "w", encoding="utf-8", buffering=1)
    except OSError:
        return None
    sys.stdout = sys.stderr = stream
    return path


def app_data_dir() -> Path:
    """A per-user, writable folder for the database, uploads and logs.

    The PyInstaller bundle itself is read-only (and on a temp path in one-file
    mode), so all mutable state must live here instead.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    d = base / "FantaManager"
    (d / "media").mkdir(parents=True, exist_ok=True)
    return d


def _get_or_create_secret_key(data_dir: Path) -> str:
    """A random Django SECRET_KEY, generated once per install and reused after.

    Without this every copy of the packaged app fell back to the same
    hardcoded string in liveauction/settings.py — every installation in the
    world signing its session/CSRF cookies with an identical, publicly known
    key. Harmless on a closed LAN, but this app also supports publishing an
    auction on the internet via a tunnel (see auctions/remote.py); with a
    shared key, anyone who has read the source could forge a session cookie
    for someone else's published auction. Stored next to the database so it
    survives restarts but never leaves this machine.
    """
    path = data_dir / "secret.key"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    import secrets
    key = secrets.token_urlsafe(64)
    try:
        path.write_text(key, encoding="utf-8")
    except OSError:
        pass  # worst case: a fresh key every start, still never shared across installs
    return key


def free_port(preferred: int = 8000) -> int:
    """Return ``preferred`` if free, otherwise an OS-assigned free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", preferred))
            return preferred
        except OSError:
            s.bind(("0.0.0.0", 0))
            return s.getsockname()[1]


def local_ip() -> str:
    """Best-effort LAN IP so participants can join from their phones."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _tray_image():
    """L'icona da mostrare vicino all'orologio: lo scudetto dell'app.

    Ricade su un quadrato tinta unita se il file non c'e' (esecuzione dai
    sorgenti, build senza icona): meglio un segnaposto che nessun modo di
    chiudere l'app.
    """
    from PIL import Image
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for candidate in (base / "packaging" / "icon.ico", base / "icon.ico"):
        try:
            return Image.open(candidate)
        except (OSError, ValueError):
            continue
    return Image.new("RGBA", (64, 64), (19, 181, 255, 255))


def _message_box(text, title, style=0):
    """Finestrella di sistema. Ritorna la scelta (6 = Si', 7 = No)."""
    try:
        import ctypes
        return ctypes.windll.user32.MessageBoxW(None, text, title, style)
    except Exception:
        return 0


def _start_tray(home_url, lan_url, data_dir):
    """Icona vicino all'orologio: apri la regia, leggi l'indirizzo, chiudi.

    Gira su un thread secondario mentre il server tiene quello principale — il
    reactor di Twisted sotto daphne vuole il thread principale per i segnali,
    l'icona no. Se il modulo manca (esecuzione dai sorgenti senza pystray)
    l'app parte lo stesso: si perde il modo comodo di chiuderla, non l'asta.

    Su macOS l'icona non si accende affatto: li' pystray passa da AppKit, che
    pretende il thread principale - e quello ce l'ha il server. Provarci
    comunque non da' un avviso ma un crash secco all'avvio
    ("NSUpdateCycleInitialize() is called off the main thread", SIGTRAP).
    Su Mac si chiude dalla regia, col pulsante "Chiudi FantaManager".
    """
    if sys.platform == "darwin":
        print("macOS: nessuna icona di sistema, si chiude dalla regia.", flush=True)
        return None
    try:
        import pystray
    except ImportError:
        print("pystray non disponibile: nessuna icona di sistema.", flush=True)
        return None

    def open_console(icon=None, item=None):
        webbrowser.open(home_url)

    def show_address(icon=None, item=None):
        _message_box(
            f"Chi e' collegato allo stesso wifi apre:\n\n{lan_url}\n\n"
            "Premi Ctrl+C per copiare questo messaggio.\n\n"
            f"I dati dell'asta sono salvati in:\n{data_dir}",
            "FantaManager — indirizzo per i telefoni")

    def quit_app(icon=None, item=None):
        # 4 = Si'/No, 48 = punto esclamativo. Chiudere qui significa staccare
        # tutti i telefoni collegati: se sta girando un'asta e' un disastro, e
        # un clic distratto sull'icona non deve poterlo causare.
        if _message_box(
                "Vuoi chiudere FantaManager?\n\n"
                "L'asta si ferma e i telefoni collegati perdono il collegamento. "
                "Quello che e' gia' stato assegnato resta salvato.",
                "FantaManager", 4 | 48) != 6:
            return
        from auctions.backup import backup_database
        backup_database(reason="chiusura")
        try:
            icon.stop()
        except Exception:
            pass
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Apri la regia", open_console, default=True),
        pystray.MenuItem("Indirizzo per i telefoni…", show_address),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Chiudi FantaManager", quit_app),
    )
    icon = pystray.Icon("FantaManager", _tray_image(), "FantaManager — asta in corso", menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


def main() -> None:
    data = app_data_dir()

    # Writable locations + LAN-friendly defaults. setdefault lets an advanced
    # user still override any of these from the environment / a .env file.
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "liveauction.settings")
    os.environ.setdefault("DJANGO_DB_PATH", str(data / "db.sqlite3"))
    os.environ.setdefault("FANTAMANAGER_MEDIA_ROOT", str(data / "media"))
    os.environ.setdefault("DJANGO_DEBUG", "True")          # localhost/LAN tool
    os.environ.setdefault("FANTAMANAGER_DESKTOP", "1")     # "Esci", browser login, ...
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "*")
    os.environ.setdefault("DJANGO_SECRET_KEY", _get_or_create_secret_key(data))

    # Da qui in poi si puo' stampare: senza console finisce nel log.
    log_path = _capture_output(data)

    import django
    django.setup()
    from django.core.management import call_command
    from auctions.backup import backup_database, repair_at_startup

    # Un database rovinato (spegnimento brusco, disco) non deve fermare la
    # serata: va da parte e torna l'ultima copia integra, con un avviso.
    repaired = repair_at_startup()
    if repaired:
        print(repaired, flush=True)
        _message_box(repaired, "FantaManager", 48)

    # Before the migration below (or anything else) touches the database,
    # keep a known-good snapshot of last night's state.
    backup_database(reason="avvio")

    # First-run / upgrade: bring the SQLite schema up to date silently.
    call_command("migrate", interactive=False, verbosity=0)

    port = free_port(8000)
    url = f"http://localhost:{port}/"
    lan = f"http://{local_ip()}:{port}/"
    # Avviare l'app apre il portale di ingresso iniziale (Home): da qui si sceglie
    # se entrare nell'Asta Libera / Maxischermo oppure fare login nella Regia (Dashboard).
    home = url

    from liveauction import __version__ as app_version

    banner = (
        f"\n  FantaManager Live Auction · v{app_version}\n"
        f"  - Questo PC (regia):   {url}\n"
        f"  - Stesso wifi:         {lan}\n"
        f"  - Dati salvati in:     {data}\n"
        + (f"  - Registro:            {log_path}\n" if log_path else "")
        + "\n  Allenatori fuori casa? Nella regia apri \"Accesso da internet\":\n"
        "  ottieni un indirizzo pubblico senza toccare il router.\n"
        "\n  Per fermare l'asta: icona FantaManager vicino all'orologio,\n"
        "  poi \"Chiudi FantaManager\".\n"
    )
    print(banner, flush=True)

    # L'icona vicino all'orologio e' l'unico appiglio visibile quando non c'e'
    # una finestra: va accesa prima che il server prenda il thread principale.
    _start_tray(home, lan, data)

    # Open the dashboard once the server is accepting connections.
    def _open_when_ready():
        for _ in range(60):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.25)
        webbrowser.open(home)

    threading.Thread(target=_open_when_ready, daemon=True).start()

    # runserver (overridden by daphne) serves HTTP + WebSocket + static + media.
    # use_reloader=False is essential under PyInstaller (no re-exec of the exe).
    call_command("runserver", f"0.0.0.0:{port}", use_reloader=False, skip_checks=True)


if __name__ == "__main__":
    main()
