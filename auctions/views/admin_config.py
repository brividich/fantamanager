"""Configuration and session management views."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import services
from ..models import Auction, AuctionSession, League
from .admin_wizards import _game_mode
from .common import current_auction, managed_or_403, staff_member_required, target_league


@staff_member_required
def admin_config(request):
    """Leghe, aste e sessioni salvate in un posto solo, con i tasti per fare
    ordine: rinominare/riconfigurare una lega, cancellare un'asta o una lega
    intera, ripulire i duplicati rimasti da riprese andate storte."""
    leagues = list(League.objects.all())
    current_league = target_league(request)

    rows = services.league_overview()
    auctions = list(Auction.objects.select_related("league").order_by("-id"))
    for a in auctions:
        a.bid_count = a.bids.count()
    sessions = []
    for s in AuctionSession.objects.select_related("league").order_by("-created_at"):
        data = s.data or {}
        s.n_teams = len(data.get("participants") or [])
        s.n_pool = len(data.get("pool") or [])
        sessions.append(s)

    empties = [r["league"].id for r in rows if not r["pool"] and not r["auctions"]]

    return render(request, "auctions/admin_config.html", {
        "leagues": leagues,
        "current_league": current_league,
        "rows": rows,
        "auctions": auctions,
        "sessions": sessions,
        "empties": empties,
        "console_section": "Configurazione",
        "console_active": "config",
        "selected": current_auction(request, current_league),
    })


@staff_member_required
@require_POST
def admin_config_action(request):
    """One POST endpoint for the config page: delete / rename / clean up."""
    action = request.POST.get("action", "")
    back = redirect("admin_config")

    if action == "delete_league":
        report = services.delete_league(request.POST.get("league_id"))
        if report:
            messages.success(request, (
                f"Lega «{report['name']}» eliminata: {report['auctions']} aste, "
                f"{report['teams']} squadre, {report['players']} giocatori, "
                f"{report['sessions']} sessioni."))
        else:
            messages.error(request, "Lega non trovata.")

    elif action == "delete_auction":
        report = services.delete_auction(request.POST.get("auction_id"))
        if report:
            messages.success(request, (
                f"Asta «{report['title']}» eliminata ({report['bids']} offerte). "
                "Lega, squadre e listone restano."))
        else:
            messages.error(request, "Asta non trovata.")

    elif action == "delete_session":
        session = AuctionSession.objects.filter(pk=request.POST.get("session_id")).first()
        if session:
            name = session.name
            session.delete()
            messages.success(request, f"Sessione «{name}» eliminata.")
        else:
            messages.error(request, "Sessione non trovata.")

    elif action == "clean_empty":
        gone = []
        for row in services.league_overview():
            if not row["pool"] and not row["auctions"]:
                report = services.delete_league(row["league"].id)
                if report:
                    gone.append(report["name"])
        messages.success(
            request,
            f"Ripulite {len(gone)} leghe vuote: {', '.join(gone)}." if gone
            else "Nessuna lega vuota da ripulire.")

    elif action == "rename_league":
        league = League.objects.filter(pk=request.POST.get("league_id")).first()
        if league is None:
            messages.error(request, "Lega non trovata.")
        else:
            def pint(name, default):
                try:
                    return max(0, int(request.POST.get(name) or default))
                except (TypeError, ValueError):
                    return default
            league.name = (request.POST.get("name") or league.name).strip()[:120]
            try:
                league.budget = Decimal(str(request.POST.get("budget") or league.budget))
            except (InvalidOperation, ValueError):
                pass
            league.slot_limits = request.POST.get("slot_limits", "1") != "0"
            league.slots_p = pint("slots_p", league.slots_p)
            league.slots_d = pint("slots_d", league.slots_d)
            league.slots_c = pint("slots_c", league.slots_c)
            league.slots_a = pint("slots_a", league.slots_a)
            league.game_mode = _game_mode(request.POST.get("game_mode"), league.game_mode)
            league.slots_gk = pint("slots_gk", league.slots_gk)
            league.slots_out = pint("slots_out", league.slots_out)
            league.save()
            messages.success(request, f"Lega «{league.name}» aggiornata.")

    else:
        messages.error(request, "Azione sconosciuta.")

    return back


@staff_member_required
def admin_sessions(request):
    """List saved sessions (GET) for browsing and resuming."""
    leagues = list(League.objects.all())
    current_league = leagues[0] if leagues else None
    return render(request, "auctions/admin_sessions.html", {
        "sessions": AuctionSession.objects.select_related("league", "source_auction"),
        "leagues": leagues,
        "current_league": current_league,
        "selected": current_auction(request, current_league),
        "console_section": "Sessioni salvate",
        "console_active": "",
    })


@staff_member_required
@require_POST
def admin_save_session(request, auction_id):
    """Snapshot the current league standings of an auction into a session."""
    auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    session = services.save_session(
        auction.id,
        name=request.POST.get("name", "").strip(),
        created_by=request.user.get_username(),
        notes=request.POST.get("notes", "").strip(),
    )
    return redirect(f"/admin-auction/sessions/?saved={session.id}")


def _after_resume(auction):
    """Where to land after rebuilding an auction from a session."""
    if not services.listone_loaded(auction):
        return redirect(
            f"{reverse('admin_players')}?league={auction.league_id}&need_listone=1&from=resume"
        )
    return redirect(f"/admin-auction/?auction={auction.id}")


@staff_member_required
@require_POST
def admin_resume_session(request, session_id):
    """Rebuild a fresh, playable auction from a saved session."""
    get_object_or_404(AuctionSession, pk=session_id)
    auction = services.resume_session(session_id, created_by=request.user.get_username())
    return _after_resume(auction)


@staff_member_required
@require_POST
def admin_resume_latest(request):
    """One-click 'Riprendi sessione' — resume the most recent saved session."""
    latest = AuctionSession.objects.order_by("-created_at").first()
    if latest is None:
        return redirect("admin_sessions")
    auction = services.resume_session(latest.id, created_by=request.user.get_username())
    return _after_resume(auction)
