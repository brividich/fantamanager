"""Admin views for the Mercato hub: sealed-envelope sessions, trades, repair auctions."""
from datetime import datetime, timedelta

from django.contrib import messages
from django.db.models import Count
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.http import require_POST

from ..models import (
    Auction, MarketBid, MarketSession, Participant, Player, RosterLog, Trade, TradeWindow,
)
from ..services import mail
from ..services.trade import decide_trade
from ..services.market import (
    plan_market_resolution,
    resolve_market_session,
    settle_market_tie,
    sync_market_schedule,
    undo_market_resolution,
)
from .common import (
    SESSION_LEAGUE_KEY,
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


# The market screens, in the order of the hub's cards.
MARKET_TABS = ("buste", "scambi", "asta", "movimenti")
_TAB_URL = {
    "buste": "admin_market_buste",
    "scambi": "admin_market_trades",
    "asta": "admin_market_repair",
    "movimenti": "admin_market_moves",
}


def _dashboard_url(request, session=None, league_id=None, tab=None):
    """Where an action lands afterwards: the session's own screen, a market's
    screen (``tab``) or the hub."""
    if session is not None:
        return reverse("admin_market_session", args=[session.id])
    name = _TAB_URL.get(tab, "admin_market_dashboard")
    url = reverse(name)
    return f"{url}?league={league_id}" if league_id else url


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


# RosterLog actions grouped the way the «Movimenti» filter chips read them.
_MOVE_KIND = {
    RosterLog.Action.ASSIGN: "acquisti", RosterLog.Action.ADMIN_ASSIGN: "acquisti",
    RosterLog.Action.RELEASE: "svincoli", RosterLog.Action.ADMIN_RELEASE: "svincoli",
    RosterLog.Action.TRADE: "scambi", RosterLog.Action.EDIT: "altro",
}


def _buste_summary(sessions):
    """One line for the Buste card: what needs the admin's eye first."""
    by_status = {}
    for s in sessions:
        by_status.setdefault(s.status, s)  # sessions come newest first
    S = MarketSession.Status
    if S.OPEN in by_status:
        s = by_status[S.OPEN]
        return {"tone": "live", "label": "Aperta",
                "text": f"{s.title}" + (f" · chiude il {timezone.localtime(s.closes_at):%d/%m %H:%M}" if s.closes_at else "")}
    if S.CLOSED in by_status:
        s = by_status[S.CLOSED]
        return {"tone": "warn", "label": "Da scrutinare", "text": s.title}
    if S.DRAFT in by_status:
        s = by_status[S.DRAFT]
        return {"tone": "info", "label": "Programmata",
                "text": s.title + (f" · apre il {timezone.localtime(s.opens_at):%d/%m %H:%M}" if s.opens_at else "")}
    if sessions:
        return {"tone": "off", "label": "Chiuse", "text": f"Ultima: {sessions[0].title}"}
    return {"tone": "off", "label": "Nessuna", "text": "Nessuna sessione creata"}


# --- Data of each market -----------------------------------------------------
# Every screen loads only what it shows; the hub reads the same helpers for
# the one-line state of each card.

def _league_sessions(league):
    """The league's buste sessions, newest first, with envelopes and teams
    that delivered counted."""
    sync_market_schedule(league)
    return list(
        MarketSession.objects.filter(league=league)
        .annotate(n_bids=Count("bids"), n_teams=Count("bids__participant", distinct=True))
        .order_by("-created_at")
    )


def _trades_data(league, now, full=False):
    trades = Trade.objects.filter(league=league).select_related("proposer", "receiver")
    windows = list(TradeWindow.objects.filter(league=league))
    data = {
        "trades_pending": list(
            trades.filter(status=Trade.Status.ACCEPTED).prefetch_related("proposer_players", "receiver_players")
        ),
        "trades_proposed": trades.filter(status=Trade.Status.PENDING).count(),
        "trades_done": trades.filter(status=Trade.Status.COMPLETED).count(),
        "trade_windows": windows,
        "window_now": next((w for w in windows if w.opens_at <= now <= w.closes_at), None),
        "window_next": next((w for w in windows if w.opens_at > now), None),
    }
    # Trades are open when the league allows them and, if it set periods,
    # one of them is running now.
    data["trades_open"] = bool(league.trades_enabled and (not windows or data["window_now"]))
    if full:
        data["trades_recent"] = list(trades.exclude(status=Trade.Status.ACCEPTED)[:10])
    return data


def _repair_data(league):
    auctions = list(Auction.objects.filter(league=league).order_by("-created_at")[:12])
    free = dict(
        Player.objects.filter(league=league, owner__isnull=True)
        .values("role").annotate(n=Count("id")).values_list("role", "n")
    )
    return {
        "league_auctions": auctions,
        "repair_auctions": [a for a in auctions if a.mode == Auction.Mode.REPAIR_AUCTION],
        "active_auction": next(
            (a for a in auctions if a.status in (Auction.Status.LIVE, Auction.Status.PAUSED)), None
        ),
        "free_by_role": [(r, free.get(r, 0)) for r in "PDCA"],
        "free_total": sum(free.values()),
    }


def _moves_data(league, now, limit=80):
    log = RosterLog.objects.filter(participant__league=league)
    moves = []
    for m in log[:limit]:
        m.kind = _MOVE_KIND.get(m.action, "altro")
        # credits_delta is what the team spent: > 0 a cost, < 0 a refund.
        m.spent = m.credits_delta if m.credits_delta > 0 else 0
        m.refund = -m.credits_delta if m.credits_delta < 0 else 0
        moves.append(m)
    return {"moves": moves, "moves_recent": log.filter(created_at__gte=now - timedelta(days=30)).count()}


def _market_page(request, template, active, league, extra):
    """Render one Mercato screen with the context every screen shares."""
    ctx = {
        "leagues": manageable_leagues(request.user),
        "current_league": league,
        "market_active": active,
        "now": timezone.now(),
        "console_section": "Mercato",
        "console_active": "market",
        "mail_ready": mail.is_ready(),
        # Choices of the session rules form (_market_rules_fields.html).
        "refund_modes": Auction.RefundMode.choices,
        "budget_rules": MarketSession.BudgetRule.choices,
        "tie_breaks": MarketSession.TieBreak.choices,
        "role_caps": [("P", "max_acquisitions_p"), ("D", "max_acquisitions_d"),
                      ("C", "max_acquisitions_c"), ("A", "max_acquisitions_a")],
    }
    ctx.update(extra)
    return render(request, template, ctx)


def _league_or_403(request):
    """``(league, None)`` for the console's league, or ``(None, response)``."""
    league = current_league(request)
    if league is not None and not user_can_manage_league(request.user, league):
        return None, HttpResponseForbidden(_FORBIDDEN_MSG)
    return league, None


def _legacy_redirect(request):
    """Links of the old one-page Mercato (``?session=``/``?tab=``) now open
    the screen they meant."""
    sess = (request.GET.get("session") or "").strip()
    if sess.isdigit():
        url = reverse("admin_market_session", args=[int(sess)])
        keep = {k: request.GET[k] for k in ("preview", "reveal") if request.GET.get(k)}
        return redirect(f"{url}?{urlencode(keep)}" if keep else url)
    tab = (request.GET.get("tab") or "").strip().lower()
    if tab in _TAB_URL:
        league = (request.GET.get("league") or "").strip()
        url = reverse(_TAB_URL[tab])
        return redirect(f"{url}?league={league}" if league.isdigit() else url)
    return None


@staff_member_required
def admin_market_dashboard(request):
    """The Mercato hub: which markets are running right now and one card per
    market. The content of a market opens in its own screen."""
    legacy = _legacy_redirect(request)
    if legacy is not None:
        return legacy
    league, denied = _league_or_403(request)
    if denied:
        return denied
    extra = {}
    if league:
        now = timezone.now()
        sessions = _league_sessions(league)
        teams = Participant.objects.filter(league=league).count()
        repair = _repair_data(league)
        trades = _trades_data(league, now)
        moves = _moves_data(league, now, limit=1)
        S = MarketSession.Status
        live_sessions = [s for s in sessions if s.status in (S.OPEN, S.CLOSED, S.DRAFT)]
        # The open window first, then the one waiting for its count, then the
        # scheduled ones.
        order = {S.OPEN: 0, S.CLOSED: 1, S.DRAFT: 2}
        live_sessions.sort(key=lambda s: order[s.status])
        extra = {
            "sessions": sessions,
            "live_sessions": live_sessions,
            "n_teams": teams,
            "buste_summary": _buste_summary(sessions),
            "last_move": moves["moves"][0] if moves["moves"] else None,
            "moves_recent": moves["moves_recent"],
            **repair,
            **trades,
        }
        extra["n_live"] = (
            len(live_sessions) + bool(trades["trades_open"] or trades["trades_pending"])
            + bool(repair["active_auction"])
        )
    return _market_page(request, "auctions/market/hub.html", "hub", league, extra)


@staff_member_required
def admin_market_buste(request):
    """Buste: the league's sessions; each one opens in its own screen."""
    league, denied = _league_or_403(request)
    if denied:
        return denied
    extra = {}
    if league:
        sessions = _league_sessions(league)
        S = MarketSession.Status
        extra = {
            "sessions": sessions,
            "active_sessions": sorted(
                (s for s in sessions if s.status != S.RESOLVED),
                key=lambda s: {S.OPEN: 0, S.CLOSED: 1}.get(s.status, 2),
            ),
            "past_sessions": [s for s in sessions if s.status == S.RESOLVED],
            "n_teams": Participant.objects.filter(league=league).count(),
            "free_total": Player.objects.filter(league=league, owner__isnull=True).count(),
        }
    return _market_page(request, "auctions/market/buste.html", "buste", league, extra)


@staff_member_required
def admin_market_session(request, session_id):
    """One buste session: deliveries, preview, count, ties and envelopes."""
    session, denied = _managed_session_or_403(request, session_id)
    if denied:
        return denied
    league = session.league
    request.session[SESSION_LEAGUE_KEY] = league.id
    sync_market_schedule(league)
    session = (
        MarketSession.objects.filter(pk=session.pk)
        .annotate(n_bids=Count("bids")).select_related("league").first()
    )

    participants_stats = []
    delivered = 0
    bid_counts = dict(
        MarketBid.objects.filter(session=session)
        .values("participant_id").annotate(cnt=Count("id")).values_list("participant_id", "cnt")
    )
    for p in Participant.objects.filter(league=league).annotate(roster_n=Count("roster")).order_by("display_name"):
        cnt = bid_counts.get(p.id, 0)
        delivered += cnt > 0
        participants_stats.append({"participant": p, "bids_count": cnt, "has_submitted": cnt > 0})

    bids_list = []
    if request.GET.get("reveal") == "1" or session.status == MarketSession.Status.RESOLVED:
        bids_list = list(
            session.bids.select_related("participant", "player", "release_player")
            .order_by("player__role", "player__name", "-amount", "priority")
        )

    results, is_preview = None, False
    if session.status == MarketSession.Status.RESOLVED:
        results = session.results_summary or None
    elif request.GET.get("preview") == "1":
        results = plan_market_resolution(session.id)
        is_preview = True

    repair = _repair_data(league)
    return _market_page(request, "auctions/market/session.html", "buste", league, {
        "s": session,
        "participants_stats": participants_stats,
        "delivered": delivered,
        "bids_list": bids_list,
        "results": results,
        "is_preview": is_preview,
        "free_total": repair["free_total"],
        "free_by_role": repair["free_by_role"],
        "reachable": len(mail.league_recipients(league)),
    })


@staff_member_required
def admin_market_trades(request):
    """Scambi: ratifications, history, rules and trade windows."""
    league, denied = _league_or_403(request)
    if denied:
        return denied
    extra = _trades_data(league, timezone.now(), full=True) if league else {}
    return _market_page(request, "auctions/market/scambi.html", "scambi", league, extra)


@staff_member_required
def admin_market_repair(request):
    """Asta di riparazione: the league's auctions and the free agents left."""
    league, denied = _league_or_403(request)
    if denied:
        return denied
    extra = _repair_data(league) if league else {}
    return _market_page(request, "auctions/market/asta.html", "asta", league, extra)


@staff_member_required
def admin_market_moves(request):
    """Movimenti: the roster log of the league's teams, newest first."""
    league, denied = _league_or_403(request)
    if denied:
        return denied
    extra = _moves_data(league, timezone.now()) if league else {}
    return _market_page(request, "auctions/market/movimenti.html", "movimenti", league, extra)


def _parse_int(val):
    try:
        return max(0, int(val))
    except (ValueError, TypeError):
        return 0


def _session_rules(post):
    """The rule fields of a MarketSession from the create/edit form."""
    refund_mode = post.get("refund_mode") or Auction.RefundMode.PURCHASE
    if refund_mode not in Auction.RefundMode.values:
        refund_mode = Auction.RefundMode.PURCHASE
    budget_rule = post.get("budget_rule")
    if budget_rule not in MarketSession.BudgetRule.values:
        budget_rule = MarketSession.BudgetRule.PRIORITY
    tie_break = post.get("tie_break")
    if tie_break not in MarketSession.TieBreak.values:
        tie_break = MarketSession.TieBreak.MANUAL
    return {
        "allow_conditional_release": post.get("allow_conditional_release") == "1",
        "require_same_role_release": post.get("require_same_role_release") == "1",
        "release_refund_mode": refund_mode,
        "max_bids": _parse_int(post.get("max_bids")),
        "budget_rule": budget_rule,
        "tie_break": tie_break,
        "max_acquisitions_p": _parse_int(post.get("max_acquisitions_p")),
        "max_acquisitions_d": _parse_int(post.get("max_acquisitions_d")),
        "max_acquisitions_c": _parse_int(post.get("max_acquisitions_c")),
        "max_acquisitions_a": _parse_int(post.get("max_acquisitions_a")),
    }


@staff_member_required
@require_POST
def admin_market_create(request):
    """Create a new MarketSession for the current league."""
    league = target_league(request) or current_league(request)
    if not league:
        messages.error(request, "Nessuna lega selezionata per la sessione di mercato.")
        return redirect("admin_market_buste")
    if not user_can_manage_league(request.user, league):
        return HttpResponseForbidden(_FORBIDDEN_MSG)

    title = (request.POST.get("title") or "Mercato di Riparazione a Buste").strip()

    opens_at = _parse_local_datetime(request.POST.get("opens_at"))
    closes_at = _parse_local_datetime(request.POST.get("closes_at"))
    if opens_at and closes_at and closes_at <= opens_at:
        messages.error(request, "La chiusura deve essere successiva all'apertura.")
        return redirect(_dashboard_url(request, league_id=league.id, tab="buste"))
    scheduled = opens_at is not None and opens_at > timezone.now()

    session = MarketSession.objects.create(
        league=league,
        title=title,
        status=MarketSession.Status.DRAFT if scheduled else MarketSession.Status.OPEN,
        opens_at=opens_at,
        closes_at=closes_at,
        **_session_rules(request.POST),
    )

    if request.POST.get("notify") == "1":
        report = mail.send_market_notice(request, session)
        (messages.success if report["sent"] and not report["failed"] else messages.warning)(
            request, "Avviso alle squadre: " + mail.report_message(report))

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
def admin_market_notify(request, session_id):
    """Email the league's teams that the session is open (or coming)."""
    session, denied = _managed_session_or_403(request, session_id)
    if denied:
        return denied
    if session.status == MarketSession.Status.RESOLVED:
        messages.error(request, "La sessione è già stata scrutinata: niente da annunciare.")
        return redirect(_dashboard_url(request, session))
    if not mail.is_ready():
        messages.error(request, "La posta non è configurata: impostala in Impostazioni → Posta.")
        return redirect(_dashboard_url(request, session))
    report = mail.send_market_notice(request, session)
    (messages.success if report["sent"] and not report["failed"] else messages.warning)(
        request, "Avviso alle squadre: " + mail.report_message(report))
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
    return redirect(_dashboard_url(request, league_id=league_id, tab="buste"))


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

    rebids = {}
    for key, value in request.POST.items():
        if key.startswith("rebid_") and key[6:].isdigit() and value.strip():
            rebids[int(key[6:])] = value.strip()
    if rebids:
        winner_id = None
    res = settle_market_tie(session.id, player_id, winner_id=winner_id, rebids=rebids or None)
    if res["ok"]:
        how = {"draw": "per sorteggio", "rebid": "al secondo sfoglio"}.get(res["method"], "per scelta dell'admin")
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
    league.trades_same_roles = request.POST.get("trades_same_roles") == "1"
    league.save(update_fields=["trades_enabled", "trades_need_approval", "trades_same_roles", "updated_at"])
    messages.success(request, "Impostazioni scambi salvate.")
    return redirect(_dashboard_url(request, league_id=league.id, tab="scambi"))


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
    # The Regia in the app ratifies from its own page and wants to stay there.
    nxt = request.POST.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return redirect(nxt)
    return redirect(_dashboard_url(request, league_id=trade.league_id, tab="scambi"))


@staff_member_required
@require_POST
def admin_market_rules(request, session_id):
    """Edit the rules of a session that has not been resolved yet."""
    session, denied = _managed_session_or_403(request, session_id)
    if denied:
        return denied
    if session.status == MarketSession.Status.RESOLVED:
        messages.error(request, "Lo spoglio è già stato eseguito: annullalo prima di cambiare le regole.")
        return redirect(_dashboard_url(request, session))
    title = (request.POST.get("title") or "").strip()
    rules = _session_rules(request.POST)
    for field, value in rules.items():
        setattr(session, field, value)
    fields = list(rules) + ["updated_at"]
    if title:
        session.title = title
        fields.append("title")
    closes_at = _parse_local_datetime(request.POST.get("closes_at"))
    if request.POST.get("closes_at") is not None:
        session.closes_at = closes_at
        fields.append("closes_at")
    session.save(update_fields=fields)
    messages.success(request, f"Regole della sessione '{session.title}' aggiornate.")
    return redirect(_dashboard_url(request, session))


@staff_member_required
@require_POST
def admin_trade_window_add(request):
    league = target_league(request) or current_league(request)
    if league is None or not user_can_manage_league(request.user, league):
        return HttpResponseForbidden(_FORBIDDEN_MSG)
    opens_at = _parse_local_datetime(request.POST.get("opens_at"))
    closes_at = _parse_local_datetime(request.POST.get("closes_at"))
    if not opens_at or not closes_at or closes_at <= opens_at:
        messages.error(request, "Indica apertura e chiusura del periodo (la chiusura dopo l'apertura).")
        return redirect(_dashboard_url(request, league_id=league.id, tab="scambi"))
    TradeWindow.objects.create(
        league=league, opens_at=opens_at, closes_at=closes_at,
        name=(request.POST.get("name") or "Periodo scambi").strip()[:80],
    )
    messages.success(request, "Periodo scambi aggiunto: fuori dai periodi gli scambi sono chiusi.")
    return redirect(_dashboard_url(request, league_id=league.id, tab="scambi"))


@staff_member_required
@require_POST
def admin_trade_window_delete(request, window_id):
    window = get_object_or_404(TradeWindow.objects.select_related("league"), pk=window_id)
    if not user_can_manage_league(request.user, window.league):
        return HttpResponseForbidden(_FORBIDDEN_MSG)
    league_id = window.league_id
    window.delete()
    messages.info(request, "Periodo scambi eliminato.")
    return redirect(_dashboard_url(request, league_id=league_id, tab="scambi"))
