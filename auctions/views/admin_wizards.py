"""Setup and creation wizards for leagues and auctions."""
import json
import os
import secrets as _sec
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import services
from ..models import Auction, League, LeagueConfig, Participant
from ..providers import importers
from .admin_dashboard import _pint
from .common import (
    _call_order,
    _flow_mode,
    _opening_price_mode,
    _refund_mode,
    _within_role,
    staff_member_required,
)


def _sealed_settings(request):
    """I campi dell'asta alle buste come li posta un wizard di creazione."""
    return {
        "sealed_bids": request.POST.get("sealed_bids") == "1",
        "sealed_threshold_p": _pint(request.POST.get("sealed_threshold_p"), 50),
        "sealed_threshold_d": _pint(request.POST.get("sealed_threshold_d"), 50),
        "sealed_threshold_c": _pint(request.POST.get("sealed_threshold_c"), 100),
        "sealed_threshold_a": _pint(request.POST.get("sealed_threshold_a"), 150),
        "sealed_seconds": max(5, _pint(request.POST.get("sealed_seconds"), 45)),
    }


def _game_mode(raw, fallback=None):
    """Legge il sistema di gioco dal form: solo CLASSIC o MANTRA."""
    value = (raw or "").strip().upper()
    if value in League.GameMode.values:
        return value
    return fallback if fallback is not None else League.GameMode.CLASSIC


# --- Auction creation wizard ------------------------------------------------

@staff_member_required
def admin_auction_wizard(request):
    """Render the "new auction" wizard (GET)."""
    modes = [
        (v, l) for v, l in Auction.Mode.choices
        if v != Auction.Mode.RESUME_SAVED
    ]
    leagues = list(League.objects.all())
    for lg in leagues:
        lg.team_count = lg.participants.count()
        lg.pool_count = lg.players.count()
        lg.free_count = lg.players.filter(owner__isnull=True).count()
    try:
        preselect = int(request.GET.get("league") or 0)
    except (TypeError, ValueError):
        preselect = 0
    return render(request, "auctions/auction_wizard.html", {
        "modes": modes,
        "leagues": leagues,
        "preselect": preselect,
        "flow_modes": Auction.FlowMode.choices,
        "call_orders": Auction.CallOrder.choices,
        "within_roles": Auction.WithinRole.choices,
        "opening_price_modes": Auction.OpeningPriceMode.choices,
    })


@staff_member_required
@require_POST
def admin_wizard_create(request):
    """Create an Auction on an *existing* league from the wizard payload (POST)."""
    def dec(name, default):
        try:
            return Decimal(str(request.POST.get(name) or default))
        except (InvalidOperation, ValueError):
            return Decimal(str(default))

    def pint(name, default):
        try:
            return max(0, int(request.POST.get(name) or default))
        except (TypeError, ValueError):
            return int(default)

    league_id = request.POST.get("league_id")
    league = League.objects.filter(pk=league_id).first() if league_id else None
    if league is None:
        return redirect("admin_create_league")

    if not league.players.exists():
        return redirect(f"{reverse('admin_players')}?league={league.id}&need_listone=1&from=wizard")

    mode = request.POST.get("mode", "").strip()
    if mode not in Auction.Mode.values:
        mode = Auction.Mode.NEW_FROM_ZERO

    start_live = request.POST.get("start_now") == "1"
    auction = Auction.objects.create(
        league=league,
        title=request.POST.get("title", "").strip() or league.name,
        mode=mode,
        source_site=league.source_site,
        source_league_id=league.external_id,
        starting_price=Decimal("1"),
        current_price=Decimal("1"),
        min_increment=dec("min_increment", "1"),
        quick_increments=request.POST.get("quick_increments", "1,2,5,10").strip() or "1,2,5,10",
        duration_seconds=pint("duration_seconds", 60),
        antisnipe_seconds=pint("antisnipe_seconds", 10),
        enforce_limits=request.POST.get("enforce_limits", "1") == "1",
        block_leader_rebid=request.POST.get("block_leader_rebid", "1") == "1",
        release_refund_mode=_refund_mode(request),
        opening_price_mode=_opening_price_mode(request),
        flow_mode=_flow_mode(request),
        call_order=_call_order(request),
        within_role_order=_within_role(request),
        manual_auto_advance=request.POST.get("manual_auto_advance") == "1",
        status=Auction.Status.READY,
        **_sealed_settings(request),
    )

    if auction.is_ordered_flow:
        services.build_queue(auction)

    if start_live:
        services.start_auction(auction.id)

    return redirect(f"/regia/{auction.id}/")


# --- League creation --------------------------------------------------------

@staff_member_required
def admin_create_league(request):
    """Create a *League* — the season-long container."""
    if request.method == "GET":
        cfg = LeagueConfig.get()
        return render(request, "auctions/league_form.html", {
            "cfg": cfg,
            "free_teams": Participant.objects.filter(league__isnull=True).order_by("display_name"),
            "leagues": League.objects.all(),
        })

    def dec(name, default):
        try:
            return Decimal(str(request.POST.get(name) or default))
        except (InvalidOperation, ValueError):
            return Decimal(str(default))

    def pint(name, default):
        try:
            return max(0, int(request.POST.get(name) or default))
        except (TypeError, ValueError):
            return int(default)

    budget = dec("budget", "500")
    league = League.objects.create(
        name=request.POST.get("name", "").strip() or "Lega",
        owner=request.user if request.user.is_authenticated else None,
        source_site=request.POST.get("source_site", "").strip(),
        external_id=request.POST.get("external_id", "").strip(),
        budget=budget,
        slot_limits=request.POST.get("slot_limits", "1") != "0",
        slots_p=pint("slots_p", 3),
        slots_d=pint("slots_d", 8),
        slots_c=pint("slots_c", 8),
        slots_a=pint("slots_a", 6),
        game_mode=_game_mode(request.POST.get("game_mode")),
        slots_gk=pint("slots_gk", 3),
        slots_out=pint("slots_out", 22),
    )

    cfg = LeagueConfig.get()
    cfg.name, cfg.budget = league.name, league.budget
    cfg.slot_limits = league.slot_limits
    cfg.slots_p, cfg.slots_d = league.slots_p, league.slots_d
    cfg.slots_c, cfg.slots_a = league.slots_c, league.slots_a
    cfg.game_mode = league.game_mode
    cfg.slots_gk, cfg.slots_out = league.slots_gk, league.slots_out
    cfg.save()

    try:
        manual = json.loads(request.POST.get("participants_json") or "[]")
    except (ValueError, TypeError):
        manual = []
    for p in manual:
        name = (p.get("name") or "").strip()[:80]
        if not name:
            continue
        try:
            credits = Decimal(str(p.get("credits"))) if p.get("credits") not in (None, "") else budget
        except (InvalidOperation, ValueError):
            credits = budget
        Participant.objects.create(
            league=league, display_name=name,
            access_code=_sec.token_hex(4), credits=credits, is_active=True,
        )

    attach_ids = [i for i in request.POST.getlist("attach_ids") if i.isdigit()]
    if attach_ids:
        Participant.objects.filter(pk__in=attach_ids).update(league=league)

    return redirect(f"/dashboard/{league.id}/")


# --- Unified setup wizard ---------------------------------------------------

def _fp_rose_ready(request):
    token = request.session.get("fp_sync_token", "")
    if not token:
        return False
    path = os.path.join(str(settings.MEDIA_ROOT), f"fp_rose_{token}.xls")
    return os.path.exists(path)


def _setup_wizard_context(request, error=""):
    return {
        "cfg": LeagueConfig.get(),
        "free_teams": Participant.objects.filter(league__isnull=True).order_by("display_name"),
        "modes": [(v, l) for v, l in Auction.Mode.choices if v != Auction.Mode.RESUME_SAVED],
        "flow_modes": Auction.FlowMode.choices,
        "call_orders": Auction.CallOrder.choices,
        "within_roles": Auction.WithinRole.choices,
        "opening_price_modes": Auction.OpeningPriceMode.choices,
        "has_fp_rose": _fp_rose_ready(request),
        "error": error,
    }


def _import_rose_into_league(request, league, *, source):
    raw = None
    upload = request.FILES.get("rose_file")
    if upload:
        raw = upload.read()
    elif source == "fantapazz":
        token = request.session.get("fp_sync_token", "")
        path = os.path.join(str(settings.MEDIA_ROOT), f"fp_rose_{token}.xls") if token else ""
        if path and os.path.exists(path):
            with open(path, "rb") as fh:
                raw = fh.read()
    if not raw:
        return None
    teams = importers.parse_rose_xls(raw)
    if not teams:
        return None
    return importers.import_rose_data(teams, league=league)


def _import_leghe_rose_into_league(request, league):
    upload = request.FILES.get("rose_file")
    if not upload:
        return None
    try:
        teams, _listone, meta = importers.parse_rose_file(upload, upload.name, league=league)
    except Exception:
        return None
    if not teams:
        return None
    result = importers.import_rose_data(teams, league=league)
    if meta.get("unmatched"):
        result = dict(result)
        result["unmatched"] = len(meta["unmatched"])
    return result


def _parse_listone_upload(request):
    upload = request.FILES.get("listone_file")
    if not upload:
        return [], ("Il listone (Quotazioni) è obbligatorio: carica il file "
                    "ufficiale .xlsx/.xls oppure un .csv con Nome, Ruolo, Squadra, Quotazione.")
    try:
        rows, _errors = importers.parse_listone_file(upload, upload.name)
    except Exception as exc:
        return [], f"Listone non leggibile: {exc}"
    if not rows:
        return [], "Nessun giocatore trovato nel listone caricato. Controlla il file e riprova."
    return rows, ""


def _import_listone_into_league(rows, league, *, replace=True):
    if not rows:
        return None
    return importers.sync_players(rows, league=league, replace=replace, prune=False)


@staff_member_required
def admin_setup(request):
    """Render the unified 'Nuova lega' wizard (GET)."""
    return render(request, "auctions/setup_wizard.html", _setup_wizard_context(request))


@staff_member_required
@require_POST
def admin_setup_create(request):
    """Build League + teams + imported rosters + Auction from the unified wizard."""
    def dec(name, default):
        try:
            return Decimal(str(request.POST.get(name) or default))
        except (InvalidOperation, ValueError):
            return Decimal(str(default))

    def pint(name, default):
        try:
            return max(0, int(request.POST.get(name) or default))
        except (TypeError, ValueError):
            return int(default)

    listone_rows, listone_error = _parse_listone_upload(request)
    if listone_error:
        return render(request, "auctions/setup_wizard.html",
                      _setup_wizard_context(request, error=listone_error), status=400)

    budget = dec("budget", "500")
    source_site = request.POST.get("source_site", "").strip()
    slot_limits = request.POST.get("slot_limits", "1") != "0"
    league = League.objects.create(
        name=request.POST.get("name", "").strip() or "Lega",
        owner=request.user if request.user.is_authenticated else None,
        source_site=source_site,
        external_id=request.POST.get("external_id", "").strip(),
        budget=budget,
        slot_limits=slot_limits,
        slots_p=pint("slots_p", 3),
        slots_d=pint("slots_d", 8),
        slots_c=pint("slots_c", 8),
        slots_a=pint("slots_a", 6),
        game_mode=_game_mode(request.POST.get("game_mode")),
        slots_gk=pint("slots_gk", 3),
        slots_out=pint("slots_out", 22),
    )

    cfg = LeagueConfig.get()
    cfg.name, cfg.budget = league.name, league.budget
    cfg.slot_limits = league.slot_limits
    cfg.slots_p, cfg.slots_d = league.slots_p, league.slots_d
    cfg.slots_c, cfg.slots_a = league.slots_c, league.slots_a
    cfg.game_mode = league.game_mode
    cfg.slots_gk, cfg.slots_out = league.slots_gk, league.slots_out
    cfg.save()

    try:
        manual = json.loads(request.POST.get("participants_json") or "[]")
    except (ValueError, TypeError):
        manual = []
    for p in manual:
        name = (p.get("name") or "").strip()[:80]
        if not name:
            continue
        try:
            credits = Decimal(str(p.get("credits"))) if p.get("credits") not in (None, "") else budget
        except (InvalidOperation, ValueError):
            credits = budget
        Participant.objects.create(
            league=league, display_name=name,
            access_code=_sec.token_hex(4), credits=credits, is_active=True,
        )
    attach_ids = [i for i in request.POST.getlist("attach_ids") if i.isdigit()]
    if attach_ids:
        Participant.objects.filter(pk__in=attach_ids).update(league=league)

    import_choice = request.POST.get("import_choice", "none")
    import_report = None
    if import_choice == "leghe":
        _import_listone_into_league(listone_rows, league, replace=True)
        import_report = _import_leghe_rose_into_league(request, league)
    else:
        if import_choice in ("fantapazz", "excel"):
            import_report = _import_rose_into_league(request, league, source=import_choice)
        _import_listone_into_league(listone_rows, league, replace=(import_report is None))

    mode = request.POST.get("mode", "").strip()
    if mode not in Auction.Mode.values:
        mode = Auction.Mode.NEW_FROM_ZERO

    auction = Auction.objects.create(
        league=league,
        title=request.POST.get("title", "").strip() or league.name,
        mode=mode,
        source_site=league.source_site,
        source_league_id=league.external_id,
        starting_price=Decimal("1"),
        current_price=Decimal("1"),
        min_increment=dec("min_increment", "1"),
        quick_increments=request.POST.get("quick_increments", "1,2,5,10").strip() or "1,2,5,10",
        duration_seconds=pint("duration_seconds", 60),
        antisnipe_seconds=pint("antisnipe_seconds", 10),
        enforce_limits=request.POST.get("enforce_limits", "1") == "1",
        block_leader_rebid=request.POST.get("block_leader_rebid", "1") == "1",
        release_refund_mode=_refund_mode(request),
        opening_price_mode=_opening_price_mode(request),
        flow_mode=_flow_mode(request),
        call_order=_call_order(request),
        within_role_order=_within_role(request),
        manual_auto_advance=request.POST.get("manual_auto_advance") == "1",
        status=Auction.Status.READY,
        **_sealed_settings(request),
    )
    if auction.is_ordered_flow:
        services.build_queue(auction)
    if request.POST.get("start_now") == "1":
        services.start_auction(auction.id)

    try:
        services.save_session(
            auction.id,
            name=league.name,
            created_by=request.user.username if request.user.is_authenticated else "",
            notes="Salvataggio automatico alla creazione",
        )
    except Exception:
        pass

    return redirect(f"/regia/{auction.id}/")


@staff_member_required
@require_POST
def admin_setup_analyze(request):
    """Inspect an uploaded source file and report what would be imported."""
    rose = request.FILES.get("rose_file")
    listone = request.FILES.get("listone_file")

    if rose:
        id_teams = importers.preview_id_based_roster(rose, rose.name)
        if id_teams is not None:
            return JsonResponse({
                "ok": True, "kind": "rose", "id_based": True,
                "teams": [{"name": t["name"], "n_players": t["n_players"], "credits": None}
                          for t in id_teams],
                "total_players": sum(t["n_players"] for t in id_teams),
                "suggested_budget": None,
            })

        rose.seek(0)
        try:
            teams = importers.parse_rose_xls(rose.read())
        except Exception as e:
            return JsonResponse({"ok": False, "error": f"File non leggibile: {e}"})
        if not teams:
            return JsonResponse({"ok": False, "error": "Nessuna squadra trovata nel file."})
        out_teams, max_total = [], Decimal("0")
        for t in teams:
            players = t.get("players", [])
            spent = sum(Decimal(str(p.get("cost", 0) or 0)) for p in players)
            remaining = t.get("credits")
            total = (Decimal(str(remaining)) + spent) if remaining is not None else spent
            max_total = max(max_total, total)
            out_teams.append({
                "name": t.get("name", ""),
                "n_players": len(players),
                "credits": float(remaining) if remaining is not None else None,
            })
        return JsonResponse({
            "ok": True, "kind": "rose",
            "teams": out_teams,
            "total_players": sum(t["n_players"] for t in out_teams),
            "suggested_budget": int(max_total) if max_total else None,
        })

    if listone:
        try:
            rows, _errors = importers.parse_listone_file(listone, listone.name)
        except Exception as e:
            return JsonResponse({"ok": False, "error": f"File non leggibile: {e}"})
        if not rows:
            return JsonResponse({"ok": False, "error": "Nessun giocatore trovato nel file."})
        roles = {"P": 0, "D": 0, "C": 0, "A": 0}
        for r in rows:
            roles[r.get("role", "A")] = roles.get(r.get("role", "A"), 0) + 1
        return JsonResponse({
            "ok": True, "kind": "listone",
            "total_players": len(rows), "roles": roles,
        })

    return JsonResponse({"ok": False, "error": "Nessun file caricato."})
