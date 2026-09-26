"""Admin participant management: roster listing, participant creation/edit, QR codes."""
import io
from decimal import Decimal, InvalidOperation

try:
    import qrcode
except ImportError:
    qrcode = None

from django.contrib import messages
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..models import Auction, League, Participant, Player
from .. import remote
from .common import (
    FORBIDDEN_LEAGUE_MSG,
    current_auction,
    manageable_leagues,
    managed_or_403,
    mixed_leagues,
    participant_join_url,
    participant_lan_join_url,
    safe_next,
    staff_member_required,
    target_league,
    user_can_manage_league,
    user_can_manage_scope,
)


@staff_member_required
def admin_participants(request):
    """Teams of one league: credits, roster size and the join link/QR to hand out.

    Every row carries the team's tokenised join link, which signs whoever
    opens it in as that team: only a user who manages the league sees them.
    """
    leagues = manageable_leagues(request.user)
    current_league = None
    raw = (request.GET.get("league") or "").strip()
    if raw.isdigit():
        current_league = League.objects.filter(pk=int(raw)).first()
        if current_league is not None and not user_can_manage_league(request.user, current_league):
            return HttpResponseForbidden(FORBIDDEN_LEAGUE_MSG)
    if current_league is None:
        current_league = target_league(request)

    participants = Participant.objects.all().order_by("display_name")
    auctions = Auction.objects.exclude(status=Auction.Status.DRAFT)
    if current_league is not None:
        participants = participants.filter(league=current_league)
        auctions = auctions.filter(league=current_league)
    elif not user_can_manage_scope(request.user, None):
        # No league picked: listing every team of every league is for superusers.
        participants = participants.none()
        auctions = auctions.none()

    # Bake the league's current auction into the links/QR so scanning drops the
    # manager straight into it. A running auction wins over one still to start.
    target = (auctions.filter(status=Auction.Status.LIVE).first()
              or auctions.filter(status=Auction.Status.READY).first())

    rows = []
    for p in participants:
        owned = Player.objects.filter(owner=p)
        counts = {r: 0 for r in ("P", "D", "C", "A")}
        for role in owned.values_list("role", flat=True):
            if role in counts:
                counts[role] += 1
        slots = current_league.total_slots if current_league else 0
        rows.append({
            "p": p,
            "roster": sum(counts.values()),
            "roster_pct": int(100 * sum(counts.values()) / slots) if slots else 0,
            "counts": counts,
            "join_url": participant_join_url(request, p, target),
            "lan_join_url": participant_lan_join_url(request, p, target),
        })

    return render(request, "auctions/admin_participants.html", {
        "leagues": leagues,
        "current_league": current_league,
        "rows": rows,
        "target_auction": target,
        "remote_on": remote.is_on(),
        "console_section": "Squadre",
        "console_active": "teams",
        "selected": current_auction(request, current_league),
    })


@staff_member_required
@require_POST
def admin_create_participant(request):
    """Create a team **inside the league the console is on**.

    The form posts ``league_id``; without it a team used to be born with
    ``league=None``, which made it invisible to every league-scoped view — the
    team simply disappeared. When the id is missing we fall back to the only
    league that exists, and refuse (with a message) when the choice would be a
    guess between several.
    """
    def dec(name, default):
        try:
            return Decimal(request.POST.get(name) or default)
        except (InvalidOperation, ValueError):
            return Decimal(default)

    league = target_league(request)
    fallback = f"/dashboard/{league.id}/#rose" if league else "/dashboard/"
    if league is None and League.objects.exists():
        messages.error(request, "Scegli prima la lega in cui creare la squadra.")
        return redirect(safe_next(request, fallback))

    p = Participant(
        league=league,
        display_name=request.POST.get("display_name", "").strip()[:80] or "Squadra",
        access_code=request.POST.get("access_code", "").strip()[:20],
        credits=dec("credits", str(league.budget) if league else "500"),
        is_active=True,
    )
    user_id = request.POST.get("user_id")
    if user_id and user_id.isdigit():
        from django.contrib.auth import get_user_model
        usr = get_user_model().objects.filter(pk=int(user_id)).first()
        if usr:
            p.user = usr
    if "logo" in request.FILES:
        p.logo = request.FILES["logo"]
    p.save()
    if league is not None:
        messages.success(request, f"Squadra «{p.display_name}» aggiunta a {league.name}.")
    return redirect(safe_next(request, fallback))


@staff_member_required
@require_POST
def admin_edit_participant(request, participant_id):
    p, denied = managed_or_403(request, Participant, participant_id)
    if denied:
        return denied

    def dec(name, default):
        try:
            return Decimal(request.POST.get(name) or default)
        except (InvalidOperation, ValueError):
            return Decimal(default)

    p.display_name = request.POST.get("display_name", p.display_name).strip()[:80]
    p.access_code  = request.POST.get("access_code", p.access_code).strip()[:20]
    p.credits      = dec("credits", str(p.credits))
    p.is_active    = request.POST.get("is_active") == "1"
    
    user_id = request.POST.get("user_id")
    if user_id == "none" or user_id == "":
        p.user = None
    elif user_id and user_id.isdigit():
        from django.contrib.auth import get_user_model
        usr = get_user_model().objects.filter(pk=int(user_id)).first()
        if usr:
            p.user = usr

    if "logo" in request.FILES:
        p.logo = request.FILES["logo"]
    elif request.POST.get("clear_logo") == "1":
        p.logo = None
    p.save()
    messages.success(request, f"Squadra «{p.display_name}» aggiornata con successo.")
    fallback = f"/dashboard/{p.league_id}/#rose" if p.league_id else "/dashboard/"
    return redirect(safe_next(request, fallback))


@staff_member_required
@require_POST
def admin_delete_participant(request, participant_id):
    p, denied = managed_or_403(request, Participant, participant_id)
    if denied:
        return denied
    league_id = p.league_id
    team_name = p.display_name
    p.delete()
    messages.success(request, f"Squadra «{team_name}» eliminata.")
    fallback = f"/dashboard/{league_id}/#rose" if league_id else "/dashboard/"
    return redirect(safe_next(request, fallback))


@staff_member_required
@require_POST
def admin_adjust_team_credits(request, participant_id):
    """Adjust credits for a team: add bonus, subtract malus, or set absolute budget."""
    p, denied = managed_or_403(request, Participant, participant_id)
    if denied:
        return denied
    mode = request.POST.get("mode", "add")  # "add", "sub", "set"
    raw_amount = request.POST.get("amount", "0")
    try:
        val = Decimal(raw_amount)
    except (InvalidOperation, ValueError):
        val = Decimal("0")

    if mode == "add":
        p.credits += val
    elif mode == "sub":
        p.credits = max(Decimal("0"), p.credits - val)
    elif mode == "set":
        p.credits = max(Decimal("0"), val)

    p.save(update_fields=["credits"])
    msg = f"Crediti di «{p.display_name}» aggiornati: {p.remaining_credits:.0f} FM rimanenti (Totale: {p.credits:.0f} FM)"
    messages.success(request, msg)

    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("format") == "json":
        return JsonResponse({
            "ok": True,
            "participant_id": p.id,
            "credits": float(p.credits),
            "spent_credits": float(p.spent_credits),
            "remaining_credits": float(p.remaining_credits),
            "message": msg,
        })
    fallback = f"/dashboard/{p.league_id}/#rose" if p.league_id else "/dashboard/"
    return redirect(safe_next(request, fallback))


@staff_member_required
@require_POST
def admin_reset_team_pin(request, participant_id):
    """Set custom PIN or generate a new random PIN, and optionally regenerate public token."""
    p, denied = managed_or_403(request, Participant, participant_id)
    if denied:
        return denied
    pin = request.POST.get("pin", "").strip()
    if not pin:
        import random
        pin = f"{random.randint(1000, 9999)}"
    p.access_code = pin
    if request.POST.get("regenerate_token") == "1":
        from ..models.core import generate_public_token
        p.public_token = generate_public_token()
    p.save(update_fields=["access_code", "public_token"])
    msg = f"PIN di accesso per «{p.display_name}» impostato su: {p.access_code}"
    messages.success(request, msg)

    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("format") == "json":
        return JsonResponse({
            "ok": True,
            "participant_id": p.id,
            "access_code": p.access_code,
            "public_token": p.public_token,
            "message": msg,
        })
    fallback = f"/dashboard/{p.league_id}/#rose" if p.league_id else "/dashboard/"
    return redirect(safe_next(request, fallback))


@staff_member_required
@require_POST
def admin_quick_assign_player(request):
    """Directly assign a player from the listone to a team without running an auction."""
    from .. import services
    participant_id = request.POST.get("participant_id")
    player_id = request.POST.get("player_id")
    price = request.POST.get("price")
    if not participant_id or not player_id:
        return JsonResponse({"ok": False, "error": "Squadra e calciatore obbligatori"}, status=400)
    p, denied = managed_or_403(request, Participant, participant_id)
    if denied:
        return denied
    player, denied = managed_or_403(request, Player, player_id)
    if denied:
        return denied
    if mixed_leagues(p, player):
        res = {"ok": False, "error": services.ERROR_LABELS["league_mismatch"]}
    else:
        res = services.assign_player(player_id, participant_id, price=price, by_admin=True)
    if res.get("ok"):
        messages.success(request, f"Calciatore assegnato a «{p.display_name}».")
    else:
        messages.error(request, res.get("error", "Errore durante l'assegnazione"))

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(res)
    fallback = f"/dashboard/{p.league_id}/#rose" if p.league_id else "/dashboard/"
    return redirect(safe_next(request, fallback))


def _qr_console_ok(request, participant):
    """The console's pass: the user manages the team's league, and — through
    the internet tunnel — has unlocked the regia, as ``staff_member_required``
    asks of every console page."""
    if not user_can_manage_scope(request.user, participant.league):
        return False
    return not remote.request_is_remote(request) or bool(request.session.get("regia_unlocked"))


def _qr_screen_token_ok(request, participant, auction):
    """The big screen's pass: ``?t=`` is the screen token of ``auction``
    (``?a=``), and the team plays in that auction's league."""
    token = (request.GET.get("t") or "").strip()
    return (auction is not None and bool(auction.public_token)
            and token == auction.public_token
            and auction.league_id == participant.league_id)


def participant_qr(request, participant_id):
    """PNG QR code of a team's tokenised join link.

    The code signs whoever scans it in as the team, so it is not handed out by
    id alone: the caller either manages the team's league (the console), or
    shows the screen token of an auction of that league (``?a=<id>&t=<token>``
    — the big screen, where each manager scans to join as their team). 403
    otherwise; 404 when the team is missing; 503 if the optional ``qrcode``
    dependency is not installed.

    ``?a=<auction_id>`` bakes the auction into the code, so scanning lands
    directly on that auction's bidding page. ``?net=lan`` encodes the wifi
    address instead of the public one — the code to show the room while the
    internet tunnel is open.
    """
    p = get_object_or_404(Participant.objects.select_related("league"), pk=participant_id)
    auction = None
    wanted = (request.GET.get("a") or "").strip()
    if wanted.isdigit():
        auction = Auction.objects.filter(pk=int(wanted)).first()
    if not (_qr_console_ok(request, p) or _qr_screen_token_ok(request, p, auction)):
        return HttpResponseForbidden("Non autorizzato.")
    if auction is not None and auction.league_id != p.league_id:
        auction = None   # never point a team at another league's auction
    if qrcode is None:
        return HttpResponse("qrcode non installato", status=503)
    url = ""
    if request.GET.get("net") == "lan":
        url = participant_lan_join_url(request, p, auction)
    img = qrcode.make(url or participant_join_url(request, p, auction), box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    resp = HttpResponse(buf.getvalue(), content_type="image/png")
    # Private: the image is a credential, no shared cache may keep it.
    resp["Cache-Control"] = "private, max-age=300"
    return resp


@staff_member_required
def admin_participant_roster(request, participant_id):
    """One team's roster as JSON — the console modal loads it when opened.

    Rendering every team's roster into the page cost thousands of DOM nodes on
    a 10-team league, for panels that are opened one at a time (if at all).
    """
    p, denied = managed_or_403(request, Participant, participant_id)
    if denied:
        return denied
    roster = [
        {"id": pl.id, "name": pl.name, "role": pl.role, "team": pl.team,
         "cost": float(pl.cost or 0), "quotation": float(pl.initial_price or 0)}
        for pl in Player.objects.filter(owner=p).order_by("role", "name")
    ]
    return JsonResponse({"ok": True, "team": p.display_name, "roster": roster})
