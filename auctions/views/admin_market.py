"""Admin views for the Mercato hub: sealed-envelope sessions, trades, repair auctions."""
from datetime import datetime, timedelta

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.http import require_POST

from ..models import (
    Auction, ContractEvent, MarketBid, MarketSession, Participant, Player, RosterLog, Trade, TradeWindow,
)
from ..services import mail
from ..services.contracts import contract_rules
from ..services.trade import decide_trade
from ..services.market import (
    build_buste_csv,
    get_buste_report_context,
    plan_market_resolution,
    resolve_market_session,
    settle_market_tie,
    session_moves,
    sync_market_schedule,
    sync_renewals_window,
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


def _back(request, fallback):
    """Where an action lands: the ``next`` page it came from (the console or
    the app's Regia, both show the same partials), else ``fallback``."""
    nxt = (request.POST.get("next") or "").strip()
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return nxt
    return fallback


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
        return {"tone": "warn", "label": session_labels(s)["todo"], "text": s.title}
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
    """The league's market sessions, newest first, each with the words of its
    kind (``mk_labels``) and what the teams handed in counted: envelopes or
    claims, or, for free agency and clauses, the purchases in the roster log
    (as in session_manage_context)."""
    sync_market_schedule(league)
    sessions = list(
        MarketSession.objects.filter(league=league)
        .annotate(n_bids=Count("bids"), n_teams=Count("bids__participant", distinct=True))
        .order_by("-created_at")
    )
    for s in sessions:
        s.mk_labels = session_labels(s)
        if s.session_type in (_ST.FREE_AGENCY, _ST.BUYOUT_CLAUSE):
            moves = session_moves(s)
            s.n_bids = moves.count()
            s.n_teams = moves.values("participant_id").distinct().count()
    return sessions


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


def trades_manage_context(league):
    """Everything market/_trades_manage.html shows: the same data for the
    console page and for the app's Regia."""
    now = timezone.now()
    return {"current_league": league, "now": now, **_trades_data(league, now, full=True)}


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
        # ?new=1 opens the «Nuovo Mercato» wizard (the one shared with the app).
        "open_wizard": request.GET.get("new") == "1" or request.GET.get("open_wizard") == "1",
        # Choices of the session rules (wizard and _market_rules_fields.html).
        **rule_choices(),
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


_ST = MarketSession.SessionType
# What each kind of session calls its phases and what the teams hand in: the
# management screen (market/_session_manage.html), the session lists of the
# console and the app's Mercato speak the session's language.
_BUSTE_LABELS = {
    "kind": "Buste", "icon": "i-mail",
    "open": "Aperta alle offerte", "closed": "Consegna chiusa", "resolved": "Spoglio eseguito",
    "todo": "Da scrutinare", "acted": "hanno consegnato",
    "step_open": "Consegna", "step_end": "Spoglio",
    "close_hint": "Chiudi la consegna delle buste", "open_hint": "Apri subito la consegna delle buste",
    "deliveries": "Consegna delle buste", "item": "offerta", "items": "offerte", "done": "Consegnate",
    "pending": "In attesa", "preview": "Anteprima spoglio", "resolve": "Scrutina buste",
    "lost": "Buste non aggiudicate",
    "reveal": "Mostra le buste (solo admin)", "all_items": "Tutte le buste ricevute",
    "amount": "Offerta", "awaiting": "In attesa spoglio", "won_tag": "Vinta", "lost_tag": "Superata",
}
SESSION_LABELS = {
    _ST.SEALED_BIDS: _BUSTE_LABELS,
    _ST.REPAIR: _BUSTE_LABELS,
    _ST.LIVE_AUCTION: {**_BUSTE_LABELS, "kind": "Asta live", "icon": "i-gavel"},
    _ST.RENEWALS: {
        "kind": "Rinnovi", "icon": "i-doc",
        "open": "Rinnovi aperti", "closed": "Rinnovi sospesi", "resolved": "Rinnovi chiusi",
        "todo": "Rinnovi sospesi", "acted": "",
        "step_open": "Rinnovi", "step_end": "Chiusura",
        "close_hint": "Sospendi i rinnovi: le squadre non possono dichiarare né tirare i dadi",
        "open_hint": "Apri subito i rinnovi",
        "deliveries": "Rinnovi delle squadre", "item": "", "items": "", "done": "In regola",
        "pending": "", "preview": "", "resolve": "Chiudi & Risolvi Rinnovi", "lost": "",
        "reveal": "", "all_items": "", "amount": "", "awaiting": "", "won_tag": "", "lost_tag": "",
    },
    _ST.FREE_AGENCY: {
        "kind": "Mercato libero", "icon": "i-shirt",
        "open": "Acquisti aperti", "closed": "Acquisti sospesi", "resolved": "Finestra conclusa",
        "todo": "Acquisti sospesi", "acted": "hanno acquistato",
        "step_open": "Acquisti", "step_end": "Conclusa",
        "close_hint": "Sospendi gli acquisti", "open_hint": "Apri subito gli acquisti",
        "deliveries": "Acquisti delle squadre", "item": "acquisto", "items": "acquisti", "done": "Attiva",
        "pending": "Nessun acquisto", "preview": "", "resolve": "Concludi la finestra", "lost": "",
        "reveal": "", "all_items": "", "amount": "Costo", "awaiting": "", "won_tag": "", "lost_tag": "",
    },
    _ST.WAIVER_WIRE: {
        "kind": "Waiver", "icon": "i-list",
        "open": "Reclami aperti", "closed": "Reclami chiusi", "resolved": "Draft eseguito",
        "todo": "Draft da eseguire", "acted": "hanno inviato reclami",
        "step_open": "Reclami", "step_end": "Draft",
        "close_hint": "Chiudi la finestra dei reclami", "open_hint": "Apri subito i reclami",
        "deliveries": "Reclami delle squadre", "item": "reclamo", "items": "reclami", "done": "Inviati",
        "pending": "In attesa", "preview": "Anteprima draft", "resolve": "Esegui Draft Waiver",
        "lost": "Reclami non assegnati",
        "reveal": "Mostra i reclami (solo admin)", "all_items": "Tutti i reclami ricevuti",
        "amount": "Costo", "awaiting": "In attesa del draft", "won_tag": "Assegnato", "lost_tag": "Non assegnato",
    },
    _ST.BUYOUT_CLAUSE: {
        "kind": "Clausole", "icon": "i-coin",
        "open": "Clausole attive", "closed": "Clausole sospese", "resolved": "Finestra conclusa",
        "todo": "Clausole sospese", "acted": "hanno pagato clausole",
        "step_open": "Clausole", "step_end": "Conclusa",
        "close_hint": "Sospendi le clausole", "open_hint": "Attiva subito le clausole",
        "deliveries": "Clausole pagate dalle squadre", "item": "clausola", "items": "clausole", "done": "Attiva",
        "pending": "Nessuna clausola", "preview": "", "resolve": "Concludi la finestra", "lost": "",
        "reveal": "", "all_items": "", "amount": "Costo", "awaiting": "", "won_tag": "", "lost_tag": "",
    },
}


def session_labels(session):
    """The words of ``session``'s kind (SESSION_LABELS), buste by default."""
    return SESSION_LABELS.get(session.session_type, _BUSTE_LABELS)


def rule_choices():
    """Choices of the session rules, for the «Nuovo Mercato» wizard and the
    «Regole» form alike: one list, the same words in both."""
    return {
        "refund_modes": Auction.RefundMode.choices,
        "budget_rules": MarketSession.BudgetRule.choices,
        "tie_breaks": MarketSession.TieBreak.choices,
        "role_caps": [("P", "max_acquisitions_p"), ("D", "max_acquisitions_d"),
                      ("C", "max_acquisitions_c"), ("A", "max_acquisitions_a")],
    }


def _renewals_board(league, participants):
    """Where every team stands in a renewals market (regolamento 4): contracts
    to declare, renewal dice and contract dice still to roll, and how the
    season's renewals went so far."""
    players = list(
        Player.objects.filter(owner__league=league, abroad_list=False)
        .filter(Q(contract_years__isnull=True) | Q(contract_years=0))
    ) if league.contracts_enabled else []
    events = ContractEvent.objects.filter(
        league=league, season=league.season_number,
        kind__in=(ContractEvent.Kind.RENEWED, ContractEvent.Kind.RESCINDED, ContractEvent.Kind.NOT_RENEWED),
    ).values_list("participant_id", "kind")
    renewed, lost = {}, {}
    for pid, kind in events:
        bucket = renewed if kind == ContractEvent.Kind.RENEWED else lost
        bucket[pid] = bucket.get(pid, 0) + 1
    rows = []
    for p in participants:
        mine = [pl for pl in players if pl.owner_id == p.id]
        expiring = [pl for pl in mine if pl.contract_years == 0]
        row = {
            "participant": p,
            "expiring": len(expiring),
            "to_declare": sum(pl.renewal_declared is None for pl in expiring),
            "to_roll": sum(pl.renewal_declared is True for pl in expiring),
            "new_contracts": sum(pl.contract_years is None for pl in mine),
            "renewed": renewed.get(p.id, 0),
            "lost": lost.get(p.id, 0),
        }
        row["done"] = not (row["to_declare"] or row["to_roll"] or row["new_contracts"])
        rows.append(row)
    rules = contract_rules(league)
    return {
        "rows": rows,
        "done": sum(r["done"] for r in rows),
        "expiring": sum(r["expiring"] for r in rows),
        "to_declare": sum(r["to_declare"] for r in rows),
        "to_roll": sum(r["to_roll"] for r in rows),
        "new_contracts": sum(r["new_contracts"] for r in rows),
        "dice": sum(r["to_roll"] + r["new_contracts"] for r in rows),
        "renewed": sum(renewed.values()),
        "lost": sum(lost.values()),
        "faces": "-".join(str(f) for f in rules["faces"]),
        "u21_years": rules["u21_years"],
    }


def session_manage_context(request, session):
    """Everything market/_session_manage.html shows for ``session``: the same
    data for the console page and for the app's Regia."""
    league = session.league
    sync_market_schedule(league)
    session = (
        MarketSession.objects.filter(pk=session.pk)
        .annotate(n_bids=Count("bids")).select_related("league").first()
    )

    is_renewals = session.session_type == MarketSession.SessionType.RENEWALS
    labels = session_labels(session)
    participants_stats = []
    delivered = 0
    bid_counts = dict(
        (session_moves(session) if session.session_type in (_ST.FREE_AGENCY, _ST.BUYOUT_CLAUSE)
         else MarketBid.objects.filter(session=session))
        .values("participant_id").annotate(cnt=Count("id")).values_list("participant_id", "cnt")
    )
    # Free agency and clauses buy on the spot: what they "hand in" are the
    # purchases in the roster log, not envelopes.
    session.n_bids = sum(bid_counts.values())
    participants = list(
        Participant.objects.filter(league=league).annotate(roster_n=Count("roster")).order_by("display_name")
    )
    for p in participants:
        cnt = bid_counts.get(p.id, 0)
        delivered += cnt > 0
        participants_stats.append({"participant": p, "bids_count": cnt, "has_submitted": cnt > 0})
    renewals = _renewals_board(league, participants) if is_renewals else None
    if renewals:
        delivered = renewals["done"]

    bids_list = []
    if request.GET.get("reveal") == "1" or session.status == MarketSession.Status.RESOLVED:
        bids_list = list(
            session.bids.select_related("participant", "player", "release_player")
            .order_by("player__role", "player__name", "-amount", "priority")
        )

    results, is_preview = None, False
    if session.status == MarketSession.Status.RESOLVED:
        results = session.results_summary or None
    elif request.GET.get("preview") == "1" and labels["preview"]:
        results = plan_market_resolution(session.id)
        is_preview = True

    repair = _repair_data(league)
    return {
        "s": session,
        "current_league": league,
        "now": timezone.now(),
        "mk_labels": labels,
        "is_buste": session.session_type in (_ST.SEALED_BIDS, _ST.REPAIR),
        "is_renewals": is_renewals,
        "renewals": renewals,
        "mail_ready": mail.is_ready(),
        "participants_stats": participants_stats,
        "delivered": delivered,
        "bids_list": bids_list,
        "results": results,
        "is_preview": is_preview,
        "free_total": repair["free_total"],
        "free_by_role": repair["free_by_role"],
        "reachable": len(mail.league_recipients(league)),
        # Choices of the rules form (_market_rules_fields.html).
        **rule_choices(),
    }


@staff_member_required
def admin_market_session(request, session_id):
    """One buste session: deliveries, preview, count, ties and envelopes."""
    session, denied = _managed_session_or_403(request, session_id)
    if denied:
        return denied
    league = session.league
    request.session[SESSION_LEAGUE_KEY] = league.id
    ctx = session_manage_context(request, session)
    ctx["mk_back"] = reverse("admin_market_session", args=[session.id])
    ctx["mk_list"] = f"{reverse('admin_market_buste')}?league={league.id}"
    return _market_page(request, "auctions/market/session.html", "buste", league, ctx)


@staff_member_required
def admin_print_buste_report(request, session_id):
    """Visualizza e stampa in formato A4 / PDF il Verbale Ufficiale di Spoglio Buste."""
    session, denied = _managed_session_or_403(request, session_id)
    if denied:
        return denied
    ctx = get_buste_report_context(session.id)
    if not ctx:
        messages.error(request, "Impossibile generare il verbale per questa sessione.")
        return redirect("admin_market_session", session_id=session.id)
    ctx["back_url"] = reverse("admin_market_session", args=[session.id])
    ctx["csv_url"] = reverse("admin_export_buste_csv", args=[session.id])
    return render(request, "auctions/print_market_buste.html", ctx)


@staff_member_required
def admin_export_buste_csv(request, session_id):
    """Scarica il file CSV del Verbale Ufficiale di Spoglio Buste."""
    session, denied = _managed_session_or_403(request, session_id)
    if denied:
        return denied
    csv_bytes = build_buste_csv(session)
    name = f"Verbale_Spoglio_{session.id}_{session.league.id if session.league else 'mercato'}.csv"
    resp = HttpResponse(csv_bytes, content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


@staff_member_required
def admin_market_trades(request):
    """Scambi: ratifications, history, rules and trade windows."""
    league, denied = _league_or_403(request)
    if denied:
        return denied
    extra = _trades_data(league, timezone.now(), full=True) if league else {}
    if league:
        extra["mk_back"] = f"{reverse('admin_market_trades')}?league={league.id}"
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

    # Resolve session_type
    raw_type = (post.get("session_type") or post.get("market_kind") or "").strip()
    type_aliases = {
        "buste": MarketSession.SessionType.SEALED_BIDS,
        "repair": MarketSession.SessionType.REPAIR,
        "renewals": MarketSession.SessionType.RENEWALS,
        "live": MarketSession.SessionType.LIVE_AUCTION,
        "live_auction": MarketSession.SessionType.LIVE_AUCTION,
        "free_agency": MarketSession.SessionType.FREE_AGENCY,
        "waiver_wire": MarketSession.SessionType.WAIVER_WIRE,
        "buyout_clause": MarketSession.SessionType.BUYOUT_CLAUSE,
        "sealed_bids": MarketSession.SessionType.SEALED_BIDS,
        "SEALED_BIDS": MarketSession.SessionType.SEALED_BIDS,
        "LIVE_AUCTION": MarketSession.SessionType.LIVE_AUCTION,
        "FREE_AGENCY": MarketSession.SessionType.FREE_AGENCY,
        "WAIVER_WIRE": MarketSession.SessionType.WAIVER_WIRE,
        "BUYOUT_CLAUSE": MarketSession.SessionType.BUYOUT_CLAUSE,
        "REPAIR": MarketSession.SessionType.REPAIR,
        "RENEWALS": MarketSession.SessionType.RENEWALS,
    }
    session_type = type_aliases.get(raw_type, MarketSession.SessionType.REPAIR if raw_type == "repair" else MarketSession.SessionType.SEALED_BIDS)

    # Session-specific configuration payload
    try:
        multiplier = float(str(post.get("buyout_multiplier") or "1.5").replace(",", "."))
    except ValueError:
        multiplier = 1.5
    config = {
        # Campo vuoto = 0 (illimitati); 3 solo se il campo non c'è.
        "fa_max_moves": _parse_int(post.get("fa_max_moves", 3)),
        "fa_cost_type": post.get("fa_cost_type") if post.get("fa_cost_type") in ("quotation", "base") else "quotation",
        "fa_period": post.get("fa_period") if post.get("fa_period") in MarketSession.FA_PERIODS else "rolling",
        "waiver_order_type": (post.get("waiver_order_type")
                              if post.get("waiver_order_type") in ("inverse_standing", "rolling") else "inverse_standing"),
        "waiver_claim_hours": _parse_int(post.get("waiver_claim_hours") or 24),
        # La clausola costa almeno quanto il cartellino.
        "buyout_multiplier": max(1.0, multiplier) if multiplier == multiplier else 1.5,
        "buyout_min_hold_days": _parse_int(post.get("buyout_min_hold_days") or 7),
        "live_timer_seconds": _parse_int(post.get("live_timer_seconds") or 15),
        "description": (post.get("description") or "").strip(),
    }

    return {
        "session_type": session_type,
        "config": config,
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

    rules = _session_rules(request.POST)
    session_type = rules.pop("session_type", MarketSession.SessionType.REPAIR)

    default_title_map = {
        MarketSession.SessionType.RENEWALS: "Mercato Rinnovi Contratti",
        MarketSession.SessionType.FREE_AGENCY: "Finestra Free Agency",
        MarketSession.SessionType.WAIVER_WIRE: "Draft Waiver Wire a Turni",
        MarketSession.SessionType.BUYOUT_CLAUSE: "Sessione Clausole Rescissorie",
        MarketSession.SessionType.LIVE_AUCTION: "Asta Live di Riparazione",
    }
    default_title = default_title_map.get(session_type, "Mercato di Riparazione a Buste")
    title = (request.POST.get("title") or default_title).strip()

    # «Apri subito» in the wizard wins over a date left in the hidden field.
    opens_at = None if request.POST.get("open_timing") == "now" else _parse_local_datetime(request.POST.get("opens_at"))
    closes_at = _parse_local_datetime(request.POST.get("closes_at"))
    if opens_at and closes_at and closes_at <= opens_at:
        messages.error(request, "La chiusura deve essere successiva all'apertura.")
        return _create_back(request, league)
    if session_type == MarketSession.SessionType.WAIVER_WIRE and closes_at is None:
        # La finestra reclami dura quanto scelto nel wizard (24/48/72 ore).
        closes_at = (opens_at or timezone.now()) + timedelta(hours=rules["config"]["waiver_claim_hours"] or 24)
    scheduled = opens_at is not None and opens_at > timezone.now()

    status = MarketSession.Status.DRAFT if scheduled else MarketSession.Status.OPEN

    session = MarketSession.objects.create(
        league=league,
        session_type=session_type,
        title=title,
        status=status,
        opens_at=opens_at,
        closes_at=closes_at,
        **rules,
    )

    if session_type == MarketSession.SessionType.RENEWALS and status == MarketSession.Status.OPEN:
        sync_renewals_window({league.id})

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
    return _create_back(request, league, session)


def _create_back(request, league, session=None):
    """Dopo «Nuovo Mercato» (creato o rifiutato) si torna da dove si è partiti."""
    if request.POST.get("from") == "app" or request.POST.get("next") == "app":
        url = reverse("app_mercato")
        return redirect(f"{url}?session_id={session.id}" if session else url)
    if request.POST.get("from") == "regia":
        return redirect(f"{reverse('app_regia')}?league={league.id}")
    if session is None:
        return redirect(_dashboard_url(request, league_id=league.id, tab="buste"))
    return redirect(_back(request, _dashboard_url(request, session)))


@staff_member_required
@require_POST
def admin_market_status(request, session_id):
    """Toggle or update status of a market session (open/closed)."""
    session, denied = _managed_session_or_403(request, session_id)
    if denied:
        return denied
    new_status = (request.POST.get("status") or "").strip().lower()
    if session.status == MarketSession.Status.RESOLVED:
        messages.error(request, "Lo spoglio è già stato eseguito: annullalo prima di riaprire la sessione.")
        return redirect(_back(request, _dashboard_url(request, session)))
    if (new_status == MarketSession.Status.OPEN and session.closes_at
            and session.closes_at <= timezone.now()):
        messages.error(request, "La chiusura è già passata: sposta prima la chiusura nelle «Regole», poi riapri.")
        return redirect(_back(request, _dashboard_url(request, session)))
    if new_status in (MarketSession.Status.OPEN, MarketSession.Status.CLOSED):
        session.status = new_status
        session.save(update_fields=["status", "updated_at"])
        if session.session_type == MarketSession.SessionType.RENEWALS:
            sync_renewals_window({session.league_id})
        label = "aperta" if new_status == MarketSession.Status.OPEN else "chiusa"
        messages.success(request, f"Sessione '{session.title}' {label}.")
    return redirect(_back(request, _dashboard_url(request, session)))


@staff_member_required
@require_POST
def admin_market_notify(request, session_id):
    """Email the league's teams that the session is open (or coming)."""
    session, denied = _managed_session_or_403(request, session_id)
    if denied:
        return denied
    if session.status == MarketSession.Status.RESOLVED:
        messages.error(request, "La sessione è già stata scrutinata: niente da annunciare.")
        return redirect(_back(request, _dashboard_url(request, session)))
    if not mail.is_ready():
        messages.error(request, "La posta non è configurata: impostala in Impostazioni → Posta.")
        return redirect(_back(request, _dashboard_url(request, session)))
    report = mail.send_market_notice(request, session)
    (messages.success if report["sent"] and not report["failed"] else messages.warning)(
        request, "Avviso alle squadre: " + mail.report_message(report))
    return redirect(_back(request, _dashboard_url(request, session)))


@staff_member_required
@require_POST
def admin_market_resolve(request, session_id):
    """Scrutinize and resolve all envelopes for the session."""
    session, denied = _managed_session_or_403(request, session_id)
    if denied:
        return denied
    if session.status == MarketSession.Status.RESOLVED:
        messages.warning(request, f"La sessione '{session.title}' è già stata scrutinata.")
        return redirect(_back(request, _dashboard_url(request, session)))

    summary = resolve_market_session(session.id)
    won_count = summary.get("total_acquisitions", 0)
    ties_count = summary.get("total_ties", 0)
    if session.session_type == MarketSession.SessionType.WAIVER_WIRE:
        messages.success(
            request,
            f"Draft Waiver completato per '{session.title}': {won_count} calciatori assegnati secondo l'ordine di priorità.",
        )
    else:
        messages.success(
            request,
            f"Spoglio completato per '{session.title}': {won_count} acquisti assegnati, {ties_count} situazioni di pareggio.",
        )
    return redirect(_back(request, _dashboard_url(request, session)))


@staff_member_required
@require_POST
def admin_market_delete(request, session_id):
    """Delete a market session and its associated bids."""
    session, denied = _managed_session_or_403(request, session_id)
    if denied:
        return denied
    league_id = session.league_id
    league = session.league
    title = session.title
    # Solo una sessione rinnovi aperta tiene aperta la finestra: eliminarne
    # una vecchia non chiude quella aperta da «Nuova stagione».
    was_open_renewals = (session.session_type == MarketSession.SessionType.RENEWALS
                         and session.status == MarketSession.Status.OPEN)
    session.delete()
    if was_open_renewals and league:
        sync_renewals_window({league.id})
    messages.info(request, f"Sessione '{title}' eliminata.")
    return redirect(_back(request, _dashboard_url(request, league_id=league_id, tab="buste")))


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
    return redirect(_back(request, _dashboard_url(request, session)))


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
    return redirect(_back(request, _dashboard_url(request, session)))


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
    return redirect(_back(request, _dashboard_url(request, league_id=league.id, tab="scambi")))


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
    return redirect(_back(request, _dashboard_url(request, league_id=trade.league_id, tab="scambi")))


# Form field of each rule, where it isn't named like the model field.
_RULE_POST_KEYS = {"release_refund_mode": "refund_mode"}


@staff_member_required
@require_POST
def admin_market_rules(request, session_id):
    """Edit the rules of a session that has not been resolved yet."""
    session, denied = _managed_session_or_403(request, session_id)
    if denied:
        return denied
    if session.status == MarketSession.Status.RESOLVED:
        messages.error(request, "Lo spoglio è già stato eseguito: annullalo prima di cambiare le regole.")
        return redirect(_back(request, _dashboard_url(request, session)))
    title = (request.POST.get("title") or "").strip()
    post = request.POST
    rules = _session_rules(post)
    # The type is chosen at creation and never changes; each type's form
    # (_market_rules_fields.html) sends only its own settings, so only those
    # change: the rest of the rules and of the config stay as they were.
    rules.pop("session_type")
    config = dict(session.config or {})
    config.update({key: value for key, value in rules.pop("config").items() if key in post})
    session.config = config
    fields = ["config", "updated_at"]
    checks = set(post.getlist("checks"))
    for field, value in rules.items():
        key = _RULE_POST_KEYS.get(field, field)
        if key in post or field in checks:
            setattr(session, field, value)
            fields.append(field)
    if title:
        session.title = title
        fields.append("title")
    closes_at = _parse_local_datetime(request.POST.get("closes_at"))
    if request.POST.get("closes_at") is not None:
        if closes_at and session.opens_at and closes_at <= session.opens_at:
            messages.error(request, "La chiusura deve essere successiva all'apertura.")
            return redirect(_back(request, _dashboard_url(request, session)))
        session.closes_at = closes_at
        fields.append("closes_at")
    session.save(update_fields=fields)
    messages.success(request, f"Regole della sessione '{session.title}' aggiornate.")
    return redirect(_back(request, _dashboard_url(request, session)))


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
        return redirect(_back(request, _dashboard_url(request, league_id=league.id, tab="scambi")))
    TradeWindow.objects.create(
        league=league, opens_at=opens_at, closes_at=closes_at,
        name=(request.POST.get("name") or "Periodo scambi").strip()[:80],
    )
    messages.success(request, "Periodo scambi aggiunto: fuori dai periodi gli scambi sono chiusi.")
    return redirect(_back(request, _dashboard_url(request, league_id=league.id, tab="scambi")))


@staff_member_required
@require_POST
def admin_trade_window_delete(request, window_id):
    window = get_object_or_404(TradeWindow.objects.select_related("league"), pk=window_id)
    if not user_can_manage_league(request.user, window.league):
        return HttpResponseForbidden(_FORBIDDEN_MSG)
    league_id = window.league_id
    window.delete()
    messages.info(request, "Periodo scambi eliminato.")
    return redirect(_back(request, _dashboard_url(request, league_id=league_id, tab="scambi")))
