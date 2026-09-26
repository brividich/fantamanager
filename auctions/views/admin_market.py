"""Admin views for Market Sessions (Buste di Mercato)."""
from datetime import datetime
from decimal import Decimal

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from ..models import Auction, League, MarketBid, MarketSession, Participant, Player, Trade
from ..services.trade import decide_trade
from ..services.market import (
    plan_market_resolution,
    resolve_market_session,
    settle_market_tie,
    sync_market_schedule,
    undo_market_resolution,
)
from .common import (
    current_auction,
    current_league,
    manageable_leagues,
    staff_member_required,
    target_league,
    user_can_manage_league,
)

_FORBIDDEN_MSG = "Non hai i permessi per gestire il mercato di questa lega."


def _managed_session_or_403(request, session_id):
    """The market session, or a 403 response when its league isn't the user's."""
    session = get_object_or_404(MarketSession.objects.select_related("league"), pk=session_id)
    if not user_can_manage_league(request.user, session.league):
        return None, HttpResponseForbidden(_FORBIDDEN_MSG)
    return session, None


def _dashboard_url(request, session=None, league_id=None):
    base = request.build_absolute_uri("/admin-auction/market/")
    if session is not None:
        return f"{base}?league={session.league_id}&session={session.id}"
    return f"{base}?league={league_id}"


def _parse_local_datetime(raw):
    """Parse a ``datetime-local`` form value into an aware datetime (or None)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    value = parse_datetime(raw)
    if value is None:
        try:
            value = datetime.strptime(raw, "%Y-%m-%dT%H:%M")
        except ValueError:
            return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value)
    return value


@staff_member_required
def admin_market_dashboard(request):
    """Dashboard to manage market sessions, inspect submitted bids, and resolve envelopes."""
    leagues = manageable_leagues(request.user)
    league = current_league(request)
    if league is not None and not user_can_manage_league(request.user, league):
        return HttpResponseForbidden(_FORBIDDEN_MSG)

    sessions = []
    selected_session = None
    participants_stats = []
    bids_list = []

    if league:
        sync_market_schedule(league)
        sessions = MarketSession.objects.filter(league=league).order_by("-created_at")
        sess_id = request.GET.get("session")
        if sess_id and sess_id.isdigit():
            selected_session = sessions.filter(pk=int(sess_id)).first()
        if not selected_session:
            # Default to the most recent open session, or latest session
            selected_session = sessions.filter(status=MarketSession.Status.OPEN).first() or sessions.first()

    if selected_session:
        # Build participant submission stats
        participants = Participant.objects.filter(league=league).order_by("display_name")
        bid_counts = dict(
            MarketBid.objects.filter(session=selected_session)
            .values("participant_id")
            .annotate(cnt=Count("id"))
            .values_list("participant_id", "cnt")
        )
        for p in participants:
            cnt = bid_counts.get(p.id, 0)
            participants_stats.append({
                "participant": p,
                "bids_count": cnt,
                "has_submitted": cnt > 0,
            })

        # Load bids: all bids if resolved or admin requested reveal
        show_all = request.GET.get("reveal") == "1" or selected_session.status == MarketSession.Status.RESOLVED
        if show_all:
            bids_list = (
                selected_session.bids.select_related("participant", "player", "release_player")
                .order_by("player__role", "player__name", "-amount", "priority")
            )

    trades_pending = []
    trades_recent = []
    if league:
        trades = Trade.objects.filter(league=league).select_related("proposer", "receiver").prefetch_related(
            "proposer_players", "receiver_players"
        )
        trades_pending = list(trades.filter(status=Trade.Status.ACCEPTED))
        trades_recent = list(trades.exclude(status=Trade.Status.ACCEPTED)[:10])

    results = None
    is_preview = False
    if selected_session:
        if selected_session.status == MarketSession.Status.RESOLVED:
            results = selected_session.results_summary or None
        elif request.GET.get("preview") == "1":
            results = plan_market_resolution(selected_session.id)
            is_preview = True

    return render(
        request,
        "auctions/admin_market.html",
        {
            "leagues": leagues,
            "current_league": league,
            "sessions": sessions,
            "selected_session": selected_session,
            "participants_stats": participants_stats,
            "bids_list": bids_list,
            "results": results,
            "trades_pending": trades_pending,
            "trades_recent": trades_recent,
            "is_preview": is_preview,
            "console_section": "Mercato Buste",
            "console_active": "market",
            "refund_modes": Auction.RefundMode.choices,
        },
    )


@staff_member_required
@require_POST
def admin_market_create(request):
    """Create a new MarketSession for the current league."""
    league = target_league(request) or current_league(request)
    if not league:
        messages.error(request, "Nessuna lega selezionata per la sessione di mercato.")
        return redirect("admin_market_dashboard")
    if not user_can_manage_league(request.user, league):
        return HttpResponseForbidden(_FORBIDDEN_MSG)

    title = (request.POST.get("title") or "Mercato di Riparazione a Buste").strip()
    allow_conditional_release = request.POST.get("allow_conditional_release") == "1"
    refund_mode = request.POST.get("refund_mode") or Auction.RefundMode.PURCHASE
    if refund_mode not in Auction.RefundMode.values:
        refund_mode = Auction.RefundMode.PURCHASE

    opens_at = _parse_local_datetime(request.POST.get("opens_at"))
    closes_at = _parse_local_datetime(request.POST.get("closes_at"))
    if opens_at and closes_at and closes_at <= opens_at:
        messages.error(request, "La chiusura deve essere successiva all'apertura.")
        return redirect(_dashboard_url(request, league_id=league.id))
    scheduled = opens_at is not None and opens_at > timezone.now()

    def _parse_int(val):
        try:
            return max(0, int(val))
        except (ValueError, TypeError):
            return 0

    session = MarketSession.objects.create(
        league=league,
        title=title,
        status=MarketSession.Status.DRAFT if scheduled else MarketSession.Status.OPEN,
        opens_at=opens_at,
        closes_at=closes_at,
        allow_conditional_release=allow_conditional_release,
        release_refund_mode=refund_mode,
        max_acquisitions_p=_parse_int(request.POST.get("max_acquisitions_p")),
        max_acquisitions_d=_parse_int(request.POST.get("max_acquisitions_d")),
        max_acquisitions_c=_parse_int(request.POST.get("max_acquisitions_c")),
        max_acquisitions_a=_parse_int(request.POST.get("max_acquisitions_a")),
    )

    if scheduled:
        messages.success(
            request,
            f"Sessione '{session.title}' creata: si aprirà il {timezone.localtime(opens_at):%d/%m/%Y alle %H:%M}.",
        )
    else:
        messages.success(request, f"Sessione '{session.title}' creata con successo e aperta alle offerte.")
    return redirect(_dashboard_url(request, session))


@staff_member_required
@require_POST
def admin_market_status(request, session_id):
    """Toggle or update status of a market session (open/closed)."""
    session, denied = _managed_session_or_403(request, session_id)
    if denied:
        return denied
    new_status = (request.POST.get("status") or "").strip().lower()
    if new_status in (MarketSession.Status.OPEN, MarketSession.Status.CLOSED):
        session.status = new_status
        session.save(update_fields=["status", "updated_at"])
        label = "aperta" if new_status == MarketSession.Status.OPEN else "chiusa"
        messages.success(request, f"Sessione '{session.title}' {label}.")
    return redirect(_dashboard_url(request, session))


@staff_member_required
@require_POST
def admin_market_resolve(request, session_id):
    """Scrutinize and resolve all envelopes for the session."""
    session, denied = _managed_session_or_403(request, session_id)
    if denied:
        return denied
    if session.status == MarketSession.Status.RESOLVED:
        messages.warning(request, f"La sessione '{session.title}' è già stata scrutinata.")
        return redirect(_dashboard_url(request, session))

    summary = resolve_market_session(session.id)
    won_count = summary.get("total_acquisitions", 0)
    ties_count = summary.get("total_ties", 0)
    messages.success(
        request,
        f"Spoglio completato per '{session.title}': {won_count} acquisti assegnati, {ties_count} situazioni di pareggio.",
    )
    return redirect(_dashboard_url(request, session))


@staff_member_required
@require_POST
def admin_market_delete(request, session_id):
    """Delete a market session and its associated bids."""
    session, denied = _managed_session_or_403(request, session_id)
    if denied:
        return denied
    league_id = session.league_id
    title = session.title
    session.delete()
    messages.info(request, f"Sessione '{title}' eliminata.")
    return redirect(_dashboard_url(request, league_id=league_id))


@staff_member_required
@require_POST
def admin_market_settle_tie(request, session_id):
    """Assign a tied player to a chosen contender, or draw one at random."""
    session, denied = _managed_session_or_403(request, session_id)
    if denied:
        return denied
    try:
        player_id = int(request.POST.get("player_id") or 0)
    except ValueError:
        player_id = 0
    raw_winner = (request.POST.get("winner_id") or "draw").strip()
    winner_id = int(raw_winner) if raw_winner.isdigit() else None

    res = settle_market_tie(session.id, player_id, winner_id=winner_id)
    if res["ok"]:
        how = "per sorteggio" if res["method"] == "draw" else "per scelta dell'admin"
        messages.success(request, f"Pareggio risolto {how}: vince {res['winner_name']}.")
    else:
        messages.error(request, res["message"])
    return redirect(_dashboard_url(request, session))


@staff_member_required
@require_POST
def admin_market_undo(request, session_id):
    """Revert a resolution: rosters and credits restored, envelopes back to pending."""
    session, denied = _managed_session_or_403(request, session_id)
    if denied:
        return denied
    res = undo_market_resolution(session.id)
    if res["ok"]:
        messages.success(
            request,
            f"Spoglio annullato: {res['reverted']} acquisti stornati. La sessione è di nuovo chiusa, in attesa di spoglio.",
        )
    else:
        messages.error(request, res["message"])
    return redirect(_dashboard_url(request, session))


@staff_member_required
@require_POST
def admin_trade_settings(request):
    """Enable/disable trades for the league and whether they need ratification."""
    league = target_league(request) or current_league(request)
    if league is None or not user_can_manage_league(request.user, league):
        return HttpResponseForbidden(_FORBIDDEN_MSG)
    league.trades_enabled = request.POST.get("trades_enabled") == "1"
    league.trades_need_approval = request.POST.get("trades_need_approval") == "1"
    league.save(update_fields=["trades_enabled", "trades_need_approval", "updated_at"])
    messages.success(request, "Impostazioni scambi salvate.")
    return redirect(_dashboard_url(request, league_id=league.id))


@staff_member_required
@require_POST
def admin_trade_decide(request, trade_id):
    """Ratify (execute) or veto a trade both teams accepted."""
    trade = get_object_or_404(Trade.objects.select_related("league"), pk=trade_id)
    if not user_can_manage_league(request.user, trade.league):
        return HttpResponseForbidden(_FORBIDDEN_MSG)
    approve = request.POST.get("action") == "approve"
    res = decide_trade(trade.id, approve, note=(request.POST.get("note") or "").strip())
    if res["ok"]:
        messages.success(request, "Scambio ratificato ed eseguito." if approve else "Scambio bocciato.")
    else:
        messages.error(request, res["message"])
    return redirect(_dashboard_url(request, league_id=trade.league_id))
