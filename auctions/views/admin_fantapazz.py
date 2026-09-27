"""Fantapazz integration views: cookie sync, auth status, browser login, and roster import."""
import json
import os
import secrets as _sec
import subprocess
import sys
import time

import requests as req
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ..models import League
from ..providers import ProviderError, get_provider
from ..providers import fantapazz as fp
from ..providers import importers
from .common import (
    FORBIDDEN_LEAGUE_MSG,
    current_auction,
    current_league,
    manageable_leagues,
    staff_member_required,
    target_league,
    user_can_manage_scope,
)

_FP_BASE = fp.BASE
_FP_COOKIE_TTL = fp.COOKIE_TTL


def _fp_provider():
    return get_provider("fantapazz")


def _resolve_import_league(request):
    """The FantaManager League a Fantapazz import writes into."""
    raw = (request.POST.get("target_league_id") or "").strip()
    if raw.isdigit():
        league = League.objects.filter(pk=int(raw)).first()
        if league is not None:
            return league

    # Several FantaManager leagues may mirror the same Fantapazz league: only
    # look among the ones this user runs.
    fp_league_id = (request.POST.get("league_id") or "").strip()
    if fp_league_id:
        league = manageable_leagues(request.user).filter(external_id=fp_league_id).first()
        if league is not None:
            return league

    return target_league(request)


def _import_league_or_403(request):
    """``(league, None)`` when the user may write into the resolved league — no
    league means the global pool, superusers only — else ``(None, 403)``."""
    league = _resolve_import_league(request)
    if not user_can_manage_scope(request.user, league):
        return None, JsonResponse({"ok": False, "error": FORBIDDEN_LEAGUE_MSG}, status=403)
    return league, None


@staff_member_required
def admin_fantapazz(request):
    if request.method == "GET":
        token = _sec.token_hex(16)
        request.session["fp_sync_token"] = token
        league = current_league(request)
        return render(request, "auctions/admin_fantapazz.html", {
            "league_id":  (league.external_id if league else "") or "332175",
            "sync_token": token,
            "has_cookie": bool(request.session.get("fp_cookie")),
            "leagues": list(manageable_leagues(request.user)),
            "current_league": league,
            "console_section": "Importa",
            "console_active": "import",
            "selected": current_auction(request, league),
        })

    league_id   = request.POST.get("league_id", "332175").strip()
    action      = request.POST.get("action", "preview")
    replace     = request.POST.get("replace") == "1"
    auth_mode   = request.POST.get("auth_mode", "cookie")

    # Check where an import would land before logging in to Fantapazz at all.
    league = None
    if action not in ("preview", "debug"):
        league, denied = _import_league_or_403(request)
        if denied:
            return denied

    provider = _fp_provider()

    try:
        if auth_mode == "login":
            username = request.POST.get("username", "").strip()
            password = request.POST.get("password", "").strip()
            if not username or not password:
                return JsonResponse({"ok": False, "error": "Inserisci username e password."})
            session = provider.login(username, password)
        elif auth_mode == "session":
            _fp_collect_synced_cookie(request)
            cookie_str = request.session.get("fp_cookie", "")
            if not cookie_str:
                return JsonResponse({"ok": False, "error": "Nessuna sessione Fantapazz salvata. Usa il metodo Browser o Snippet prima."})
            session = provider.session_from_cookie(cookie_str)
        else:
            cookie_str = request.POST.get("cookie", "").strip()
            if not cookie_str:
                return JsonResponse({"ok": False, "error": "Cookie di sessione mancante."})
            session = provider.session_from_cookie(cookie_str)

        manual_ids = [
            x.strip() for x in request.POST.get("team_ids", "").split(",")
            if x.strip().isdigit()
        ]
        if not manual_ids:
            manual_ids = [str(t) for t in request.session.get("fp_team_ids", []) if str(t).isdigit()]

        team_ids, r = provider.discover_team_ids(session, league_id, manual_ids or None)

        if not team_ids:
            return JsonResponse({
                "ok": False,
                "error": "Nessuna squadra trovata. Inserisci gli ID squadra manualmente (dal tab Network).",
                "html_snippet": r.text[:2000],
            })

        teams, errors, all_players = provider.fetch_rosters(session, league_id, team_ids)

        if action == "debug":
            debug = {
                "teams_found": team_ids,
                "league_html_snippet": r.text[:4000],
                "paginated_responses": [],
                "first_team_html": "",
            }
            for page in range(1, 4):
                ts  = int(time.time() * 1000)
                url = f"{_FP_BASE}/fantacalcio/squadre-lega/{league_id}/0?expanded=0&ignore=20&pageNumber={page}&_={ts}"
                pr  = session.get(url, timeout=8)
                debug["paginated_responses"].append({
                    "page": page, "status": pr.status_code,
                    "html": pr.text[:2000],
                })
            if team_ids:
                url = f"{_FP_BASE}/modal/fantacalcio/rosa-squadra/{team_ids[0]['id']}"
                rr  = session.get(url, timeout=10)
                debug["first_team_html"]   = rr.text[:8000]
                debug["first_team_parsed"] = provider.parse_rosters(rr.text)
            return JsonResponse({"ok": True, **debug})

        if action == "preview":
            return JsonResponse({
                "ok": True,
                "teams": team_ids,
                "players": all_players[:50],
                "total": len(all_players),
                "errors": errors,
            })

        created = importers.import_players_simple(all_players, replace=replace, league=league)

        return JsonResponse({
            "ok": True,
            "created": created,
            "teams": len(team_ids),
            "errors": errors,
        })

    except req.exceptions.ConnectionError:
        return JsonResponse({"ok": False, "error": "Impossibile connettersi a Fantapazz."})
    except ProviderError as e:
        return JsonResponse({"ok": False, "error": str(e)})
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)})


@csrf_exempt
def admin_fantapazz_cookie_sync(request):
    """Receives the Fantapazz cookie from a cross-origin browser snippet."""
    def _cors(response):
        response["Access-Control-Allow-Origin"]  = "*"
        response["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response["Access-Control-Allow-Headers"] = "Content-Type, X-Token"
        return response

    if request.method == "OPTIONS":
        return _cors(JsonResponse({}))
    if request.method != "POST":
        return _cors(JsonResponse({"ok": False}))

    token = request.headers.get("X-Token", "") or request.POST.get("token", "")
    if not token or len(token) < 8:
        return _cors(JsonResponse({"ok": False, "error": "Token mancante o non valido."}))

    cookie_str = ""
    team_ids   = []
    raw = request.body.decode("utf-8", errors="replace").strip()

    rose_ready = False
    if raw.startswith("{"):
        try:
            data       = json.loads(raw)
            cookie_str = (data.get("cookie") or "").strip()
            team_ids   = [str(t) for t in data.get("team_ids", []) if str(t).isdigit()]
            rose_ready = bool(data.get("rose_ready"))
        except (ValueError, TypeError):
            cookie_str = raw
    else:
        cookie_str = raw or request.POST.get("cookie", "").strip()

    if not cookie_str:
        return _cors(JsonResponse({"ok": False, "error": "Nessun cookie ricevuto."}))

    cache.set(f"fp_cookie_{token}", cookie_str, _FP_COOKIE_TTL)
    if team_ids:
        cache.set(f"fp_teams_{token}", team_ids, _FP_COOKIE_TTL)
    if rose_ready:
        cache.set(f"fp_rose_{token}", True, _FP_COOKIE_TTL)
    return _cors(JsonResponse({"ok": True, "message": "Cookie salvato. Torna su FantaManager."}))


def _fp_collect_synced_cookie(request):
    """Move a token-keyed synced cookie (+ team IDs) into the admin's session."""
    token = request.session.get("fp_sync_token", "")
    if not token:
        return False
    cookie_str = cache.get(f"fp_cookie_{token}")
    if cookie_str:
        request.session["fp_cookie"] = cookie_str
        teams = cache.get(f"fp_teams_{token}")
        if teams:
            request.session["fp_team_ids"] = teams
            cache.delete(f"fp_teams_{token}")
        request.session.modified = True
        cache.delete(f"fp_cookie_{token}")
        return True
    return False


@staff_member_required
def admin_fantapazz_auth_status(request):
    """Verify the saved Fantapazz session is valid and return the logged-in username."""
    _fp_collect_synced_cookie(request)

    token     = request.session.get("fp_sync_token", "")
    rose_path = os.path.join(str(settings.MEDIA_ROOT), f"fp_rose_{token}.xls")
    rose_ready = bool(token) and os.path.exists(rose_path)

    cookie_str = request.session.get("fp_cookie", "")
    if not cookie_str:
        return JsonResponse({"ok": True, "authenticated": False, "reason": "no_session", "rose_ready": rose_ready})

    try:
        from bs4 import BeautifulSoup
        session = _fp_provider().session_from_cookie(cookie_str)
        r = session.get(f"{_FP_BASE}/fantacalcio", timeout=12, allow_redirects=True)

        logged_in = False
        username  = None

        text = r.text.lower()
        if "/user/logout" in text or "esci" in text or "logout" in text:
            logged_in = True

        if logged_in:
            soup = BeautifulSoup(r.text, "html.parser")
            for sel in [
                ("a", {"href": lambda h: h and "/user/" in h and h != "/user/logout"}),
                ("span", {"class": lambda c: c and "username" in (c if isinstance(c, str) else " ".join(c))}),
            ]:
                el = soup.find(*sel)
                if el and el.get_text(strip=True):
                    username = el.get_text(strip=True)[:60]
                    break

        return JsonResponse({
            "ok": True,
            "authenticated": logged_in,
            "username": username,
            "reason": "" if logged_in else "session_expired",
            "rose_ready": rose_ready,
        })
    except Exception as e:
        return JsonResponse({"ok": True, "authenticated": False, "reason": str(e), "rose_ready": rose_ready})


@staff_member_required
@require_POST
def admin_fantapazz_browser_login(request):
    """Launch the Playwright login helper as a SEPARATE PROCESS.

    It opens a visible browser on the machine running the server: that is the
    desktop app's operator sitting at it. On a server nobody would see that
    window, and any account could keep spawning browsers.
    """
    if not settings.DESKTOP_APP:
        return JsonResponse({
            "ok": False,
            "error": "Il login con il browser funziona solo nell'app desktop: qui carica il file delle rose.",
        }, status=404)
    try:
        import playwright  # noqa: F401
    except ImportError:
        return JsonResponse({
            "ok": False,
            "error": "Playwright non installato. Esegui nel terminale:\npip install playwright\nplaywright install chromium",
        })

    token = request.session.get("fp_sync_token", "")
    if not token:
        token = _sec.token_hex(16)
        request.session["fp_sync_token"] = token
        request.session.modified = True

    server_origin = f"{request.scheme}://{request.get_host()}"
    league_id     = request.POST.get("league_id", "").strip()
    media_root    = str(settings.MEDIA_ROOT)
    os.makedirs(media_root, exist_ok=True)

    # Note: fp_browser_login.py is in the parent auctions/ directory
    script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fp_browser_login.py")

    try:
        subprocess.Popen(
            [sys.executable, script, token, server_origin, league_id, media_root],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Impossibile avviare il browser: {e}"})

    return JsonResponse({
        "ok": True,
        "message": "Browser aperto — fai la login su Fantapazz (anche con Facebook). Al termine il cookie viene catturato automaticamente.",
    })


@staff_member_required
@require_POST
def admin_fantapazz_import_rose(request):
    """Parse the auto-downloaded rose .xls (or an uploaded one) and import it."""
    replace = request.POST.get("replace") == "1"
    download_log = ""

    upload = request.FILES.get("rose_file")
    if upload:
        try:
            teams = importers.parse_rose_xls(upload.read())
        except Exception as e:
            return JsonResponse({"ok": False, "error": f"File non leggibile: {e}"})
    else:
        token = request.session.get("fp_sync_token", "")
        path  = os.path.join(settings.MEDIA_ROOT, f"fp_rose_{token}.xls")
        if not token or not os.path.exists(path):
            return JsonResponse({"ok": False, "error": "Nessun file rose disponibile. Esegui prima il login browser."})
        try:
            teams = importers.parse_rose_xls(path)
        except Exception as e:
            return JsonResponse({"ok": False, "error": f"File non leggibile: {e}"})
        log_path = path + ".log"
        if os.path.exists(log_path):
            try:
                with open(log_path, encoding="utf-8") as fh:
                    download_log = fh.read()
            except Exception:
                pass

    if not teams:
        return JsonResponse({"ok": False, "error": "Nessuna squadra trovata nel file."})

    warning = ""
    if len(teams) <= 1:
        warning = ("Attenzione: nel file c'è una sola squadra. Probabilmente è stato "
                   "scaricato l'export della tua rosa invece di quello dell'intera lega.")

    if request.POST.get("action") == "preview":
        return JsonResponse({
            "ok": True,
            "teams": [
                {"name": t["name"], "credits": t["credits"], "n_players": len(t["players"]),
                 "players": t["players"][:5]}
                for t in teams
            ],
            "total_players": sum(len(t["players"]) for t in teams),
            "warning": warning,
            "download_log": download_log if warning else "",
        })

    league, denied = _import_league_or_403(request)
    if denied:
        return denied
    result = importers.import_rose_data(teams, replace=replace, league=league)
    return JsonResponse({"ok": True, **result, "warning": warning})
