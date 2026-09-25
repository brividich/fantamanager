"""Admin player management: listone, imports, assignments, and search."""
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from ..models import Auction, League, LeagueConfig, Participant, Player
from ..providers import importers
from .. import services
from .common import (
    broadcast_state,
    current_auction,
    current_league,
    staff_member_required,
    target_league,
)


@staff_member_required
def admin_players(request):
    """The listone of one league, filtered and paginated server-side.

    500+ players used to render as one enormous page (tens of thousands of DOM
    nodes). Search, role, team and status now filter in the database and only a
    page of rows is sent; every filter travels in the querystring so pagination
    links keep them — the league included.
    """
    leagues = list(League.objects.all())
    league = current_league(request)

    all_players = Player.objects.filter(league=league) if league else Player.objects.filter(league__isnull=True)
    counts  = {r: all_players.filter(role=r).count() for r in ["P", "D", "C", "A"]}
    free_count = all_players.filter(owner__isnull=True).count()
    total_count = all_players.count()

    q      = (request.GET.get("q") or "").strip()
    role   = (request.GET.get("role") or "").strip().upper()[:1]
    team   = (request.GET.get("team") or "").strip()
    status = (request.GET.get("status") or "").strip()
    sort   = (request.GET.get("sort") or "name").strip()

    players = all_players.select_related("owner")
    if q:
        players = players.filter(Q(name__icontains=q) | Q(team__icontains=q))
    if role in ("P", "D", "C", "A"):
        players = players.filter(role=role)
    if team:
        players = players.filter(team=team)
    if status == "free":
        players = players.filter(owner__isnull=True)
    elif status == "owned":
        players = players.filter(owner__isnull=False)

    ORDERS = {
        "name":  ["name"],
        "-name": ["-name"],
        "quota": ["initial_price", "name"],
        "-quota": ["-initial_price", "name"],
        "role":  ["role", "name"],
        "team":  ["team", "name"],
    }
    players = players.order_by(*ORDERS.get(sort, ORDERS["name"]))

    paginator = Paginator(players, 50)
    page = paginator.get_page(request.GET.get("page"))

    # Querystring without page/league, so the pager can re-append them.
    keep = request.GET.copy()
    for k in ("page",):
        keep.pop(k, None)
    filter_qs = keep.urlencode()

    teams = list(all_players.exclude(team="").order_by("team")
                 .values_list("team", flat=True).distinct())

    # Teams available as assignment targets (scoped to the current league).
    participants = Participant.objects.filter(is_active=True)
    participants = participants.filter(league=league) if league else participants
    return render(request, "auctions/admin_players.html", {
        "players": page.object_list, "page_obj": page, "paginator": paginator,
        "counts": counts, "free_count": free_count, "total_count": total_count,
        "filters": {"q": q, "role": role, "team": team, "status": status, "sort": sort},
        "filter_qs": filter_qs, "teams": teams,
        "leagues": leagues, "current_league": league,
        "participants": participants.order_by("display_name"),
        "photo_template_default": importers.FANTACALCIO_PHOTO_TEMPLATE,
        # The stats card reports coverage rather than offering a blind upload:
        # the season file ships with the app and an import applies it by itself,
        # so what a league actually needs to know is how many cards came out full.
        "stats_season": importers.BUNDLED_STATS_SEASON,
        "stats_covered": all_players.exclude(fanta_avg__isnull=True).count(),
        "console_section": "Giocatori",
        "console_active": "players",
        # Keeps the "Asta" tab pointing back at the auction being run.
        "selected": current_auction(request, league),
        # Set when the wizard (or a resumed session) bounced here: this league
        # has no listone yet.
        "need_listone": request.GET.get("need_listone") == "1",
        "need_listone_from": request.GET.get("from", ""),
    })


@staff_member_required
@require_POST
def admin_apply_photos(request):
    """Populate Player.photo_url from a URL template (default: Fantacalcio.it).

    Optional ``listone_file`` first backfills missing ``ext_id`` by name match
    (so a pool imported before ids were captured still gets photos). ``template``
    overrides the default pattern; ``only_missing=0`` re-applies to everyone."""
    league = League.objects.filter(pk=request.POST.get("league_id")).first()
    template = (request.POST.get("template") or "").strip() or importers.FANTACALCIO_PHOTO_TEMPLATE
    only_missing = request.POST.get("only_missing", "1") == "1"

    backfilled = 0
    f = request.FILES.get("listone_file")
    if f is not None:
        rows, _errors = importers.parse_listone_file(f, f.name)
        backfilled = importers.backfill_ext_ids(rows, league=league)

    report = importers.apply_photos(league=league, template=template, only_missing=only_missing)
    report["ok"] = True
    report["backfilled_ext_ids"] = backfilled
    report["template"] = template
    return JsonResponse(report)


@staff_member_required
@require_POST
def admin_import_stats(request):
    """Merge a Fantacalcio "Statistiche" file onto the pool (season numbers).

    Additive: it only sets the stat fields (presences/media voto/fantamedia/
    goals/assists) on players already imported, matching by official id then by
    name. The base card still works for pools without a stats file.

    With no upload it re-applies the season file shipped inside the app — the
    same one an import seeds automatically. That is the button a league presses
    after fixing up the listone by hand; uploading a newer export overrides it.
    """
    league = League.objects.filter(pk=request.POST.get("league_id")).first()
    f = request.FILES.get("stats_file")
    if f:
        rows, errors = importers.parse_stats_file(f, f.name)
        source = f.name
    else:
        rows, errors = importers.bundled_stats_rows()
        source = f"Statistiche {importers.BUNDLED_STATS_SEASON} incluse"
        if not rows:
            return JsonResponse(
                {"ok": False, "error": "Statistiche incluse non disponibili."}, status=500)
    report = importers.import_stats(rows, league=league)
    report["ok"] = True
    report["source"] = source
    report["errors"] = errors[:20]
    return JsonResponse(report)


@staff_member_required
@require_POST
def admin_import_players(request):
    f = request.FILES.get("csv_file")
    if not f:
        return JsonResponse({"ok": False, "error": "Nessun file"}, status=400)

    replace = request.POST.get("replace") == "1"
    # When syncing (not replacing) the listone, drop free agents who are no
    # longer listed (left Serie A). Defaults on; owned players are never pruned.
    prune = request.POST.get("prune", "1") == "1"
    # Import into a specific league's pool (None = legacy/global pool).
    league = League.objects.filter(pk=request.POST.get("league_id")).first()

    parsed, errors = importers.parse_listone_file(f, f.name)

    # Reconcile against the existing pool: matches existing players (preserving
    # roster ownership), creates free agents for the rest, and prunes departed
    # free agents — instead of blindly creating duplicate rows.
    report = importers.sync_players(parsed, league=league, replace=replace, prune=prune)
    return JsonResponse({
        "ok": True,
        "created": report["created"],
        "updated": report["updated"],
        "matched_owned": report["matched_owned"],
        "pruned": report["pruned"],
        "owned_not_in_listone": report["owned_not_in_listone"][:50],
        "errors": errors[:10],
    })


_ROSE_SOURCE_LABELS = {
    "fantapazz": "Fantapazz", "fantacalcio": "Fantacalcio.it", "generic": "Generico",
    "leghe_fantacalcio_id": "Leghe Fantacalcio",
}


def _resolve_rose_league(request):
    """League an import writes into. The rose/players UI posts a FantaManager pk
    in ``league_id`` (unlike the Fantapazz flow, which posts an external id), so
    resolve by pk first and fall back to the only league when there is exactly one."""
    league = League.objects.filter(pk=request.POST.get("league_id")).first()
    if league is not None:
        return league
    leagues = League.objects.all()[:2]
    return leagues[0] if len(leagues) == 1 else None


@staff_member_required
@require_POST
def admin_import_rose(request):
    """Import team rosters (rose) from any supported export, auto-detecting the
    source (Fantapazz / Fantacalcio.it / generic). ``action=preview`` only reports
    what was found; otherwise it assigns ownership onto the listone pool.

    When the file also carries a full listone (the Fantacalcio.it flat export
    has a ``QUOT.`` column) it is synced first — non-destructively — so the free
    agents (svincolati) still appear at the auction."""
    f = request.FILES.get("rose_file")
    if not f:
        return JsonResponse({"ok": False, "error": "Nessun file"}, status=400)

    league = _resolve_rose_league(request)
    try:
        teams, listone, meta = importers.parse_rose_file(f, f.name, league=league)
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)

    if not teams:
        return JsonResponse({"ok": False, "error": "Nessuna squadra trovata nel file."}, status=400)

    source_label = _ROSE_SOURCE_LABELS.get(meta["source"], meta["source"])
    warning = ""
    if len(teams) <= 1:
        warning = ("Attenzione: nel file c'è una sola squadra. Verifica di aver "
                   "esportato le rose dell'intera lega e non solo la tua.")
    unmatched = meta.get("unmatched") or []
    if unmatched:
        names = ", ".join(sorted({row[0] for row in unmatched}))
        warning = (warning + " " if warning else "") + (
            f"{len(unmatched)} giocatori con Id non trovato nel listone di questa "
            f"lega (squadre coinvolte: {names}) — non sono stati importati. "
            "Verifica di aver caricato il listone giusto per questa lega."
        )

    if request.POST.get("action") == "preview":
        return JsonResponse({
            "ok": True, "preview": True,
            "source": meta["source"], "source_label": source_label,
            "teams": [
                {"name": t["name"], "credits": t["credits"], "n_players": len(t["players"]),
                 "players": t["players"][:5]}
                for t in teams
            ],
            "total_players": meta["n_players"],
            "n_listone": len(listone) if listone else 0,
            "warning": warning,
        })

    replace = request.POST.get("replace") == "1"
    budget  = league.budget if league is not None else LeagueConfig.get().budget

    listone_report = None
    # Default on: feed the file's listone so svincolati exist at the auction.
    if listone and request.POST.get("import_listone", "1") == "1":
        listone_report = importers.sync_players(listone, league=league, replace=False, prune=False)

    result = importers.import_rose_data(
        teams, replace=replace, league=league, default_budget=budget,
    )
    return JsonResponse({
        "ok": True,
        "source": meta["source"], "source_label": source_label,
        "teams": result["teams"], "players": result["players"],
        "listone_created": (listone_report or {}).get("created", 0),
        "warning": warning,
    })


@staff_member_required
@require_POST
def admin_delete_player(request, player_id):
    Player.objects.filter(pk=player_id).delete()
    return JsonResponse({"ok": True})


@staff_member_required
@require_POST
def admin_clear_players(request):
    """Wipe a league's pool (or the global pool when no league is given)."""
    if request.POST.get("league_id"):
        league = League.objects.filter(pk=request.POST.get("league_id")).first()
        Player.objects.filter(league=league).delete()
    else:
        Player.objects.all().delete()
    return JsonResponse({"ok": True})


@staff_member_required
@require_POST
def admin_release_player(request, player_id):
    """Admin svincolo: free any owned player, refunding per the auction policy."""
    auction_id = request.POST.get("auction_id") or None
    result = services.release_player(player_id, auction_id=auction_id, by_admin=True)
    if result.get("ok") and auction_id:
        auction = Auction.objects.filter(pk=auction_id).first()
        if auction is not None:
            broadcast_state(auction)
    return JsonResponse(result, status=200 if result.get("ok") else 400)


@staff_member_required
@require_POST
def admin_assign_player(request, player_id):
    """Admin manual assign / re-assign / price-correction of a player.

    POST ``participant_id`` (required), optional ``price`` (defaults to the
    player's listone quotazione) and ``note``. Broadcasts state when an
    ``auction_id`` is supplied so the live screens reflect the change."""
    participant_id = request.POST.get("participant_id")
    if not participant_id:
        return JsonResponse({"ok": False, "error": "Nessuna squadra selezionata"}, status=400)
    price = request.POST.get("price") or None
    result = services.assign_player(
        player_id, participant_id, price=price, by_admin=True,
        note=request.POST.get("note", ""),
    )
    auction_id = request.POST.get("auction_id") or None
    if result.get("ok") and auction_id:
        auction = Auction.objects.filter(pk=auction_id).first()
        if auction is not None:
            broadcast_state(auction)
    return JsonResponse(result, status=200 if result.get("ok") else 400)


@staff_member_required
def admin_player_search(request):
    """Free agents matching ``?q=`` in the console's league — max 20 rows.

    Rendering the whole listone into the page cost hundreds of <tr> on every
    load; the "prossimo giocatore" box now asks for what the admin is typing.
    Accent-blind, because mid-auction nobody reaches for the accented key.
    """
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"ok": True, "results": [], "total": 0})

    league = target_league(request)
    qs = Player.objects.filter(owner__isnull=True)
    qs = qs.filter(league=league) if league is not None else qs.filter(league__isnull=True)

    needle = importers.normalize_name(q)
    rows, total = [], 0
    for p in qs.order_by("role", "name").only(
            "id", "name", "role", "team", "initial_price"):
        if needle not in importers.normalize_name(p.name) \
                and needle not in importers.normalize_name(p.team or ""):
            continue
        total += 1
        if len(rows) < 20:
            rows.append({
                "id": p.id, "name": p.name, "role": p.role,
                "team": p.team, "quotation": float(p.initial_price),
            })
    return JsonResponse({"ok": True, "results": rows, "total": total})
