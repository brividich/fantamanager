"""The league admin's corner of the app: the console's dashboard, app-sized.

A league owner (or the superadmin) opening /app/ used to land on a manager's
screen — or on a login form when they had no team — and had to go back to
/dashboard/ for anything about the league. The Regia tab brings the same
picture into the app: what needs doing, the teams and their rosters, the
auctions and market sessions, and a way to look at the app through any team's
eyes ("Vedi come"). The two halves share the selected league through the
session, so switching league on one side is already done on the other.
"""
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import remote, services
from ..services import mail
from ..models import Auction, MarketBid, MarketSession, Participant, Player, Trade
from .admin_dashboard import _classifica_standings
from .admin_market import rule_choices, session_labels, session_manage_context, trades_manage_context
from .admin_participants import teams_manage_context
from .app import _redirect_login
from .common import (
    SESSION_LEAGUE_KEY,
    _app_active_auction,
    _app_ctx,
    app_admin_league,
    app_admin_leagues,
    participant_join_url,
    user_can_manage_league,
)

_LEVEL_ORDER = {"live": 0, "warn": 1, "info": 2, "ok": 3}
_ROLE_NAMES = (("P", "Portieri"), ("D", "Difensori"), ("C", "Centrocampisti"), ("A", "Attaccanti"))


def _todo(level, icon, title, text, url="", cta=""):
    return {"level": level, "icon": icon, "title": title, "text": text, "url": url, "cta": cta}


def league_admin_digest(league):
    """What ``league`` needs from its admin right now, most urgent first.

    One list for the app's Regia and for the admin's own home: a running
    auction, trades waiting for ratification, envelopes to open, and the setup
    gaps that would stop the next auction (no listone, no teams, teams nobody
    can log into). Ends with a single "all good" row when there's nothing.
    """
    if league is None:
        return []
    q = f"?league={league.id}"
    items = []

    auctions = Auction.objects.filter(league=league)
    running = auctions.filter(status__in=[Auction.Status.LIVE, Auction.Status.PAUSED]).order_by("-id").first()
    if running is not None:
        live = running.status == Auction.Status.LIVE
        items.append(_todo(
            "live", "gavel",
            ("Asta live: " if live else "Asta in pausa: ") + running.title,
            "I rilanci arrivano in tempo reale." if live else "Riprendila dalla regia quando la sala è pronta.",
            reverse("regia_auction", args=[running.id]), "Apri la regia"))

    to_ratify = Trade.objects.filter(league=league, status=Trade.Status.ACCEPTED).count()
    if to_ratify:
        items.append(_todo(
            "warn", "swap",
            f"{to_ratify} scambi{'o' if to_ratify == 1 else ''} da ratificare",
            "Le due squadre hanno accettato: tocca a te approvare o bocciare.",
            reverse("app_regia_trades") + q, "Ratifica"))

    services.sync_market_schedule(league)
    sessions = MarketSession.objects.filter(league=league)
    closed = sessions.filter(status=MarketSession.Status.CLOSED).first()
    if closed is not None:
        if closed.session_type in (MarketSession.SessionType.SEALED_BIDS, MarketSession.SessionType.REPAIR):
            items.append(_todo(
                "warn", "mail", f"Spoglio da fare: {closed.title}",
                "Le buste sono chiuse: controlla l'anteprima e assegna i giocatori.",
                reverse("app_regia_market_session", args=[closed.id]), "Vai allo spoglio"))
        else:
            labels = session_labels(closed)
            items.append(_todo(
                "warn", "mail", f"{labels['todo']}: {closed.title}",
                f"{labels['closed']}: riaprila o concludila con «{labels['resolve']}».",
                reverse("app_regia_market_session", args=[closed.id]), "Apri la sessione"))
    opened = sessions.filter(status=MarketSession.Status.OPEN).first()
    if opened is not None:
        delivered = (MarketBid.objects.filter(session=opened)
                     .values("participant_id").distinct().count())
        teams_n = Participant.objects.filter(league=league, is_active=True).count()
        when = f" · chiude il {opened.closes_at:%d/%m %H:%M}" if opened.closes_at else ""
        is_buste = opened.session_type in (MarketSession.SessionType.SEALED_BIDS, MarketSession.SessionType.REPAIR)
        items.append(_todo(
            "info", "mail", f"{'Buste aperte' if is_buste else 'Mercato aperto'}: {opened.title}",
            f"{delivered}/{teams_n} squadre {'hanno consegnato' if is_buste else 'hanno già agito'}{when}.",
            reverse("app_regia_market_session", args=[opened.id]), "Segui"))
    draft = sessions.filter(status=MarketSession.Status.DRAFT).first()
    if draft is not None:
        items.append(_todo(
            "info", "mail", f"Sessione in bozza: {draft.title}",
            "Le squadre non la vedono finché non la apri.",
            reverse("app_regia_market_session", args=[draft.id]), "Apri"))

    pool = Player.objects.filter(league=league).count()
    teams = Participant.objects.filter(league=league, is_active=True)
    teams_n = teams.count()
    if not pool:
        items.append(_todo(
            "warn", "import", "Carica il listone",
            "Senza listone non c'è niente da mettere all'asta.",
            reverse("admin_players") + q + "&need_listone=1", "Carica"))
    if not teams_n:
        items.append(_todo(
            "warn", "shield", "Aggiungi le squadre",
            "La lega non ha ancora squadre iscritte.",
            reverse("app_regia_teams") + q, "Aggiungi"))
    else:
        locked_out = teams.filter(user__isnull=True, access_code="").count()
        if locked_out:
            items.append(_todo(
                "info", "lock", f"{locked_out} squadr{'a' if locked_out == 1 else 'e'} senza accesso",
                "Né un account né un PIN: dai loro un codice o il link di invito.",
                reverse("app_regia_teams") + q, "Sistema"))
    if pool and teams_n and not auctions.exists():
        items.append(_todo(
            "info", "plus", "Crea la prima asta",
            "Listone e squadre ci sono: manca solo l'asta.",
            reverse("admin_auction_wizard") + q, "Crea"))
    left = Player.objects.filter(owner__league=league, left_serie_a_at__isnull=False).count()
    if left:
        items.append(_todo(
            "warn", "doc", f"{left} giocator{'e' if left == 1 else 'i'} fuori dal listone",
            "In rosa ma non più nel listone ufficiale: conferma destinazione e compenso (5.05).",
            reverse("admin_contracts") + q, "Gestisci"))
    if league.contracts_enabled and league.renewals_open:
        items.append(_todo(
            "info", "doc", "Finestra rinnovi aperta",
            "Le squadre stanno dichiarando e tirando i dadi dei rinnovi.",
            reverse("admin_contracts") + q, "Segui"))

    if not items:
        items.append(_todo("ok", "star", "Tutto in ordine",
                           "Nessuna azione in sospeso per questa lega."))
    items.sort(key=lambda t: _LEVEL_ORDER[t["level"]])
    return items


def _team_rows(request, league, viewer):
    """Every team of ``league`` with credits, roster fill and roster by role."""
    players = Player.objects.filter(league=league)
    participants = _classifica_standings(
        league, players, Participant.objects.filter(league=league).select_related("user"))
    by_owner = {}
    for pl in players.filter(owner__isnull=False).order_by("name"):
        by_owner.setdefault(pl.owner_id, []).append(pl)
    user_id = getattr(request.user, "id", None)
    rows = []
    for p in participants:
        roster = by_owner.get(p.id, [])
        rows.append({
            "team": p,
            "groups": [{"code": code, "label": label, "list": [pl for pl in roster if pl.role == code]}
                       for code, label in _ROLE_NAMES],
            "is_mine": p.user_id is not None and p.user_id == user_id,
            "is_viewing": viewer is not None and viewer.id == p.id,
            "join_url": participant_join_url(request, p),
        })
    # Richest roster first, like the console's classifica.
    rows.sort(key=lambda r: (-r["team"].roster_n, -float(r["team"].remaining_credits),
                             r["team"].display_name.lower()))
    return rows


def app_regia(request):
    """The Regia tab: the console's league dashboard inside the app."""
    participant, ctx = _app_ctx(request, "regia")
    if ctx is None:
        return _redirect_login(request, ctx)
    league = app_admin_league(request, ctx["admin_leagues"], participant)
    if league is None:
        messages.error(request, "La Regia è per chi gestisce una lega: il tuo account non ne gestisce nessuna.")
        return redirect("app_home")

    teams = _team_rows(request, league, participant)
    spent = sum((r["team"].spent_credits or Decimal("0")) for r in teams)
    budget = sum((r["team"].credits or Decimal("0")) for r in teams)
    assigned = sum(r["team"].roster_n for r in teams)
    slots = (league.total_slots or 0) * len(teams)
    pool = Player.objects.filter(league=league)

    trades = (Trade.objects.filter(league=league, status=Trade.Status.ACCEPTED)
              .select_related("proposer", "receiver")
              .prefetch_related("proposer_players", "receiver_players"))
    auctions = list(Auction.objects.filter(league=league).order_by("-id")[:6])
    sessions = list(MarketSession.objects.filter(league=league).order_by("-created_at")[:3])
    for s in sessions:
        s.mk_labels = session_labels(s)
    from ..services.competitions import ensure_league_season_and_competitions
    season, competitions = ensure_league_season_and_competitions(league) if league else (None, [])

    ctx.update({
        # The shell follows the league being run, not the viewed team's.
        "app_league": league,
        "active_auction": _app_active_auction(league),
        "manages_app_league": True,
        "todo": league_admin_digest(league),
        "teams": teams,
        "kpi": {
            "teams": len(teams),
            "spent": spent,
            "budget": budget,
            "spent_pct": int(100 * spent / budget) if budget else 0,
            "assigned": assigned,
            "slots": slots,
            "fill_pct": int(100 * assigned / slots) if slots else 0,
            "pool": pool.count(),
            "free": pool.filter(owner__isnull=True).count(),
        },
        "trades_to_ratify": list(trades),
        "auctions": auctions,
        "market_sessions": sessions,
        "competitions": competitions,
        "team_elsewhere": participant is not None and participant.league_id != league.id,
        "league_q": f"?league={league.id}",
        # The «Nuovo Mercato» wizard, shared with the console (_market_wizard.html).
        "mail_ready": mail.is_ready(),
        "open_wizard": request.GET.get("open_wizard") == "1",
        **rule_choices(),
    })
    return render(request, "auctions/app_regia.html", ctx)


@require_POST
def app_view_as(request, participant_id):
    """Open the app as one of the admin's teams — to check what a manager sees,
    or to act for a team that can't. Only teams of leagues the user runs."""
    admin_ids = {lg.id for lg in app_admin_leagues(request.user)}
    team = Participant.objects.filter(pk=participant_id, is_active=True).first()
    if team is None or team.league_id not in admin_ids:
        messages.error(request, "Puoi aprire solo le squadre delle leghe che gestisci.")
        return redirect("app_regia")
    request.session["participant_id"] = team.id
    request.session["display_name"] = team.display_name
    request.session[SESSION_LEAGUE_KEY] = team.league_id
    # No flash message: the shell's "Vista admin" banner already says it.
    return redirect("app_home")


@require_POST
def app_view_as_exit(request):
    """Back to the admin's own identity: their team if they have one, else the
    Regia (``_session_participant`` falls back to the account's team)."""
    viewed_league = request.session.get(SESSION_LEAGUE_KEY)
    request.session.pop("participant_id", None)
    request.session.pop("display_name", None)
    # Land on the Regia of the league whose team was being viewed.
    suffix = f"?league={viewed_league}" if viewed_league else ""
    return redirect(reverse("app_regia") + suffix)


def app_regia_market_session(request, session_id):
    """One market session, managed from the app: the console's own screen
    (market/_session_manage.html) inside the app shell."""
    participant, ctx = _app_ctx(request, "regia")
    if ctx is None:
        return _redirect_login(request, ctx)
    session = get_object_or_404(MarketSession.objects.select_related("league"), pk=session_id)
    league = session.league
    if not user_can_manage_league(request.user, league):
        return HttpResponseForbidden("Non hai i permessi per gestire il mercato di questa lega.")
    request.session[SESSION_LEAGUE_KEY] = league.id
    ctx.update(session_manage_context(request, session))
    ctx.update({
        "app_league": league,
        "active_auction": _app_active_auction(league),
        "manages_app_league": True,
        "mk_back": reverse("app_regia_market_session", args=[session.id]),
        "mk_list": f"{reverse('app_regia')}?league={league.id}",
    })
    return render(request, "auctions/app_regia_session.html", ctx)


def app_regia_trades(request):
    """The league's trades, managed from the app: the console's own screen
    (market/_trades_manage.html) inside the app shell."""
    participant, ctx = _app_ctx(request, "regia")
    if ctx is None:
        return _redirect_login(request, ctx)
    league = app_admin_league(request, ctx["admin_leagues"], participant)
    if league is None:
        messages.error(request, "La Regia è per chi gestisce una lega: il tuo account non ne gestisce nessuna.")
        return redirect("app_home")
    ctx.update(trades_manage_context(league))
    ctx.update({
        "app_league": league,
        "active_auction": _app_active_auction(league),
        "manages_app_league": True,
        "mk_back": f"{reverse('app_regia_trades')}?league={league.id}",
    })
    return render(request, "auctions/app_regia_trades.html", ctx)


def app_regia_teams(request):
    """The league's teams, managed from the app: the console's own screen
    (_teams_manage.html) inside the app shell."""
    participant, ctx = _app_ctx(request, "regia")
    if ctx is None:
        return _redirect_login(request, ctx)
    league = app_admin_league(request, ctx["admin_leagues"], participant)
    if league is None:
        messages.error(request, "La Regia è per chi gestisce una lega: il tuo account non ne gestisce nessuna.")
        return redirect("app_home")
    # Join links and coaches' accounts: through the internet tunnel the
    # console asks for the regia PIN first, and so does this page.
    if remote.request_is_remote(request) and not request.session.get("regia_unlocked"):
        return redirect(f"{reverse('regia_unlock')}?{urlencode({'next': request.get_full_path()})}")
    ctx.update(teams_manage_context(request, league))
    ctx.update({
        "app_league": league,
        "active_auction": _app_active_auction(league),
        "manages_app_league": True,
    })
    return render(request, "auctions/app_regia_teams.html", ctx)
