"""Admin views for Market Sessions (Buste di Mercato)."""
from datetime import datetime
from decimal import Decimal

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from ..models import Auction, League, MarketBid, MarketSession, Participant, Player
from ..services.market import resolve_market_session
from .common import (
    current_auction,
    current_league,
    staff_member_required,
    target_league,
)


@staff_member_required
def admin_market_dashboard(request):
    """Dashboard to manage market sessions, inspect submitted bids, and resolve envelopes."""
    leagues = League.objects.all().order_by("name")
    league = current_league(request)

    sessions = []
    selected_session = None
    participants_stats = []
    bids_list = []

    if league:
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

    title = (request.POST.get("title") or "Mercato di Riparazione a Buste").strip()
    allow_conditional_release = request.POST.get("allow_conditional_release") == "1"
    refund_mode = request.POST.get("refund_mode") or Auction.RefundMode.PURCHASE
    if refund_mode not in Auction.RefundMode.values:
        refund_mode = Auction.RefundMode.PURCHASE

    closes_at_raw = (request.POST.get("closes_at") or "").strip()
    closes_at = None
    if closes_at_raw:
        closes_at = parse_datetime(closes_at_raw)
        if not closes_at:
            try:
                closes_at = datetime.strptime(closes_at_raw, "%Y-%m-%dT%H:%M")
            except ValueError:
                closes_at = None
        if closes_at and timezone.is_naive(closes_at):
            closes_at = timezone.make_aware(closes_at)

    def _parse_int(val):
        try:
            return max(0, int(val))
        except (ValueError, TypeError):
            return 0

    session = MarketSession.objects.create(
        league=league,
        title=title,
        status=MarketSession.Status.OPEN,
        closes_at=closes_at,
        allow_conditional_release=allow_conditional_release,
        release_refund_mode=refund_mode,
        max_acquisitions_p=_parse_int(request.POST.get("max_acquisitions_p")),
        max_acquisitions_d=_parse_int(request.POST.get("max_acquisitions_d")),
        max_acquisitions_c=_parse_int(request.POST.get("max_acquisitions_c")),
        max_acquisitions_a=_parse_int(request.POST.get("max_acquisitions_a")),
    )

    messages.success(request, f"Sessione '{session.title}' creata con successo e aperta alle offerte.")
    return redirect(f"{request.build_absolute_uri('/admin-auction/market/')}?league={league.id}&session={session.id}")


@staff_member_required
@require_POST
def admin_market_status(request, session_id):
    """Toggle or update status of a market session (open/closed)."""
    session = get_object_or_404(MarketSession, pk=session_id)
    new_status = (request.POST.get("status") or "").strip().lower()
    if new_status in (MarketSession.Status.OPEN, MarketSession.Status.CLOSED):
        session.status = new_status
        session.save(update_fields=["status", "updated_at"])
        label = "aperta" if new_status == MarketSession.Status.OPEN else "chiusa"
        messages.success(request, f"Sessione '{session.title}' {label}.")
    return redirect(f"{request.build_absolute_uri('/admin-auction/market/')}?league={session.league_id}&session={session.id}")


@staff_member_required
@require_POST
def admin_market_resolve(request, session_id):
    """Scrutinize and resolve all envelopes for the session."""
    session = get_object_or_404(MarketSession, pk=session_id)
    if session.status == MarketSession.Status.RESOLVED:
        messages.warning(request, f"La sessione '{session.title}' è già stata scrutinata.")
        return redirect(f"{request.build_absolute_uri('/admin-auction/market/')}?league={session.league_id}&session={session.id}")

    summary = resolve_market_session(session.id)
    won_count = summary.get("total_acquisitions", 0)
    ties_count = summary.get("total_ties", 0)
    messages.success(
        request,
        f"Spoglio completato per '{session.title}': {won_count} acquisti assegnati, {ties_count} situazioni di pareggio.",
    )
    return redirect(f"{request.build_absolute_uri('/admin-auction/market/')}?league={session.league_id}&session={session.id}")


@staff_member_required
@require_POST
def admin_market_delete(request, session_id):
    """Delete a market session and its associated bids."""
    session = get_object_or_404(MarketSession, pk=session_id)
    league_id = session.league_id
    title = session.title
    session.delete()
    messages.info(request, f"Sessione '{title}' eliminata.")
    return redirect(f"{request.build_absolute_uri('/admin-auction/market/')}?league={league_id}")
