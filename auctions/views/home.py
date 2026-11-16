"""Home landing portal: Chooser & Login before Dashboard."""
from django.shortcuts import redirect, render
from django.urls import reverse
from ..models import Auction
from .. import remote
from ..services.onboarding import pending_co_invites
from .common import _session_participant, manageable_leagues, safe_next, target_league, try_regia_pin, visible_leagues


def home_portal(request):
    """Initial landing portal of the application.
    
    Provides the dual-path choice:
    1. Free Area (Asta Live, Maxischermo, App Allenatori) — no login needed.
    2. Managerial Area (Regia, Command Center / Dashboard) — PIN/login protected.
    """
    visible = visible_leagues(request)
    leagues = list(visible.order_by("name"))
    participant = _session_participant(request)
    if participant and participant.league:
        current_league = participant.league
    else:
        current_league = target_league(request)
    # ?league= would otherwise open anyone's league, live auction and
    # maxischermo token included.
    if current_league is not None and not visible.filter(pk=current_league.pk).exists():
        current_league = None

    if current_league is not None:
        auctions = list(Auction.objects.filter(league=current_league).order_by("-id"))
        active_auction = next(
            (a for a in auctions if a.status in [Auction.Status.LIVE, Auction.Status.PAUSED]),
            None
        )
    else:
        auctions = []
        active_auction = None

    user = getattr(request, "user", None)
    has_admin = bool(user and user.is_authenticated and (
        user.is_superuser or user.is_staff or manageable_leagues(user).exists()
    ))
    is_unlocked = bool(request.session.get("regia_unlocked", False)) or has_admin
    is_mobile = getattr(request, "is_mobile", False)

    # Smartphone visitors are directed straight to the App unless they are an admin
    # or explicitly using the PIN gate to unlock the Regia console.
    if is_mobile and request.method == "GET" and not is_unlocked:
        if not has_admin and not request.GET.get("admin"):
            if participant:
                return redirect("app_home")
            return redirect("app_login")

    error = ""

    if request.method == "POST":
        action = request.POST.get("action", "login")
        if action == "logout":
            request.session.pop("regia_unlocked", None)
            return redirect("home_portal")

        # Same gate, same lockout as /regia/unlock/: only the minted tunnel PIN.
        error = try_regia_pin(request)
        if not error:
            return redirect(safe_next(request, reverse("dashboard")))

    return render(request, "auctions/home_portal.html", {
        "my_team": _my_team_card(participant),
        "leagues": leagues,
        "current_league": current_league,
        "auctions": auctions,
        "active_auction": active_auction,
        "is_unlocked": is_unlocked,
        "has_admin": has_admin,
        "error": error,
        "is_remote": remote.request_is_remote(request),
        "co_invites": pending_co_invites(user),
    })


def _my_team_card(participant):
    """Il riquadro «La tua squadra» del portale da PC: chi ha una squadra
    arriva qui dopo l'accesso, e da qui va al mercato aperto, alla rosa e
    alla formazione (le pagine dell'app, che funzionano anche da PC)."""
    if participant is None or participant.league_id is None:
        return None
    from .. import services
    from ..models import MarketBid, MarketSession
    league = participant.league
    services.sync_market_schedule(league)
    market = next((s for s in MarketSession.objects.filter(league=league, status=MarketSession.Status.OPEN)
                   if s.is_open), None)
    # Buste scrutinate in cui la squadra deve ancora scegliere chi esce.
    releases = None
    for done in MarketSession.objects.filter(league=league,
                                             status=MarketSession.Status.RESOLVED).order_by("-updated_at")[:5]:
        if services.pending_releases(done, participant.id):
            releases = done
            break
    return {
        "team": participant,
        "league": league,
        "market": market,
        "is_buste": bool(market and market.session_type in (MarketSession.SessionType.SEALED_BIDS,
                                                            MarketSession.SessionType.REPAIR)),
        "my_bids": MarketBid.objects.filter(session=market, participant=participant).count() if market else 0,
        "releases": releases,
    }
