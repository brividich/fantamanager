"""Remote access management: Cloudflare quick tunnel, remote PIN gate, and shutdown."""
import io
import json

try:
    import qrcode
except ImportError:
    qrcode = None

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import remote
from .common import (current_auction, forbidden_json, manageable_leagues,
                     regia_pin_lockout_error, safe_next, staff_member_required,
                     target_league, try_regia_pin)

# The tunnel is one door into the whole server, and its status carries the
# regia PIN: it belongs to whoever runs the machine, not to every organiser
# (anyone can sign up and become one).
_SUPERADMIN_ONLY = "L'accesso remoto è riservato al superadmin."


def _current_port(request):
    """The port this server is actually listening on, for the tunnel target."""
    host = request.get_host()
    if ":" in host:
        try:
            return int(host.rsplit(":", 1)[1])
        except ValueError:
            pass
    return 443 if request.is_secure() else 80


@staff_member_required
def admin_remote_status(request):
    if not request.user.is_superuser:
        return forbidden_json()
    return JsonResponse({"ok": True, "remote": remote.status()})


@staff_member_required
def admin_remote_page(request):
    """The full 'accesso remoto' desk: address + QR, regia PIN, quick links.

    The console only carries the compact strip; everything tunnel-related that
    needs room (and is looked at once per evening) lives here.
    """
    if not request.user.is_superuser:
        return HttpResponseForbidden(_SUPERADMIN_ONLY)
    leagues = list(manageable_leagues(request.user))
    current_league = target_league(request)
    live = current_auction(request, current_league) if current_league else None
    return render(request, "auctions/admin_remote.html", {
        "leagues": leagues,
        "current_league": current_league,
        "selected": live,
        "console_section": "Accesso remoto",
        "console_active": "remote",
        "remote_json": json.dumps(remote.status()),
        "lan_url": remote.lan_url(request),
    })


def admin_remote_qr(request):
    """QR of the public tunnel address (404 while the tunnel is closed)."""
    url = remote.public_url()
    if not url:
        return HttpResponse("tunnel non attivo", status=404)
    if qrcode is None:
        return HttpResponse("qrcode non installato", status=503)
    buf = io.BytesIO()
    qrcode.make(url, box_size=10, border=2).save(buf, format="PNG")
    resp = HttpResponse(buf.getvalue(), content_type="image/png")
    resp["Cache-Control"] = "no-store"
    return resp


@staff_member_required
@require_POST
def admin_quit(request):
    """Shut the whole app down from the browser.

    The Windows build shows a console window you can close; a macOS ``.app``
    shows nothing at all, so without this the only way to stop the server is
    Activity Monitor. Only the desktop app has it (on a server, stopping the
    process would take the service down for every league), only for the
    superadmin, and never from the public tunnel: stopping the auction is for
    whoever is sitting at the host machine, PIN or not.
    """
    if not settings.DESKTOP_APP:
        return JsonResponse({"ok": False, "error": "not_available"}, status=404)
    if not request.user.is_superuser:
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    if remote.request_is_remote(request):
        return JsonResponse({"ok": False, "error": "local_only"}, status=403)
    remote.stop()            # never leave a tunnel (and its child) behind
    remote.shutdown_process()
    return JsonResponse({"ok": True})


@staff_member_required
@require_POST
def admin_remote_start(request):
    """Open the public tunnel. The URL lands a few seconds later — poll status."""
    if not request.user.is_superuser:
        return forbidden_json()
    if remote.is_on():
        return JsonResponse({"ok": True, "remote": remote.status()})
    # Never tunnel to the tunnel: when the request already came in through the
    # public hostname we don't know the local port from the Host header.
    port = request.POST.get("port") or _current_port(request)
    try:
        port = int(port)
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "bad_port"}, status=400)
    return JsonResponse({"ok": True, "remote": remote.start(port)})


@staff_member_required
@require_POST
def admin_remote_stop(request):
    if not request.user.is_superuser:
        return forbidden_json()
    return JsonResponse({"ok": True, "remote": remote.stop()})


def regia_unlock(request):
    """PIN gate for reaching the console through the public tunnel.

    Only relevant remotely: on the LAN the console is open, so a visit here just
    bounces back to the dashboard. Attempts are throttled process-wide (not per
    session): a session-based counter can be reset for free by simply not
    sending the cookie back, which defeats the "5 tries then 60s lockout"
    entirely — see remote.regia_pin_register_failure(). The PIN is short by
    design (it has to be read off a screen and typed on a phone), so the
    lockout is the only thing standing between it and brute force.
    """
    if not remote.request_is_remote(request):
        return redirect("admin_dashboard")

    target = safe_next(request, reverse("admin_dashboard"))   # never bounce off-site

    if request.method == "POST":
        error = try_regia_pin(request)
        if not error:
            return redirect(target)
    else:
        error = regia_pin_lockout_error()

    return render(request, "auctions/regia_unlock.html",
                  {"error": error, "next": target}, status=200 if not error else 401)
