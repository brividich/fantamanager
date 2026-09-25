"""Home landing portal: Chooser & Login before Dashboard."""
from decimal import Decimal
import re
from django.shortcuts import redirect, render
from django.urls import reverse
from ..models import Auction, League, Participant, Player
from .. import remote
from .common import _session_participant, target_league


def home_portal(request):
    """Initial landing portal of the application.
    
    Provides the dual-path choice:
    1. Free Area (Asta Live, Maxischermo, App Allenatori) — no login needed.
    2. Managerial Area (Regia, Command Center / Dashboard) — PIN/login protected.
    """
    leagues = list(League.objects.all().order_by("name"))
    participant = _session_participant(request)
    if participant and participant.league:
        current_league = participant.league
    else:
        current_league = target_league(request)

    if current_league is not None:
        auctions = list(Auction.objects.filter(league=current_league).order_by("-id"))
        active_auction = next(
            (a for a in auctions if a.status in [Auction.Status.LIVE, Auction.Status.PAUSED]),
            None
        )
    else:
        auctions = []
        active_auction = None

    is_unlocked = bool(request.session.get("regia_unlocked", False))
    error = ""

    if request.method == "POST":
        action = request.POST.get("action", "login")
        if action == "logout":
            request.session["regia_unlocked"] = False
            request.session.pop("regia_unlocked", None)
            return redirect("home")

        # Login attempt
        pin = (request.POST.get("pin") or "").strip()
        expected_pin = remote.regia_pin() or "123456"

        # Accept the minted tunnel PIN, default 123456, or admin
        if pin and (pin == expected_pin or pin == "123456" or pin.lower() == "admin"):
            request.session["regia_unlocked"] = True
            remote.regia_pin_register_success()
            target = request.POST.get("next") or reverse('dashboard')
            return redirect(target)
        else:
            remote.regia_pin_register_failure()
            error = f"PIN non valido. Riprova (PIN predefinito: {expected_pin})."

    expected_pin = remote.regia_pin() or "123456"

    return render(request, "auctions/home_portal.html", {
        "leagues": leagues,
        "current_league": current_league,
        "auctions": auctions,
        "active_auction": active_auction,
        "is_unlocked": is_unlocked,
        "expected_pin": expected_pin,
        "error": error,
        "is_remote": remote.request_is_remote(request),
    })
