"""Admin dashboard and real-time auction control views."""
import json
from decimal import Decimal, InvalidOperation
from itertools import groupby as _groupby

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import Count, Q
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from .. import remote, services
from ..models import Auction, AuctionCycleResult, Bid, League, MarketSession, Participant, Player
from .admin_participants import SESSION_ACCOUNT_SECRET_KEY, can_manage_accounts
from .common import (
    SESSION_AUCTION_KEY,
    form_int,
    SESSION_LEAGUE_KEY,
    _call_order,
    _flow_mode,
    _opening_price_mode,
    _refund_mode,
    _remember_auction,
    _within_role,
    broadcast_state,
    current_auction,
    forbidden_json,
    league_mismatch_json,
    linkable_users,
    managed_or_403,
    participant_join_url,
    participant_lan_join_url,
    staff_member_required,
    target_league,
    user_can_manage,
    manageable_leagues,
    user_can_manage_league,
)


def _classifica_standings(league, players_qs, participants_qs):
    """Participants annotated with roster/credit fill, for the classifica
    table — shared between the full dashboard render and the live-refresh
    partial (admin_classifica_partial), so both compute it identically and
    a round's charge shows up without a manual page reload.
    """
    participants = list(participants_qs.order_by("display_name"))
    slots_total = league.total_slots if league else 0
    # One aggregate query for every participant's roster count instead of one
    # `Player.objects.filter(owner=p).count()` per row in the loop below.
    roster_counts = dict(
        players_qs.filter(owner__isnull=False)
        .values("owner").annotate(n=Count("id")).values_list("owner", "n")
    )
    for p in participants:
        # Roster fill drives the "rosa" meter in the standings table.
        p.roster_n = roster_counts.get(p.id, 0)
        p.slots_total = slots_total
        p.roster_pct = int(100 * p.roster_n / slots_total) if slots_total else 0
        budget = p.credits or 0
        p.credit_pct = int(100 * p.remaining_credits / budget) if budget else 0
    return participants


@staff_member_required
def admin_dashboard(request, league_id=None, hub=False, auction_id=None):
    user = request.user

    if not user.is_superuser:
        owned_leagues = list(manageable_leagues(user))
        if not owned_leagues:
            if Participant.objects.filter(user=user, is_active=True).exists():
                return redirect("app_home")
            return redirect("onboarding")
        leagues = owned_leagues
    else:
        leagues = list(League.objects.all())

    # Determine requested league and perform tenant authorization check
    req_league_raw = request.GET.get("league")
    check_league_id = league_id
    if check_league_id is None and req_league_raw and req_league_raw.isdigit():
        check_league_id = int(req_league_raw)

    if check_league_id is not None and not hub:
        req_lg = League.objects.filter(pk=check_league_id).first()
        if req_lg and not user_can_manage_league(user, req_lg):
            return HttpResponseForbidden("Non hai i permessi per accedere a questa lega.")

    # Clean URL redirect for legacy /admin-auction/?league=X (when not a test ?home=1 query)
    if request.path.startswith("/admin-auction") and not request.GET.get("home"):
        if req_league_raw and req_league_raw.isdigit():
            return redirect(f"/dashboard/{req_league_raw}/")

    if hub:
        request.session.pop(SESSION_LEAGUE_KEY, None)
        request.session.pop(SESSION_AUCTION_KEY, None)
        current_league = None
    elif league_id is not None:
        current_league = get_object_or_404(League, pk=league_id)
        if not user_can_manage_league(user, current_league):
            return HttpResponseForbidden("Non hai i permessi per accedere a questa lega.")
        request.session[SESSION_LEAGUE_KEY] = current_league.id
    else:
        current_league = target_league(request)
        if current_league and not user_can_manage_league(user, current_league):
            return HttpResponseForbidden("Non hai i permessi per gestire questa lega.")

    selected_id = auction_id or request.GET.get("auction")
    selected = None
    bids_by_cycle = []
    state_json = "null"

    if selected_id:
        selected = get_object_or_404(Auction, pk=selected_id)
        current_league = selected.league
        if not user.is_superuser:
            if not user_can_manage_league(user, current_league):
                return HttpResponseForbidden("Non hai i permessi per gestire le aste di questa lega.")
        if current_league is not None:
            request.session[SESSION_LEAGUE_KEY] = current_league.id
        bids_qs = selected.bids.select_related("participant").order_by("-cycle", "-server_received_at")[:200]
        bids_by_cycle = [
            {"cycle": k, "bids": list(v)}
            for k, v in _groupby(bids_qs, key=lambda b: b.cycle)
        ]
        state_json = json.dumps(services.serialize_state(selected))

    # No explicit ?auction=: stay on the auction this console was running, so a
    # trip to Giocatori/Squadre and back never dumps you on the start screen.
    # ?home=1 or direct /dashboard/ routes opt out: accessing the dashboard must land on the dashboard, not
    # inside whatever auction was left open.
    is_dashboard_route = (
        getattr(request.resolver_match, "url_name", "") in ("dashboard", "dashboard_league", "dashboard_hub", "league_view")
        or request.path.startswith("/dashboard")
        or request.path.startswith("/lega/")
    )
    if not selected_id and not request.GET.get("home") and not is_dashboard_route and not hub and league_id is None:
        if current_league is not None:
            pinned = current_auction(request, league=current_league)
            if pinned is not None:
                return redirect(f"/regia/{pinned.id}/")

    league_cards = []
    available_users = []
    free_players_sample = []
    league_admin_ids = set()
    if current_league is not None:
        auctions = Auction.objects.filter(league=current_league)
        participants = Participant.objects.filter(league=current_league).select_related("user")
        players = Player.objects.filter(league=current_league)
        participants = _classifica_standings(current_league, players, participants)
        for p in participants:
            p.join_url = participant_join_url(request, p, selected)
            p.lan_join_url = participant_lan_join_url(request, p, selected)
        free_count = players.filter(owner__isnull=True).count()
        free_players_sample = list(players.filter(owner__isnull=True).order_by("role", "-initial_price", "name")[:60])

        # Pre-organize rosters by role for all participants (zero N+1 queries)
        owned_players = list(
            players.filter(owner__isnull=False)
            .select_related("owner")
            .order_by("role", "name")
        )
        rosters_by_participant = {p.id: {"P": [], "D": [], "C": [], "A": []} for p in participants}
        roster_spent_by_participant = {p.id: {"P": Decimal("0"), "D": Decimal("0"), "C": Decimal("0"), "A": Decimal("0")} for p in participants}

        for pl in owned_players:
            if pl.owner_id in rosters_by_participant:
                role = pl.role if pl.role in ("P", "D", "C", "A") else "A"
                rosters_by_participant[pl.owner_id][role].append(pl)
                roster_spent_by_participant[pl.owner_id][role] += (pl.cost or Decimal("0"))

        caps = {
            "P": current_league.slots_p if current_league.slot_limits else 0,
            "D": current_league.slots_d if current_league.slot_limits else 0,
            "C": current_league.slots_c if current_league.slot_limits else 0,
            "A": current_league.slots_a if current_league.slot_limits else 0,
        }

        league_admin_ids = set(current_league.admins.values_list("id", flat=True)) if current_league else set()
        owner_id = current_league.owner_id if current_league else None

        for p in participants:
            p.roster_by_role = rosters_by_participant.get(p.id, {"P": [], "D": [], "C": [], "A": []})
            p.role_spent = roster_spent_by_participant.get(p.id, {"P": Decimal("0"), "D": Decimal("0"), "C": Decimal("0"), "A": Decimal("0")})
            p.role_counts = {r: len(p.roster_by_role[r]) for r in ("P", "D", "C", "A")}
            p.role_caps = caps
            p.empty_slots = {
                r: max(0, caps[r] - p.role_counts[r]) if caps[r] > 0 else 0
                for r in ("P", "D", "C", "A")
            }
            p.empty_slots_ranges = {
                r: range(p.empty_slots[r]) for r in ("P", "D", "C", "A")
            }
            if p.user_id:
                p.is_league_owner = (p.user_id == owner_id)
                p.is_league_admin = (p.user_id in league_admin_ids)
                if p.is_league_owner:
                    p.league_role = "owner"
                    p.league_role_label = "Presidente"
                elif p.is_league_admin:
                    p.league_role = "admin"
                    p.league_role_label = "Amministratore"
                else:
                    p.league_role = "manager"
                    p.league_role_label = "Allenatore"
            else:
                p.is_league_owner = False
                p.is_league_admin = False
                p.league_role = "none"
                p.league_role_label = "Nessun Account"

        available_users = list(linkable_users(user))
    else:
        auctions = Auction.objects.none()
        participants = []
        players = Player.objects.none()
        free_count = 0
        for lg in leagues:
            lg_active = lg.auctions.filter(status__in=[Auction.Status.LIVE, Auction.Status.PAUSED]).first()
            league_cards.append({
                "league": lg,
                "team_count": lg.participants.count(),
                "player_count": lg.players.count(),
                "active_auction": lg_active,
                "auctions_count": lg.auctions.count(),
            })

    queue_preview = []
    queue_pending = 0
    if selected is not None and selected.flow_mode != Auction.FlowMode.CALL:
        pending = selected.queue_items.filter(done=False)
        # The card shows the first 20 but counts them all, so the number matches
        # what the live state pushes a moment later instead of jumping.
        queue_pending = pending.count()
        queue_preview = list(
            pending.select_related("player").order_by("order", "id")[:20]
        )

    # Storico: every resolved lot (sold or invenduto), most recent first.
    storico = []
    if selected is not None:
        storico = list(
            selected.cycle_results.select_related("winner")
            .order_by("-cycle")[:300]
        )

    _remember_auction(request, selected)

    # "Attività live": the most recent accepted rilanci, newest first.
    recent_bids = []
    if selected is not None:
        recent_bids = list(
            selected.bids.filter(accepted=True, cancelled=False)
            .select_related("participant").order_by("-server_received_at")[:12]
        )

    active_auction = None
    market_sessions = []
    total_spent_credits = Decimal("0")
    total_league_credits = Decimal("0")
    remaining_league_credits = Decimal("0")
    spent_pct = 0
    assigned_count = 0
    avg_spent = Decimal("0")
    slots_per_team = 0
    total_roster_slots = 0
    roster_fill_pct = 0
    role_totals = {}
    role_assigned = {}
    recent_assignments = []

    competitions = []
    season = None
    if selected is None and current_league is not None:
        from ..services.competitions import ensure_league_season_and_competitions
        season, competitions = ensure_league_season_and_competitions(current_league)
        active_auction = auctions.filter(status__in=[Auction.Status.LIVE, Auction.Status.PAUSED]).first()
        market_sessions = list(MarketSession.objects.filter(league=current_league).order_by("-created_at")[:6])

        for p in participants:
            total_spent_credits += (p.spent_credits or Decimal("0"))
            total_league_credits += (p.credits or Decimal("0"))
        assigned_count = players.filter(owner__isnull=False).count()
        if assigned_count > 0:
            avg_spent = total_spent_credits / assigned_count

        remaining_league_credits = max(Decimal("0"), total_league_credits - total_spent_credits)
        spent_pct = int(100 * total_spent_credits / total_league_credits) if total_league_credits else 0
        slots_per_team = current_league.total_slots or 25
        total_roster_slots = slots_per_team * len(participants)
        roster_fill_pct = int(100 * assigned_count / total_roster_slots) if total_roster_slots else 0

        role_totals = dict(players.values("role").annotate(n=Count("id")).values_list("role", "n"))
        role_assigned = dict(players.filter(owner__isnull=False).values("role").annotate(n=Count("id")).values_list("role", "n"))
        recent_assignments = list(
            AuctionCycleResult.objects.filter(
                Q(auction__league=current_league) & (Q(assigned=True) | Q(winner__isnull=False) | ~Q(winner_name=""))
            )
            .select_related("player", "winner", "auction")
            .order_by("-id")[:8]
        )

    return render(request, "auctions/admin_dashboard.html", {
        "auctions":     auctions,
        "leagues":      leagues,
        "league_cards": league_cards,
        "recent_bids":  recent_bids,
        "console_section": "Live auction" if selected else "Dashboard",
        "console_active":  "live" if selected else "dashboard",
        "current_league": current_league,
        "season": season,
        "competitions": competitions,
        "selected":     selected,
        "active_auction": active_auction,
        "market_sessions": market_sessions,
        "total_spent_credits": total_spent_credits,
        "total_league_credits": total_league_credits,
        "remaining_league_credits": remaining_league_credits,
        "spent_pct":    spent_pct,
        "slots_per_team": slots_per_team,
        "total_roster_slots": total_roster_slots,
        "roster_fill_pct": roster_fill_pct,
        "role_totals":  role_totals,
        "role_assigned": role_assigned,
        "recent_assignments": recent_assignments,
        "assigned_count": assigned_count,
        "avg_spent":    avg_spent,
        "bids_by_cycle": bids_by_cycle,
        "state_json":   state_json,
        "participants": participants,
        "players":      players,
        "free_count":   free_count,
        "available_users": available_users,
        "free_players_sample": free_players_sample,
        "flow_modes":   Auction.FlowMode.choices,
        "call_orders":  Auction.CallOrder.choices,
        "within_roles": Auction.WithinRole.choices,
        "opening_price_modes": Auction.OpeningPriceMode.choices,
        "queue_preview": queue_preview,
        "queue_pending": queue_pending,
        "storico": storico,
        "account_secret": request.session.pop(SESSION_ACCOUNT_SECRET_KEY, None) if can_manage_accounts(user, current_league) else None,
        "can_manage_accounts": can_manage_accounts(user, current_league),
        "league_admin_ids": league_admin_ids,
        "error_labels_json": json.dumps(services.ERROR_LABELS),
        # The tunnel status carries the regia PIN: superadmin only.
        "remote_json": json.dumps(remote.status()) if user.is_superuser else "null",
        "lan_url": remote.lan_url(request),
    })


@staff_member_required
def admin_classifica_partial(request, auction_id):
    """Fresh classifica rows for one auction's league — re-fetched by the
    dashboard's own JS whenever a round concludes (see applyState's cycle
    tracking), since the live websocket state never carried per-team
    credit/roster figures and the table used to sit frozen at whatever it
    showed on the last full page load.
    """
    auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    league = auction.league
    participants = Participant.objects.filter(league=league) if league else Participant.objects.all()
    players = Player.objects.filter(league=league) if league else Player.objects.all()
    rows = _classifica_standings(league, players, participants)
    html = render_to_string(
        "auctions/_classifica_rows.html", {"participants": rows}, request=request,
    )
    return JsonResponse({"ok": True, "html": html})


@staff_member_required
def admin_storico_partial(request, auction_id):
    """Fresh storico rows for one auction — re-fetched by the dashboard's own
    JS whenever a round concludes, and by its manual "Aggiorna" button, so a
    knock-down or an admin svincolo/rifai-asta shows up without a full page
    reload (see refreshStorico() / applyState's cycle tracking)."""
    auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    storico = list(
        auction.cycle_results.select_related("winner").order_by("-cycle")[:300]
    )
    html = render_to_string(
        "auctions/_storico_rows.html", {"storico": storico}, request=request,
    )
    return JsonResponse({"ok": True, "html": html})


@staff_member_required
@require_POST
def admin_create_auction(request):
    def dec(name, default):
        try:
            return Decimal(request.POST.get(name) or default)
        except (InvalidOperation, ValueError):
            return Decimal(default)

    player_id = request.POST.get("player_id")
    player    = Player.objects.select_related("league").filter(pk=player_id).first() if player_id else None
    if player is not None and not user_can_manage(request.user, player):
        return forbidden_json()
    # The auction lives in the player's league, else the one the console is on.
    # One outside any league is superuser-only: its creator could not open it.
    league = player.league if player is not None else target_league(request)
    if league is None:
        if not request.user.is_superuser:
            return forbidden_json()
    elif not user_can_manage_league(request.user, league):
        return forbidden_json()
    sp        = dec("starting_price", str(player.initial_price) if player else "1")

    mode = request.POST.get("mode", "").strip()
    if mode not in Auction.Mode.values:
        mode = Auction.Mode.NEW_FROM_ZERO

    auction = Auction.objects.create(
        league=league,
        title=request.POST.get("title", "").strip() or (str(player) if player else "Asta"),
        description=request.POST.get("description", "").strip(),
        player=player,
        mode=mode,
        source_site=request.POST.get("source_site", "").strip(),
        source_league_id=request.POST.get("source_league_id", "").strip(),
        starting_price=sp,
        current_price=sp,
        min_increment=dec("min_increment", "1"),
        quick_increments=request.POST.get("quick_increments", "10,50,100,500"),
        duration_seconds=form_int(request.POST.get("duration_seconds"), 60,
                                  min_value=MIN_LOT_SECONDS, max_value=MAX_TIMER_SECONDS),
        antisnipe_seconds=form_int(request.POST.get("antisnipe_seconds"), 0,
                                   min_value=0, max_value=MAX_TIMER_SECONDS),
        release_refund_mode=_refund_mode(request),
        opening_price_mode=_opening_price_mode(request),
        flow_mode=_flow_mode(request),
        call_order=_call_order(request),
        within_role_order=_within_role(request),
        status=Auction.Status.READY,
    )
    return redirect(f"/dashboard/?auction={auction.id}")


def _pool_player_or_error(request, auction, player_id):
    """Check the player a regia action names against ``auction``.

    ``(player, None)`` when it is in the auction's pool, ``(None, None)`` when
    there is no such player (the service reports that), else ``(None, error)``:
    403 for a player the user does not manage, 400 for one of another league.
    The queue and call services look the player up by id in every league.
    """
    player = Player.objects.select_related("league").filter(pk=player_id).first() \
        if str(player_id or "").isdigit() else None
    if player is None:
        return None, None
    if not user_can_manage(request.user, player):
        return None, forbidden_json()
    if player.league_id != auction.league_id:
        return None, league_mismatch_json()
    return player, None


# Bounds for the lot timers typed in the console: a 0 or negative duration
# would close every lot the moment it opens.
MIN_LOT_SECONDS = 3
MAX_TIMER_SECONDS = 3600


def _pint(raw, fallback):
    """Un intero non negativo da un campo di form, o quello che c'era."""
    if raw is None or str(raw).strip() == "":
        return int(fallback)
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return int(fallback)


def _break_seconds(raw, fallback):
    """Parse the pause between two lots: a tolerant, clamped number of seconds."""
    if raw is None or str(raw).strip() == "":
        return fallback
    try:
        value = float(str(raw).strip().replace(",", "."))
    except ValueError:
        return fallback
    return round(max(0.0, min(60.0, value)), 1)


@staff_member_required
@require_POST
def admin_edit_auction(request, auction_id):
    auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied

    def dec(name, fallback):
        try:
            return Decimal(request.POST.get(name) or fallback)
        except (InvalidOperation, ValueError):
            return Decimal(fallback)

    auction.title            = request.POST.get("title", auction.title).strip() or auction.title
    auction.description      = request.POST.get("description", auction.description).strip()
    auction.min_increment    = dec("min_increment", str(auction.min_increment))
    auction.quick_increments = request.POST.get("quick_increments", auction.quick_increments).strip()
    auction.duration_seconds = form_int(request.POST.get("duration_seconds"), auction.duration_seconds,
                                        min_value=MIN_LOT_SECONDS, max_value=MAX_TIMER_SECONDS)
    auction.antisnipe_seconds = form_int(request.POST.get("antisnipe_seconds"), 0,
                                         min_value=0, max_value=MAX_TIMER_SECONDS)
    auction.cycle_break_seconds = _break_seconds(
        request.POST.get("cycle_break_seconds"), auction.cycle_break_seconds)
    auction.starting_price   = dec("starting_price", str(auction.starting_price))
    auction.release_refund_mode = _refund_mode(request, auction.release_refund_mode)
    auction.opening_price_mode = _opening_price_mode(request, auction.opening_price_mode)
    # The settings form always posts the checkbox state (hidden 0 + checkbox 1).
    auction.enforce_limits = request.POST.get("enforce_limits", "1") == "1"
    auction.block_leader_rebid = request.POST.get("block_leader_rebid", "1") == "1"
    auction.manual_auto_advance = request.POST.get("manual_auto_advance", "0") == "1"
    auction.sealed_bids = request.POST.get("sealed_bids", "0") == "1"
    for role in ("p", "d", "c", "a"):
        field = f"sealed_threshold_{role}"
        setattr(auction, field, _pint(request.POST.get(field), getattr(auction, field)))
    auction.sealed_seconds = max(5, _pint(
        request.POST.get("sealed_seconds"), auction.sealed_seconds))
    auction.sealed_enforce_rules = request.POST.get(
        "sealed_enforce_rules", "1" if auction.sealed_enforce_rules else "0") == "1"
    ts = request.POST.get("screen_timer_size")
    if ts in Auction.ScreenSize.values:
        auction.screen_timer_size = ts
    ns = request.POST.get("screen_name_size")
    if ns in Auction.ScreenSize.values:
        auction.screen_name_size = ns
    up = request.POST.get("unsold_policy")
    if up in Auction.UnsoldPolicy.values:
        auction.unsold_policy = up
    # Remember the queue-shaping fields so we can rebuild the queue if any change.
    prev_flow = (auction.flow_mode, auction.call_order, auction.within_role_order)
    fm = request.POST.get("flow_mode")
    if fm in Auction.FlowMode.values:
        auction.flow_mode = fm
    co = request.POST.get("call_order")
    if co in Auction.CallOrder.values:
        auction.call_order = co
    wr = request.POST.get("within_role_order")
    if wr in Auction.WithinRole.values:
        auction.within_role_order = wr
    auction.save()
    if (auction.flow_mode, auction.call_order, auction.within_role_order) != prev_flow:
        if auction.is_ordered_flow:
            services.build_queue(auction)
        else:
            auction.queue_items.filter(done=False).delete()
    broadcast_state(auction)
    return JsonResponse({"ok": True, "state": services.serialize_state(auction)})


@staff_member_required
def admin_control(request, auction_id, action):
    handlers = {
        "start":  services.start_auction,
        "pause":  services.pause_auction,
        "resume": services.resume_auction,
        "close":  services.close_auction,
    }
    auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    handler = handlers.get(action)
    if handler is None:
        return JsonResponse({"ok": False, "error": "unknown_action"}, status=400)
    if action == "start":
        if not services.listone_loaded(auction):
            return JsonResponse(
                {"ok": False,
                 "error": "Carica prima il file Quotazioni (listone): è obbligatorio "
                          "per avviare l'asta."},
                status=400,
            )
    auction = handler(auction_id)
    if auction is None:
        return JsonResponse(
            {"ok": False,
             "error": "Impossibile avviare: nessun giocatore sul piatto e coda vuota. "
                      "Richiama un giocatore o costruisci la coda."},
            status=400,
        )
    broadcast_state(auction)
    return JsonResponse({"ok": True, "state": services.serialize_state(auction)})


@staff_member_required
@require_POST
def admin_build_queue(request, auction_id):
    """(Re)build the running order from the current free-agent pool."""
    auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    count = services.build_queue(auction)
    broadcast_state(auction)
    return JsonResponse({"ok": True, "pending": count,
                         "items": services.get_queue_preview(auction, limit=20),
                         "state": services.serialize_state(auction)})


@staff_member_required
def admin_queue_preview(request, auction_id):
    """Get real-time pending queue items."""
    auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    limit = form_int(request.GET.get("limit"), 20, min_value=1, max_value=500)
    items = services.get_queue_preview(auction, limit=limit)
    pending = auction.queue_items.filter(done=False).count()
    return JsonResponse({"ok": True, "pending": pending, "items": items})


@staff_member_required
@require_POST
def admin_queue_prioritize(request, auction_id):
    """Move a player to the very front of the pending queue."""
    auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    player_id = request.POST.get("player_id")
    if not player_id:
        return JsonResponse({"ok": False, "error": "missing_player_id"}, status=400)
    _player, denied = _pool_player_or_error(request, auction, player_id)
    if denied:
        return denied
    item = services.prioritize_queue_item(auction, player_id)
    if item is None:
        return JsonResponse({"ok": False, "error": "player_not_found"}, status=404)
    broadcast_state(auction)
    return JsonResponse({
        "ok": True,
        "items": services.get_queue_preview(auction, limit=20),
        "pending": auction.queue_items.filter(done=False).count(),
        "state": services.serialize_state(auction),
    })


@staff_member_required
@require_POST
def admin_queue_postpone(request, auction_id):
    """Postpone a player to the end of their role band."""
    auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    player_id = request.POST.get("player_id")
    if not player_id:
        return JsonResponse({"ok": False, "error": "missing_player_id"}, status=400)
    _player, denied = _pool_player_or_error(request, auction, player_id)
    if denied:
        return denied
    res = services.postpone_queue_item(auction, player_id)
    if res is None:
        return JsonResponse({"ok": False, "error": "player_not_found"}, status=404)
    broadcast_state(auction)
    return JsonResponse({
        "ok": True,
        "items": services.get_queue_preview(auction, limit=20),
        "pending": auction.queue_items.filter(done=False).count(),
        "state": services.serialize_state(auction),
    })


@staff_member_required
@require_POST
def admin_queue_exclude(request, auction_id):
    """Exclude a player from the queue."""
    auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    player_id = request.POST.get("player_id")
    if not player_id:
        return JsonResponse({"ok": False, "error": "missing_player_id"}, status=400)
    res = services.exclude_queue_item(auction, player_id)
    if not res:
        return JsonResponse({"ok": False, "error": "player_not_found"}, status=404)
    broadcast_state(auction)
    return JsonResponse({
        "ok": True,
        "items": services.get_queue_preview(auction, limit=20),
        "pending": auction.queue_items.filter(done=False).count(),
        "state": services.serialize_state(auction),
    })


@staff_member_required
@require_POST
def admin_call_player(request, auction_id):
    """CALL mode: put a specific free agent on the block."""
    auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    player_id = request.POST.get("player_id")
    _player, denied = _pool_player_or_error(request, auction, player_id)
    if denied:
        return denied
    auction = services.call_player(auction_id, player_id)
    if auction is None:
        return JsonResponse({"ok": False, "error": "player_unavailable"}, status=400)
    broadcast_state(auction)
    return JsonResponse({"ok": True, "state": services.serialize_state(auction)})


@staff_member_required
@require_POST
def admin_manual_step(request, auction_id):
    """MANUAL flow: step the running order forward / backward."""
    _auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    direction = request.POST.get("direction", "next")
    if direction not in ("next", "prev", "next_role", "prev_role"):
        return JsonResponse({"ok": False, "error": "bad_direction"}, status=400)
    auction = services.manual_step(
        auction_id, direction, undo_sale=(request.POST.get("undo_sale") == "1"),
    )
    undo = getattr(auction, "needs_undo_confirm", None)
    if undo is not None:
        return JsonResponse({"ok": False, "error": "needs_undo_confirm", "undo": undo},
                            status=409)
    broadcast_state(auction)
    return JsonResponse({"ok": True, "state": services.serialize_state(auction)})


@staff_member_required
@require_POST
def admin_set_auto_advance(request, auction_id):
    """Regia toggle: in MANUAL, let un-bid lots expire and roll on by themselves."""
    _auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    auction = services.set_auto_advance(auction_id, request.POST.get("on") == "1")
    broadcast_state(auction)
    return JsonResponse({"ok": True, "state": services.serialize_state(auction)})


@staff_member_required
@require_POST
def admin_confirm_advance(request, auction_id):
    """Regia gives the OK after a knocked-down lot closes on an auto-advancing flow."""
    _auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    auction = services.reset_if_closed(auction_id)
    if auction is None:
        return JsonResponse({"ok": False, "error": "nothing_to_advance"}, status=400)
    broadcast_state(auction)
    return JsonResponse({"ok": True, "state": services.serialize_state(auction)})


@staff_member_required
@require_POST
def admin_force_close_lot(request, auction_id):
    """Regia: skip a lot nobody is bidding on, without waiting it out."""
    _auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    auction = services.force_close_lot(auction_id)
    if auction.force_close_error:
        return JsonResponse({"ok": False, "error": auction.force_close_error}, status=409)
    broadcast_state(auction)
    return JsonResponse({"ok": True, "state": services.serialize_state(auction)})


@staff_member_required
@require_POST
def admin_open_sealed(request, auction_id):
    """Regia: manda il lotto in corso alle buste senza aspettare la soglia."""
    _auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    auction = services.open_sealed_now(auction_id)
    if getattr(auction, "sealed_error", None):
        return JsonResponse({"ok": False, "error": auction.sealed_error}, status=409)
    broadcast_state(auction)
    return JsonResponse({"ok": True, "state": services.serialize_state(auction)})


@staff_member_required
@require_POST
def admin_resolve_sealed(request, auction_id):
    """Regia: spoglio immediato delle buste, senza aspettare il tempo."""
    _auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    auction = services.resolve_sealed(auction_id, force=True)
    if auction is None:
        return JsonResponse({"ok": False, "error": "sealed_not_open"}, status=409)
    broadcast_state(auction)
    return JsonResponse({
        "ok": True,
        "event": getattr(auction, "sealed_event", ""),
        "state": services.serialize_state(auction),
    })


@staff_member_required
@require_POST
def admin_adjust_timer(request, auction_id):
    """Regista control: add/remove seconds from the running lot timer."""
    _auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    try:
        delta = int(request.POST.get("delta", "0"))
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "bad_delta"}, status=400)
    if not -600 <= delta <= 600:
        return JsonResponse({"ok": False, "error": "out_of_range"}, status=400)
    auction = services.adjust_timer(auction_id, delta)
    if not getattr(auction, "timer_changed", False):
        return JsonResponse({"ok": False, "error": "timer_not_running"}, status=409)
    broadcast_state(auction)
    return JsonResponse({"ok": True, "state": services.serialize_state(auction)})


@staff_member_required
@require_POST
def admin_bid_for(request, auction_id):
    """Regista control: place a bid on behalf of a participant (absent bidder)."""
    _auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    participant_id = request.POST.get("participant_id")
    increment = request.POST.get("increment")
    if not participant_id:
        return JsonResponse({"ok": False, "error": "no_participant"}, status=400)
    result = services.place_bid(auction_id, participant_id, increment,
                                user_agent="regia", ip_address=None)
    if result.accepted:
        layer = get_channel_layer()
        if layer is not None:
            async_to_sync(layer.group_send)(
                f"auction_{auction_id}",
                {"type": "bid.new", "bid": services.serialize_bid(result.bid), "extended": result.extended},
            )
        auction = get_object_or_404(Auction, pk=auction_id)
        broadcast_state(auction)
        return JsonResponse({"ok": True, "bid": services.serialize_bid(result.bid)})
    return JsonResponse({"ok": False, "error": result.reason}, status=400)


@staff_member_required
@require_POST
def admin_announce(request, auction_id):
    """Push a short announcement banner to everyone in the auction room."""
    auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    text = (request.POST.get("text") or "").strip()[:140]
    if not text:
        return JsonResponse({"ok": False, "error": "empty"}, status=400)
    level = request.POST.get("level", "info")
    if level not in ("info", "call"):
        level = "info"
    layer = get_channel_layer()
    if layer is not None:
        async_to_sync(layer.group_send)(
            f"auction_{auction.id}",
            {"type": "announcement", "text": text, "level": level},
        )
    return JsonResponse({"ok": True, "text": text, "level": level})


@staff_member_required
@require_POST
def admin_cancel_bid(request, bid_id):
    bid = Bid.objects.select_related("auction__league").filter(pk=bid_id).first()
    if bid is not None and not user_can_manage(request.user, bid.auction):
        return forbidden_json()
    reason = request.POST.get("reason", "cancelled_by_admin")
    result = services.cancel_bid(bid_id, reason=reason)
    if not result["ok"]:
        return JsonResponse(result, status=400)
    auction = result["auction"]
    broadcast_state(auction)
    return JsonResponse({"ok": True, "state": services.serialize_state(auction)})


@staff_member_required
def admin_logs_tail(request):
    """Return the last 100 lines of system logs for the regia console.

    Superadmin only: the log covers every league on the server (names, IPs,
    bids), not just the one a league admin runs.
    """
    from collections import deque
    from pathlib import Path
    from django.conf import settings

    if not request.user.is_superuser:
        return forbidden_json()

    log_file = Path(settings.BASE_DIR) / "logs" / "fantamanager.log"
    lines = []
    if log_file.exists():
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = list(deque(f, maxlen=100))
        except OSError:
            lines = ["[Errore durante la lettura del file di log]"]
    return JsonResponse({"ok": True, "lines": [line.rstrip("\r\n") for line in lines]})



@staff_member_required
@require_POST
def admin_auction_turns(request, auction_id):
    """Regia: chiamata a turno (5.02) — attiva con l'ordine di classifica, passa il turno, disattiva."""
    from ..services.turns import default_order

    auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    action = request.POST.get("action")
    if action == "enable":
        auction.turn_order = default_order(auction.league) if auction.league else []
        auction.turn_skips = 0
    elif action == "skip":
        auction.turn_skips += 1
    elif action == "disable":
        auction.turn_order = []
    else:
        return JsonResponse({"ok": False, "error": "bad_action"}, status=400)
    auction.save(update_fields=["turn_order", "turn_skips"])
    broadcast_state(auction)
    return JsonResponse({"ok": True, "state": services.serialize_state(auction)})
