"""Big screen view for in-room display."""

from django.conf import settings
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, render

from ..models import Auction, Participant
from .. import services


def screen(request, auction_id):
    auction = get_object_or_404(Auction, pk=auction_id)
    # Read-only public screen: when tokens are required (internet-facing), the
    # shareable link must carry ?t=<auction.public_token>. Staff bypass it.
    # This app itself has no login (request.user is always anonymous — see
    # staff_member_required above), so every internal link to this view
    # (console header, dashboard, "accesso remoto" desk) must embed the token
    # itself; the bypass only helps a deployment with real Django admin users.
    if settings.PUBLIC_TOKENS_REQUIRED and not request.user.is_staff:
        if (request.GET.get("t") or "").strip() != auction.public_token:
            return HttpResponseForbidden("Token schermo mancante o non valido.")
    # La connessione in tempo reale dello schermo lo riconosce da qui.
    request.session[f"screen_ok_{auction.id}"] = True
    recent = (
        auction.bids.select_related("participant")
        .filter(accepted=True, cancelled=False)[:10]
    )
    # Teams are per-league: the screen shows only this auction's league teams.
    # Legacy auctions without a league fall back to the global pool.
    participants = Participant.objects.filter(is_active=True)
    if auction.league_id is not None:
        participants = participants.filter(league=auction.league_id)
    participants = participants.order_by("display_name")
    return render(request, "auctions/screen.html", {
        "auction": auction,
        "state": services.serialize_state(auction),
        "recent": recent,
        "participants": participants,
    })
