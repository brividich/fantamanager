"""Expose a running auction on the internet through a Cloudflare quick tunnel.

The app is built for a LAN evening: everyone on the same wifi, the regia on the
host PC. This module covers the other case — the managers are elsewhere and only
the host PC has internet. It runs ``cloudflared`` as a child process, which dials
out to Cloudflare and gets back a public ``https://<random>.trycloudflare.com``
address that forwards to the local port (WebSockets included, so live bidding
works). No account, no router config, no port forwarding.

Opening that door changes the threat model, so starting a tunnel also:

* mints a 6-digit **regia PIN** — the console is login-free on the LAN, but a
  request arriving through the public hostname has to unlock with the PIN first
  (see ``request_is_remote`` and the gate in ``views.staff_member_required``);
* turns on ``PUBLIC_TOKENS_REQUIRED``, so anonymous name-joins are refused and
  managers must use their personal tokenised links;
* trusts the public origin for CSRF and reads ``X-Forwarded-Proto`` so Django
  builds ``https://`` URLs;
* hides debug tracebacks from remote callers (the app otherwise runs with
  DEBUG on, to serve static/media from a single process).

Stopping the tunnel reverts every one of those.
"""
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

from django.conf import settings

# Cloudflare's own "latest" download endpoints.
_RELEASE = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
_ASSETS = {
    ("windows", "amd64"): "cloudflared-windows-amd64.exe",
    ("windows", "arm64"): "cloudflared-windows-arm64.exe",
    ("linux",   "amd64"): "cloudflared-linux-amd64",
    ("linux",   "arm64"): "cloudflared-linux-arm64",
    ("darwin",  "amd64"): "cloudflared-darwin-amd64.tgz",
    ("darwin",  "arm64"): "cloudflared-darwin-arm64.tgz",
}

_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")

# Everything mutable lives here, behind one lock: the tunnel is a per-process
# singleton (one auction host, one door).
_LOCK = threading.RLock()
_STATE = {
    "status": "off",     # off | preparing | starting | on | error
    "url": "",           # public https URL once Cloudflare answers
    "host": "",          # just the hostname, for the remote-request check
    "pin": "",           # regia unlock PIN, minted per tunnel
    "error": "",
    "detail": "",        # human-readable progress ("Scarico cloudflared…")
    "port": None,
    "started_at": None,
}
_PROC = None
_SAVED = {}              # settings we override while the tunnel is up

# Regia PIN brute-force throttle — process-global, not per-session. The PIN
# gate used to count failed attempts in request.session, which a caller can
# simply not send back (a fresh request with no cookie resets the count to
# zero every time), making the "5 tries then 60s lockout" trivially
# bypassable. A global, in-memory counter has nothing for the client to shed.
_PIN_LOCK = threading.Lock()
_PIN_STATE = {"tries": 0, "locked_until": 0.0}


# --- cloudflared binary -----------------------------------------------------

def _arch():
    m = (platform.machine() or "").lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    return "amd64"


def data_dir():
    """Where the downloaded binary is cached (same folder the app writes to)."""
    override = os.getenv("FANTAMANAGER_DATA_DIR")
    if override:
        d = Path(override)
    elif sys.platform == "win32":
        d = Path(os.environ.get("APPDATA") or Path.home()) / "FantaManager"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "FantaManager"
    else:
        d = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "FantaManager"
    d.mkdir(parents=True, exist_ok=True)
    return d


def binary_path():
    """Cached cloudflared path, or whatever is already on PATH."""
    name = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
    local = data_dir() / name
    if local.exists():
        return local
    found = shutil.which("cloudflared")
    return Path(found) if found else local


def binary_ready():
    p = binary_path()
    return p.exists()


def ensure_binary(progress=None):
    """Download cloudflared into the data folder if we don't have it yet.

    Returns the path. Raises ``RuntimeError`` with an Italian message on failure
    — this runs behind a button in the console, so the text is user-facing.
    """
    p = binary_path()
    if p.exists():
        return p

    osname = "windows" if sys.platform == "win32" else sys.platform
    key = (osname, _arch())
    asset = _ASSETS.get(key)
    if asset is None:
        raise RuntimeError(f"Nessun cloudflared disponibile per {key[0]}/{key[1]}.")

    if progress:
        progress("Scarico cloudflared (una volta sola, ~35 MB)…")
    target = data_dir() / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(_RELEASE + asset, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as exc:  # noqa: BLE001 — surfaced to the UI as text
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Download di cloudflared non riuscito: {exc}") from exc

    if asset.endswith(".tgz"):
        # macOS ships a tarball holding the single 'cloudflared' binary. Extract
        # beside the target and only then move it into place: a half-written
        # binary left at `target` would look "already downloaded" forever and
        # fail on every later run.
        staged = target.with_suffix(".staged")
        try:
            with tarfile.open(tmp) as tf:
                member = next(
                    (m for m in tf.getmembers()
                     if m.isfile() and Path(m.name).name == "cloudflared"),
                    None,
                )
                if member is None:
                    raise RuntimeError("l'archivio non contiene il binario cloudflared")
                with tf.extractfile(member) as src, open(staged, "wb") as f:
                    shutil.copyfileobj(src, f)
            staged.replace(target)
        except Exception as exc:  # noqa: BLE001
            staged.unlink(missing_ok=True)
            raise RuntimeError(f"Estrazione di cloudflared non riuscita: {exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)
    else:
        tmp.replace(target)

    if sys.platform != "win32":
        target.chmod(0o755)
    return target


# --- local addresses --------------------------------------------------------

def lan_ip():
    """Best-effort LAN address of this machine (no traffic is actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return ""


def _is_loopback_host(host):
    h = (host or "").split(":")[0].lower()
    return h in ("localhost", "127.0.0.1", "::1", "[::1]")


def _lan_from_request(request):
    """LAN address derived from the incoming request (host, port, scheme).

    Internal: only correct for a request that actually arrived on this machine.
    Use :func:`lan_url`, which also covers requests coming through the tunnel.
    """
    ip = lan_ip()
    if not ip:
        return ""
    host = request.get_host()
    port = host.rsplit(":", 1)[1] if ":" in host else ""
    scheme = "https" if request.is_secure() else "http"
    base = f"{scheme}://{ip}" + (f":{port}" if port else "")
    if base.rstrip("/") == f"{scheme}://{host}".rstrip("/"):
        return ""
    return base


def lan_url(request):
    """**The** wifi address of this machine — one canonical builder.

    Deriving it from the request is only right when the request came in on the
    LAN. Through the Cloudflare tunnel the host has no port and speaks https, so
    the naive version produced ``https://192.168.1.50`` — an address that
    answers nowhere. When the tunnel is up we rebuild it from the port the
    tunnel was opened for, which is the port this server is really listening on.

    Returns "" when there is no usable LAN address (offline machine), or when
    the caller is already on it (nothing to suggest).
    """
    if not request_is_remote(request):
        return _lan_from_request(request)
    ip = lan_ip()
    if not ip:
        return ""
    with _LOCK:
        port = _STATE.get("port")
    return f"http://{ip}" + (f":{port}" if port else "")


# Legacy name kept so nothing breaks mid-refactor; prefer lan_url().
lan_base_url = _lan_from_request


def best_base_url(request):
    """Where to point links handed to other people, in order of usefulness.

    The public tunnel wins when it is open (works from anywhere), then the LAN
    address (works for anyone in the room), then whatever host was used.
    """
    public = public_url()
    if public:
        return public.rstrip("/")
    if _is_loopback_host(request.get_host()):
        lan = _lan_from_request(request)
        if lan:
            return lan
    return request.build_absolute_uri("/").rstrip("/")


# --- settings hardening -----------------------------------------------------

def _harden(host, url):
    """Tighten the settings that matter once the app is reachable publicly."""
    _SAVED["PUBLIC_TOKENS_REQUIRED"] = getattr(settings, "PUBLIC_TOKENS_REQUIRED", False)
    _SAVED["CSRF_TRUSTED_ORIGINS"] = list(getattr(settings, "CSRF_TRUSTED_ORIGINS", []))
    _SAVED["SECURE_PROXY_SSL_HEADER"] = getattr(settings, "SECURE_PROXY_SSL_HEADER", None)

    # Managers must use their personal tokenised links; no joining by typing a
    # name, and the big screen needs its token.
    settings.PUBLIC_TOKENS_REQUIRED = True
    origins = list(_SAVED["CSRF_TRUSTED_ORIGINS"])
    if url and url not in origins:
        origins.append(url)
    settings.CSRF_TRUSTED_ORIGINS = origins
    # cloudflared terminates TLS and forwards this header, so Django knows the
    # request was https and builds https:// links for QR codes and join links.
    settings.SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


def _unharden():
    for key, value in _SAVED.items():
        setattr(settings, key, value)
    _SAVED.clear()


# --- public API -------------------------------------------------------------

def status():
    """A JSON-safe snapshot for the console."""
    with _LOCK:
        s = dict(_STATE)
    s["binary_ready"] = binary_ready()
    started = s.pop("started_at", None)
    # How long the door has been open — the one live figure the detail page can
    # report honestly (cloudflared tells us nothing about clients or latency).
    s["uptime_seconds"] = int(time.time() - started) if started else 0
    return s


def public_host():
    with _LOCK:
        return _STATE["host"] if _STATE["status"] == "on" else ""


def public_url():
    with _LOCK:
        return _STATE["url"] if _STATE["status"] == "on" else ""


def is_on():
    return bool(public_host())


def regia_pin():
    with _LOCK:
        return _STATE["pin"]


def regia_pin_lockout_remaining():
    """Seconds left before the next PIN attempt is allowed (0 if none)."""
    with _PIN_LOCK:
        remaining = _PIN_STATE["locked_until"] - time.time()
        return max(0.0, remaining)


def regia_pin_register_failure():
    """Count one failed attempt; lock out for 60s after the 5th in a row."""
    with _PIN_LOCK:
        _PIN_STATE["tries"] += 1
        if _PIN_STATE["tries"] >= 5:
            _PIN_STATE["locked_until"] = time.time() + 60
            _PIN_STATE["tries"] = 0


def regia_pin_register_success():
    with _PIN_LOCK:
        _PIN_STATE["tries"] = 0
        _PIN_STATE["locked_until"] = 0.0


def request_is_remote(request):
    """True when this request came in through the public tunnel hostname.

    The LAN experience is deliberately login-free, so the admin gate only bites
    on the public door. cloudflared preserves the ``Host`` header, so comparing
    against the tunnel hostname is both simple and reliable — and it cannot be
    spoofed into *less* protection, only into more.
    """
    host = public_host()
    if not host:
        return False
    try:
        return request.get_host().split(":")[0].lower() == host.lower()
    except Exception:  # noqa: BLE001 — DisallowedHost etc. count as remote
        return True


def _set(**kw):
    with _LOCK:
        _STATE.update(kw)


def _timeout_guard(seconds=75):
    """Flip to an error if the tunnel never comes up, even if cloudflared is mute.

    Needed because ``_watch`` blocks on ``readline``: without this a silent
    child would leave the console spinning on "starting" forever.
    """
    def _run():
        time.sleep(seconds)
        with _LOCK:
            if _STATE["status"] in ("preparing", "starting"):
                _STATE.update(
                    status="error", detail="",
                    error="Cloudflare non ha risposto in tempo. Controlla la "
                          "connessione del PC e riprova.")
    threading.Thread(target=_run, daemon=True).start()


def _watch(proc):
    """Read cloudflared's output until the public URL shows up, then keep it."""
    while True:
        line = proc.stderr.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
        match = _URL_RE.search(text)
        if match and not public_url():
            url = match.group(0)
            host = url.split("//", 1)[1]
            _harden(host, url)
            _set(status="on", url=url, host=host, detail="", error="",
                 started_at=time.time())
    # Child ended. If it never opened a tunnel, say so; if it was up and died,
    # drop back to "off" with a note so the console stops advertising a dead URL.
    with _LOCK:
        if _STATE["status"] in ("starting", "preparing"):
            _STATE.update(status="error", detail="",
                          error=_STATE["error"] or "cloudflared si è chiuso senza aprire il tunnel.")
        elif _STATE["status"] == "on":
            _unharden()
            _STATE.update(status="error", url="", host="", detail="",
                          error="Il tunnel si è interrotto. Riattivalo per riavere il link.")


def start(port):
    """Open the tunnel for ``port``. Returns the status dict.

    Non-blocking beyond the binary download: the public URL arrives a couple of
    seconds later, so the console polls :func:`status`.
    """
    global _PROC
    with _LOCK:
        if _STATE["status"] in ("on", "starting", "preparing"):
            return status()
        _STATE.update(status="preparing", url="", host="", error="", detail="",
                      port=int(port), pin=_STATE["pin"] or f"{secrets.randbelow(900000) + 100000}")

    def _run():
        global _PROC
        try:
            exe = ensure_binary(progress=lambda msg: _set(detail=msg))
        except RuntimeError as exc:
            _set(status="error", error=str(exc), detail="")
            return
        _set(status="starting", detail="Apro il tunnel…")
        _timeout_guard()
        cmd = [str(exe), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
        if sys.platform == "win32":
            # Don't flash a console window in the packaged app.
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            _PROC = subprocess.Popen(cmd, **kwargs)
        except OSError as exc:
            _set(status="error", error=f"Avvio di cloudflared non riuscito: {exc}", detail="")
            return
        _watch(_PROC)

    threading.Thread(target=_run, daemon=True).start()
    return status()


def shutdown_process(delay=0.6):
    """Quit the whole app shortly after the caller has replied.

    A packaged macOS ``.app`` opens no window, so "close the console to stop"
    only works on Windows; this backs the in-app quit button. Kept here (and out
    of the views) so tests can replace it without killing the test runner. The
    delay lets the HTTP response reach the browser first; ``os._exit`` skips
    interpreter cleanup on purpose — daphne would otherwise hold the process
    open on its listening socket.
    """
    def _die():
        time.sleep(delay)
        os._exit(0)

    threading.Thread(target=_die, daemon=True).start()


def stop():
    """Close the tunnel and put every hardened setting back."""
    global _PROC
    proc, _PROC = _PROC, None
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    _unharden()
    with _LOCK:
        _STATE.update(status="off", url="", host="", error="", detail="",
                      port=None, started_at=None)
    with _PIN_LOCK:
        _PIN_STATE.update(tries=0, locked_until=0.0)
    return status()
