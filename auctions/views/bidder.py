"""Bidder-facing views: join flow, live bid page, watchlist management, and self-release."""
import json
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..models import Auction, Participant, Player, Watch
from ..providers import importers
from .. import services
from .common import (
    SESSION_LEAGUE_KEY,
    _session_participant,
    broadcast_state,
    participant_lan_join_url,
    target_league,
)


@require_POST
def participant_release_player(request, player_id):
    """Participant svincolo: a manager frees one of *their own* players."""
    participant_id = request.session.get("participant_id")
    if not participant_id:
        return JsonResponse({"ok": False, "error": "no_session"}, status=403)
    auction_id = request.POST.get("auction_id") or None
    result = services.release_player(
        player_id, auction_id=auction_id, by_admin=False, participant_id=participant_id
    )
    if result.get("ok") and auction_id:
        auction = Auction.objects.filter(pk=auction_id).first()
        if auction is not None:
            broadcast_state(auction)
    if _wants_html(request):
        # Plain form post from the Rosa page: go back there with a message
        # instead of leaving the manager on a raw JSON page.
        if result.get("ok"):
            messages.success(
                request,
                f"{result['player_name']} svincolato: +{Decimal(result['refund']):.0f} FM.",
            )
        else:
            messages.error(request, result.get("message") or _RELEASE_ERRORS.get(result.get("error"), "Svincolo non riuscito."))
        return redirect("app_rosa")
    return JsonResponse(result, status=200 if result.get("ok") else 400)


_RELEASE_ERRORS = {
    "player_not_found": "Calciatore non trovato.",
    "not_owned": "Il calciatore è già svincolato.",
    "forbidden": "Puoi svincolare solo i calciatori della tua rosa.",
}


def _wants_html(request):
    return (
        request.headers.get("X-Requested-With") != "XMLHttpRequest"
        and "text/html" in request.headers.get("Accept", "")
    )


def _auction_for_join(participant, wanted_id, joinable):
    """The auction this manager belongs in.

    A team plays its own league's auctions and never anybody else's, so there is
    always a right answer as long as that league has one: an explicit ``?a=``
    (the QR was made for a specific auction) wins, otherwise the one actually
    being played — running first, then paused, then waiting to start — and the
    most recent of those, which is the one the room is sitting at. No picker:
    on auction night nobody should have to choose which auction they are in.

    Returns ``None`` only when there is genuinely nothing to join.
    """
    if wanted_id and str(wanted_id).isdigit():
        chosen = joinable.filter(pk=int(wanted_id)).first()
        if chosen is not None and services.participates_in(participant, chosen):
            return chosen

    candidates = joinable
    if participant.league_id is not None:
        candidates = candidates.filter(league_id=participant.league_id)
    elif joinable.filter(league__isnull=False).exists():
        # Legacy team with no league: it can only belong to the legacy pool.
        candidates = candidates.filter(league__isnull=True)

    ranked = sorted(
        candidates,
        key=lambda a: (
            {Auction.Status.LIVE: 0, Auction.Status.PAUSED: 1,
             Auction.Status.READY: 2}.get(a.status, 3),
            -a.id,
        ),
    )
    return ranked[0] if ranked else None


def join(request):
    joinable = Auction.objects.exclude(status=Auction.Status.DRAFT)

    if request.method == "POST":
        # Accept either a strong public_token (shareable link) or the
        # human-friendly access_code typed in by the bidder.
        token       = (request.POST.get("public_token") or request.GET.get("t") or "").strip()
        access_code = (request.POST.get("access_code") or "").strip()
        auction_id  = request.POST.get("auction_id")
        error = None
        participant = None

        if token:
            participant = Participant.objects.filter(
                public_token=token, is_active=True
            ).first()
            if not participant:
                error = "Link non valido o scaduto. Contatta l'organizzatore."
        elif access_code:
            # Look up pre-created participant by code.
            participant = Participant.objects.filter(
                access_code=access_code, is_active=True
            ).first()
            if not participant:
                error = "Codice non riconosciuto. Contatta l'organizzatore."
        elif settings.PUBLIC_TOKENS_REQUIRED:
            # On internet-facing deployments, never mint arbitrary participants
            # from a public name — a valid code/link is mandatory.
            error = "Inserisci il codice squadra fornito dall'organizzatore."
        else:
            # LAN fallback: create an anonymous participant from a name, with a
            # strong token so the resulting join link is unguessable.
            name = (request.POST.get("display_name") or "").strip()
            if not name:
                error = "Inserisci il tuo nome o il codice squadra."
            else:
                participant = Participant.objects.create(
                    display_name=name[:80],
                    is_active=True,
                )

        if error:
            scoped_joinable = joinable
            req_l = target_league(request)
            if req_l is not None:
                scoped_joinable = scoped_joinable.filter(league=req_l)
            return render(request, "auctions/join.html", {
                "joinable": scoped_joinable,
                "error": error,
                "token": token,
            })

        request.session["participant_id"] = participant.id
        request.session["display_name"]   = participant.display_name
        if participant.league_id is not None:
            request.session[SESSION_LEAGUE_KEY] = participant.league_id

        target = _auction_for_join(participant, auction_id, joinable)
        if target is not None:
            return redirect("bid", auction_id=target.id)
        if auction_id:
            target_a = joinable.filter(pk=auction_id).first()
            if target_a and (participant.league_id is None or target_a.league_id == participant.league_id):
                return redirect("bid", auction_id=target_a.id)
        return redirect("join")

    # GET — a tokenised invite link (?t=…, e.g. a scanned QR) signs the manager
    # in as that team, and goes straight to the bidding page: scanning your
    # team's code should put you in the auction, not on a menu.
    token = (request.GET.get("t") or "").strip()
    recognized = None
    if token:
        recognized = Participant.objects.filter(
            public_token=token, is_active=True
        ).first()
        if recognized:
            request.session["participant_id"] = recognized.id
            request.session["display_name"]   = recognized.display_name
            if recognized.league_id is not None:
                request.session[SESSION_LEAGUE_KEY] = recognized.league_id
            target = _auction_for_join(recognized, request.GET.get("a"), joinable)
            if target is not None:
                return redirect("bid", auction_id=target.id)

    if not recognized:
        recognized = _session_participant(request)

    scoped_joinable = joinable
    if recognized and recognized.league_id is not None:
        scoped_joinable = scoped_joinable.filter(league_id=recognized.league_id)
    else:
        req_l = target_league(request)
        if req_l is not None:
            scoped_joinable = scoped_joinable.filter(league=req_l)

    return render(request, "auctions/join.html", {
        "joinable": scoped_joinable,
        "recognized": recognized,
        "token": token,
    })


def bid_page(request, auction_id):
    auction = get_object_or_404(Auction, pk=auction_id)
    participant_id = request.session.get("participant_id")
    if not participant_id:
        return redirect(f"/join/?next={auction_id}")
    participant = Participant.objects.filter(pk=participant_id).first()
    # A team only bids in its own league's auctions. Landing here on somebody
    # else's auction means a stale session or a shared link: send them back to
    # their own instead of showing a board they cannot play.
    if participant is not None and not services.participates_in(participant, auction):
        own = _auction_for_join(participant, None, Auction.objects.exclude(status=Auction.Status.DRAFT))
        if own is not None:
            return redirect("bid", auction_id=own.id)
        return redirect("join")
    if participant and participant.league_id:
        request.session[SESSION_LEAGUE_KEY] = participant.league_id
    watches = (
        participant.watches.select_related("player", "player__owner")
        if participant else []
    )
    watched_ids = [w.player_id for w in watches]
    # Player id -> your own noted ceiling, for the live "sopra/sotto il tuo
    # obiettivo" chip next to the current price (only where one was set —
    # a bare star with no number isn't a target to compare against).
    watch_max_prices = {
        w.player_id: str(w.max_price) for w in watches if w.max_price is not None
    }
    return render(request, "auctions/bid.html", {
        "auction": auction,
        "participant": participant,
        # Set only when this manager came in through the public tunnel: the same
        # seat, reached over the wifi. Rebuilt as a tokenised join link because
        # the LAN address is a different origin and carries no session cookie.
        "lan_switch_url": (participant_lan_join_url(request, participant, auction)
                           if participant else ""),
        "state": json.dumps(services.serialize_state(auction)),
        "plan": services.roster_plan(participant) if participant else None,
        "watches": watches,
        "watched_ids_json": json.dumps(watched_ids),
        "watch_max_prices_json": json.dumps(watch_max_prices),
        "error_labels_json": json.dumps(services.ERROR_LABELS),
    })


@require_POST
def participant_watch_toggle(request, auction_id):
    """Add/remove the given player from the manager's watchlist."""
    participant = _session_participant(request)
    if participant is None:
        return JsonResponse({"ok": False, "error": "no_session"}, status=403)
    player = Player.objects.filter(pk=request.POST.get("player_id")).first()
    if player is None:
        return JsonResponse({"ok": False, "error": "no_player"}, status=400)
    existing = Watch.objects.filter(participant=participant, player=player).first()
    if existing:
        existing.delete()
        return JsonResponse({"ok": True, "watching": False, "player_id": player.id})
    watch = Watch.objects.create(participant=participant, player=player)
    return JsonResponse({"ok": True, "watching": True, "player_id": player.id, "watch_id": watch.id})


@require_POST
def participant_watch_update(request, watch_id):
    """Set the manager's self-noted max price on a watched player."""
    participant = _session_participant(request)
    if participant is None:
        return JsonResponse({"ok": False, "error": "no_session"}, status=403)
    watch = Watch.objects.filter(pk=watch_id, participant=participant).first()
    if watch is None:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    raw = (request.POST.get("max_price") or "").strip()
    if raw == "":
        watch.max_price = None
    else:
        try:
            watch.max_price = max(Decimal("0"), Decimal(raw.replace(",", ".")))
        except (InvalidOperation, ValueError):
            return JsonResponse({"ok": False, "error": "bad_price"}, status=400)
    watch.save(update_fields=["max_price"])
    return JsonResponse({"ok": True, "max_price": str(watch.max_price) if watch.max_price is not None else ""})


def participant_watch_search(request, auction_id):
    """Free agents matching ?q= in the manager's league (for adding targets)."""
    participant = _session_participant(request)
    if participant is None:
        return JsonResponse({"ok": False, "error": "no_session"}, status=403)
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"ok": True, "results": []})
    qs = Player.objects.filter(owner__isnull=True)
    if participant.league_id is not None:
        qs = qs.filter(league=participant.league_id)
    # Match on the accent-stripped name: mid-auction nobody types "Montipò"
    # or "Laurientè" with the accent. SQLite's icontains is accent-sensitive,
    # so filter in Python over the (league-scoped, free-agent) pool.
    needle = importers.normalize_name(q)
    watched = set(participant.watches.values_list("player_id", flat=True))
    results = [
        {"id": p.id, "name": p.name, "role": p.role, "team": p.team,
         "quota": str(p.initial_price), "watching": p.id in watched}
        for p in qs.order_by("role", "name")
        if needle in importers.normalize_name(p.name)
    ][:15]
    return JsonResponse({"ok": True, "results": results})
