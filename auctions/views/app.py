"""Product shell (mobile-first FantaManager app; session-participant identity)."""
import json
from django.core.paginator import Paginator
from django.db.models import Case, F, Q, Value, When
from django.db.models.functions import Coalesce
from django.shortcuts import redirect, render

from django.contrib import messages
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..models import (
    Auction,
    Formation,
    Giornata,
    GiornataScore,
    League,
    MarketBid,
    MarketSession,
    Participant,
    Player,
    PlayerPerformance,
    Season,
    Trade,
)
from .. import services
from .common import (
    SESSION_LEAGUE_KEY,
    _ROLE_LABELS,
    _app_ctx,
    _app_standings,
    _session_participant,
)


def _redirect_login(request):
    return redirect(f"{reverse('app_login')}?next={request.path}")


def app_home(request):
    participant, ctx = _app_ctx(request, "home")
    if participant is None:
        return _redirect_login(request)
    plan = services.roster_plan(participant)
    fstate = services.formation_state(participant)
    ctx.update({
        "plan": plan,
        "roster_count": plan["owned"],
        "slots_total": plan["total_slots"],
        "watch_count": participant.watches.count(),
        "standings": _app_standings(participant.league, participant.id)[:5],
        "lineup_done": fstate["starters_count"],
        "lineup_target": fstate["starters_target"],
        "lineup_incomplete": plan["owned"] > 0 and fstate["starters_count"] < fstate["starters_target"],
    })
    league = participant.league
    open_market = None
    if league is not None:
        services.sync_market_schedule(league)
        open_market = MarketSession.objects.filter(league=league, status=MarketSession.Status.OPEN).first()
    ctx.update({
        "open_market": open_market if open_market and open_market.is_open else None,
        "my_open_bids": MarketBid.objects.filter(session=open_market, participant=participant).count() if open_market else 0,
        "incoming_trades": Trade.objects.filter(receiver=participant, status=Trade.Status.PENDING).count(),
    })
    return render(request, "auctions/app_home.html", ctx)


def app_rosa(request):
    participant, ctx = _app_ctx(request, "rosa")
    if participant is None:
        return _redirect_login(request)
    roster = list(Player.objects.filter(owner=participant).order_by("role", "-cost", "name"))
    groups = [
        {"code": code, "label": label, "list": [p for p in roster if p.role == code]}
        for code, label in _ROLE_LABELS
    ]
    stats_covered = sum(1 for p in roster if p.fanta_avg is not None or p.avg_vote is not None)
    ctx.update({
        "roster": roster,
        "roster_count": len(roster),
        "roster_groups": [g for g in groups if g["list"]],
        "stats_covered": stats_covered,
        "is_mantra": participant.league.is_mantra if participant.league else False,
    })
    return render(request, "auctions/app_rosa.html", ctx)


def app_live(request):
    participant, ctx = _app_ctx(request, "live")
    if participant is None:
        return _redirect_login(request)
    league = participant.league

    season = Season.objects.filter(league=league, is_current=True).first() if league else None
    current_giornata = None
    my_score = None
    lineup_performances = []

    if season:
        current_giornata = (
            season.giornate.filter(status__in=[Giornata.Status.OPEN, Giornata.Status.LOCKED]).order_by("number").first()
            or season.giornate.filter(status=Giornata.Status.SCORED).order_by("-number").first()
        )
        if current_giornata:
            my_score = GiornataScore.objects.filter(giornata=current_giornata, participant=participant).first()
            formation = Formation.objects.filter(participant=participant).first()
            if formation and formation.starter_ids:
                starter_pids = [i for i in formation.starter_ids if i]
                starters = {p.id: p for p in Player.objects.filter(id__in=starter_pids)}
                perf_map = {
                    p.player_id: p
                    for p in PlayerPerformance.objects.filter(giornata=current_giornata, player_id__in=starter_pids)
                }
                for pid in starter_pids:
                    pl = starters.get(pid)
                    if not pl:
                        continue
                    perf = perf_map.get(pid)
                    lineup_performances.append({
                        "player": pl,
                        "perf": perf,
                        "has_vote": perf.vote is not None if perf else False,
                        "vote": perf.vote if perf else None,
                        "goals": perf.goals if perf else 0,
                        "assists": perf.assists if perf else 0,
                        "yellow": perf.yellow if perf else False,
                        "red": perf.red if perf else False,
                    })

    ctx.update({
        "season": season,
        "giornata": current_giornata,
        "my_score": my_score,
        "lineup_performances": lineup_performances,
    })
    return render(request, "auctions/app_live.html", ctx)


def app_lega(request):
    participant, ctx = _app_ctx(request, "lega")
    if participant is None:
        return _redirect_login(request)
    league = participant.league
    auctions = Auction.objects.all()
    auctions = auctions.filter(league=league) if league is not None else auctions.filter(league__isnull=True)
    auctions = [a for a in auctions.order_by("-id") if a.status != Auction.Status.DRAFT]
    ctx.update({
        "standings": _app_standings(league, participant.id),
        "auctions": auctions,
    })
    return render(request, "auctions/app_lega.html", ctx)


def app_mercato(request):
    participant, ctx = _app_ctx(request, "mercato")
    if participant is None:
        return _redirect_login(request)
    league = participant.league
    q = (request.GET.get("q") or "").strip()
    role = (request.GET.get("role") or "").strip().upper()[:1]
    sort = (request.GET.get("sort") or "-quota").strip()

    in_budget = request.GET.get("budget") == "1"
    is_mantra = bool(league and league.is_mantra)

    free_agents = Player.objects.filter(owner__isnull=True)
    free_agents = free_agents.filter(league=league) if league else free_agents.filter(league__isnull=True)
    # The quotation the league actually plays with (Mantra price when present).
    quota = Coalesce("price_m", "initial_price") if is_mantra else F("initial_price")
    free_agents = free_agents.annotate(quota=quota)
    if role in ("P", "D", "C", "A"):
        free_agents = free_agents.filter(role=role)
    if q:
        free_agents = free_agents.filter(Q(name__icontains=q) | Q(team__icontains=q))
    if in_budget:
        free_agents = free_agents.filter(quota__lte=participant.remaining_credits)

    orders = {
        "-quota": ["-quota", "name"],
        "quota": ["quota", "name"],
        "name": ["name"],
        "-fm": [F("fanta_avg").desc(nulls_last=True), "-quota"],
    }
    if sort not in orders:
        sort = "-quota"
    page = Paginator(free_agents.order_by(*orders[sort]), 30).get_page(request.GET.get("page"))
    watched = set(participant.watches.values_list("player_id", flat=True))
    for pl in page.object_list:
        pl.watched = pl.id in watched

    base_query = request.GET.copy()
    base_query.pop("page", None)

    my_roster = list(Player.objects.filter(owner=participant).order_by("role", "-cost", "name"))
    active_auc = ctx.get("active_auction")
    refund_mode = active_auc.release_refund_mode if active_auc else "purchase"

    # Market session (buste di riparazione): the open one, else the latest
    # closed/resolved one so the manager can read how their envelopes went.
    services.sync_market_schedule(league)
    sessions = MarketSession.objects.filter(league=league).exclude(status=MarketSession.Status.DRAFT)
    market_session = (
        sessions.filter(status=MarketSession.Status.OPEN).first()
        or sessions.order_by("-updated_at").first()
    )
    market_open = bool(market_session and market_session.is_open)
    my_bids = []
    if market_session:
        my_bids = services.get_participant_market_bids(market_session.id, participant.id)
        for rp in my_roster:
            rp.market_refund = int(services.market_release_refund(market_session, rp))
    my_bid_player_ids = {b["player_id"] for b in my_bids}
    my_bids_total = sum(b["amount"] for b in my_bids)

    ctx.update({
        "free_agents": page.object_list,
        "page": page,
        "base_query": base_query.urlencode(),
        "in_budget": in_budget,
        "role_filters": [("", "Tutti"), ("P", "Portieri"), ("D", "Difensori"), ("C", "Centrocampisti"), ("A", "Attaccanti")],
        "plan": services.roster_plan(participant),
        "my_roster": my_roster,
        "market_session": market_session,
        "market_open": market_open,
        "my_bids": my_bids,
        "my_bid_player_ids": my_bid_player_ids,
        "my_bids_total": my_bids_total,
        "trades_enabled": bool(league and league.trades_enabled),
        "incoming_trades": Trade.objects.filter(receiver=participant, status=Trade.Status.PENDING).count(),
        "role": role,
        "q": q,
        "sort": sort,
        "refund_mode": refund_mode,
        "is_mantra": is_mantra,
    })
    return render(request, "auctions/app_mercato.html", ctx)


@require_POST
def app_market_bid(request):
    participant, _ = _app_ctx(request, "mercato")
    if participant is None:
        return JsonResponse({"ok": False, "error": "not_authenticated"}, status=401)

    if request.content_type == "application/json":
        try:
            body = json.loads(request.body.decode("utf-8"))
        except Exception:
            body = {}
    else:
        body = request.POST

    session_id = body.get("session_id")
    player_id = body.get("player_id")
    amount = body.get("amount")
    priority = body.get("priority", 1)
    release_player_id = body.get("release_player_id") or None

    res = services.place_market_bid(
        session_id=session_id,
        participant_id=participant.id,
        player_id=player_id,
        amount=amount,
        priority=priority,
        release_player_id=release_player_id,
    )
    if res.get("ok") and session_id:
        res["my_bids"] = services.get_participant_market_bids(session_id, participant.id)
    return JsonResponse(res)


@require_POST
def app_market_delete_bid(request):
    participant, _ = _app_ctx(request, "mercato")
    if participant is None:
        return JsonResponse({"ok": False, "error": "not_authenticated"}, status=401)

    if request.content_type == "application/json":
        try:
            body = json.loads(request.body.decode("utf-8"))
        except Exception:
            body = {}
    else:
        body = request.POST

    session_id = body.get("session_id")
    bid_id = body.get("bid_id")

    res = services.delete_market_bid(
        session_id=session_id,
        participant_id=participant.id,
        bid_id=bid_id,
    )
    if res.get("ok") and session_id:
        res["my_bids"] = services.get_participant_market_bids(session_id, participant.id)
    return JsonResponse(res)


_PDCA = Case(
    When(role="P", then=Value(0)), When(role="D", then=Value(1)),
    When(role="C", then=Value(2)), default=Value(3),
)


def _roster_pdca(participant):
    return list(Player.objects.filter(owner=participant).order_by(_PDCA, "name"))


def _trade_rows(trades, me):
    rows = []
    for t in trades:
        mine_out = t.proposer_id == me.id
        rows.append({
            "trade": t,
            "other": t.receiver if mine_out else t.proposer,
            "give": list(t.proposer_players.all() if mine_out else t.receiver_players.all()),
            "get": list(t.receiver_players.all() if mine_out else t.proposer_players.all()),
            "give_credits": t.proposer_credits if mine_out else t.receiver_credits,
            "get_credits": t.receiver_credits if mine_out else t.proposer_credits,
            "outgoing": mine_out,
        })
    return rows


def app_scambi(request):
    participant, ctx = _app_ctx(request, "mercato")
    if participant is None:
        return _redirect_login(request)
    league = participant.league
    mine = Trade.objects.filter(Q(proposer=participant) | Q(receiver=participant)).select_related(
        "proposer", "receiver"
    ).prefetch_related("proposer_players", "receiver_players")

    teams = []
    partner = None
    if league is not None:
        teams = list(league.participants.filter(is_active=True).exclude(pk=participant.pk).order_by("display_name"))
        raw = request.GET.get("with") or ""
        if raw.isdigit():
            partner = next((t for t in teams if t.id == int(raw)), None)

    ctx.update({
        "trades_enabled": bool(league and league.trades_enabled),
        "trades_need_approval": bool(league and league.trades_need_approval),
        "incoming": _trade_rows(mine.filter(receiver=participant, status=Trade.Status.PENDING), participant),
        "outgoing": _trade_rows(mine.filter(proposer=participant, status__in=Trade.OPEN_STATUSES), participant),
        "awaiting": _trade_rows(mine.filter(receiver=participant, status=Trade.Status.ACCEPTED), participant),
        "history": _trade_rows(mine.exclude(status__in=Trade.OPEN_STATUSES)[:20], participant),
        "teams": teams,
        "partner": partner,
        "my_roster": _roster_pdca(participant),
        "partner_roster": _roster_pdca(partner) if partner else [],
    })
    return render(request, "auctions/app_scambi.html", ctx)


def _trade_feedback(request, res, ok_message):
    if res.get("ok"):
        messages.success(request, ok_message)
    else:
        messages.error(request, res.get("message") or "Operazione non riuscita.")
    return redirect("app_scambi")


@require_POST
def app_trade_propose(request):
    participant, _ = _app_ctx(request, "mercato")
    if participant is None:
        return _redirect_login(request)
    res = services.propose_trade(
        participant.id,
        request.POST.get("receiver_id"),
        give_ids=request.POST.getlist("give"),
        get_ids=request.POST.getlist("get"),
        give_credits=request.POST.get("give_credits") or 0,
        get_credits=request.POST.get("get_credits") or 0,
        message=request.POST.get("message") or "",
    )
    return _trade_feedback(request, res, "Proposta di scambio inviata.")


@require_POST
def app_trade_respond(request, trade_id):
    participant, _ = _app_ctx(request, "mercato")
    if participant is None:
        return _redirect_login(request)
    accept = request.POST.get("action") == "accept"
    res = services.respond_trade(trade_id, participant.id, accept)
    if not accept:
        msg = "Scambio rifiutato."
    elif res.get("status") == Trade.Status.ACCEPTED:
        msg = "Scambio accettato: ora serve la ratifica dell'admin."
    else:
        msg = "Scambio completato: le rose sono aggiornate."
    return _trade_feedback(request, res, msg)


@require_POST
def app_trade_cancel(request, trade_id):
    participant, _ = _app_ctx(request, "mercato")
    if participant is None:
        return _redirect_login(request)
    return _trade_feedback(request, services.cancel_trade(trade_id, participant.id), "Proposta ritirata.")


def app_altro(request):
    participant, ctx = _app_ctx(request, "altro")
    if participant is None:
        return _redirect_login(request)
    return render(request, "auctions/app_altro.html", ctx)


@require_POST
def app_update_pin(request):
    """Allow manager to update their team's access_code (PIN)."""
    participant, _ = _app_ctx(request, "altro")
    if participant is None:
        return _redirect_login(request)

    new_code = (request.POST.get("access_code") or "").strip().upper()[:20]
    if len(new_code) < 3:
        messages.error(request, "Il codice deve contenere almeno 3 caratteri.")
        return redirect("app_altro")

    # Ensure uniqueness within the same league
    already_used = Participant.objects.filter(
        league=participant.league, access_code__iexact=new_code
    ).exclude(pk=participant.id).exists()
    if already_used:
        messages.error(request, "Questo codice è già utilizzato da un'altra squadra della lega.")
        return redirect("app_altro")

    participant.access_code = new_code
    participant.save(update_fields=["access_code"])
    messages.success(request, f"Codice di accesso aggiornato: {new_code}")
    return redirect("app_altro")


def app_formazione(request):
    """Lineup builder (inside Rosa): pick a module, assign starters per role,
    save. Reuses the roster; no Giornata yet, so it's a single current lineup."""
    participant = _session_participant(request)
    if participant is None:
        return _redirect_login(request)
    if request.method == "POST":
        module = request.POST.get("module", "")
        services.save_formation(participant, module, request.POST.getlist("starter"))
        return redirect("app_formazione")
    _, ctx = _app_ctx(request, "rosa")
    ctx.update(services.formation_state(participant))
    return render(request, "auctions/app_formazione.html", ctx)


def app_login(request):
    """Dedicated login for the managerial area (FantaManager).

    Supports:
    1. Direct tokenized access via ?t=<token>
    2. Account login (Username / Email + Password)
    3. Quick access code entry (e.g. 'DRAGO23')
    4. Guided team selection (League -> Team) with optional access code check
    """
    next_url = request.GET.get("next") or request.POST.get("next") or reverse("app_home")

    # 1. Check tokenized link in GET (?t=...)
    token = (request.GET.get("t") or "").strip()
    if token:
        participant = Participant.objects.filter(public_token=token, is_active=True).first()
        if participant:
            request.session["participant_id"] = participant.id
            request.session["display_name"] = participant.display_name
            if participant.league_id is not None:
                request.session[SESSION_LEAGUE_KEY] = participant.league_id
            return redirect(next_url)

    error = None
    active_mode = request.GET.get("mode") or "account"

    # If the user is already authenticated via Django user and has a linked team,
    # and they are not explicitly asking to switch (?switch=1):
    if request.method == "GET" and request.user.is_authenticated and not request.GET.get("switch"):
        participant = Participant.objects.filter(user=request.user, is_active=True).first()
        if participant:
            request.session["participant_id"] = participant.id
            request.session["display_name"] = participant.display_name
            if participant.league_id is not None:
                request.session[SESSION_LEAGUE_KEY] = participant.league_id
            return redirect(next_url)

    if request.method == "POST":
        login_mode = request.POST.get("login_mode", "")
        identifier = (request.POST.get("identifier") or request.POST.get("username") or "").strip()
        password = (request.POST.get("password") or "").strip()
        access_code = (request.POST.get("access_code") or "").strip()
        participant_id = (request.POST.get("participant_id") or "").strip()

        # Deduce mode if not explicitly tagged
        if not login_mode:
            if identifier or password:
                login_mode = "account"
            elif participant_id:
                login_mode = "select"
            elif access_code:
                login_mode = "code"

        active_mode = login_mode or "account"
        participant = None

        if login_mode == "account":
            if not identifier or not password:
                error = "Inserisci nome utente / email e password."
            else:
                username = identifier
                if "@" in identifier:
                    user_obj = User.objects.filter(email__iexact=identifier).first()
                    if user_obj:
                        username = user_obj.username
                user = authenticate(request, username=username, password=password)
                if user is None:
                    error = "Credenziali non valide. Verifica username/email e password."
                elif not user.is_active:
                    error = "Questo account è disattivato. Contatta l'amministratore."
                else:
                    auth_login(request, user)
                    teams = Participant.objects.filter(user=user, is_active=True)
                    if teams.exists():
                        participant = teams.first()
                    else:
                        # User has no linked team: check if superuser or league owner
                        owned = League.objects.filter(owner=user).first()
                        if user.is_superuser or owned:
                            target_l = owned or League.objects.first()
                            p_cand = Participant.objects.filter(league=target_l, is_active=True).first() or Participant.objects.filter(is_active=True).first()
                            if p_cand:
                                participant = p_cand
                                messages.info(request, f"Accesso come amministratore. Visualizzazione con la squadra {participant.display_name}.")
                        if not participant:
                            error = "Nessuna squadra associata a questo account. Usa la scheda 'Codice Squadra' per collegare la tua rosa."

        elif login_mode == "code" or (access_code and not participant_id):
            if not access_code:
                error = "Inserisci il codice della tua squadra."
            else:
                participant = Participant.objects.filter(
                    Q(access_code__iexact=access_code) | Q(public_token=access_code),
                    is_active=True,
                ).first()
                if not participant:
                    error = "Codice squadra non valido o non riconosciuto."
                elif participant.user_id is not None and request.user.is_authenticated and request.user.id != participant.user_id:
                    error = "Questa squadra è già associata a un altro account utente."
                elif request.user.is_authenticated and participant.user is None:
                    # Link unassigned team to the currently logged-in user
                    participant.user = request.user
                    participant.save(update_fields=["user"])

        elif login_mode == "select" or participant_id:
            if participant_id and participant_id.isdigit():
                candidate = Participant.objects.filter(pk=int(participant_id), is_active=True).first()
                if candidate:
                    if candidate.user_id is not None and (not request.user.is_authenticated or request.user.id != candidate.user_id):
                        error = f"{candidate.display_name} è associata all'account di un utente. Accedi con Username e Password."
                    elif candidate.access_code and candidate.access_code.lower() != access_code.lower():
                        error = f"Codice di accesso errato per {candidate.display_name}."
                    elif not candidate.access_code and not request.user.is_authenticated:
                        error = f"Per gestire {candidate.display_name} accedi al tuo account o inserisci il codice squadra."
                    else:
                        participant = candidate
                        if request.user.is_authenticated and participant.user is None:
                            participant.user = request.user
                            participant.save(update_fields=["user"])
                else:
                    error = "Squadra non trovata."
            else:
                error = "Seleziona la tua squadra dall'elenco."
        else:
            error = "Inserisci le credenziali di accesso, il codice squadra o selezionala dall'elenco."

        if participant and not error:
            request.session["participant_id"] = participant.id
            request.session["display_name"] = participant.display_name
            if participant.league_id is not None:
                request.session[SESSION_LEAGUE_KEY] = participant.league_id
            return redirect(next_url)

    leagues = League.objects.all().prefetch_related("participants").order_by("name")
    user_teams = []
    if request.user.is_authenticated:
        user_teams = list(Participant.objects.filter(user=request.user, is_active=True))

    return render(
        request,
        "auctions/app_login.html",
        {
            "leagues": leagues,
            "next": next_url,
            "error": error,
            "active_mode": active_mode,
            "user_teams": user_teams,
        },
    )


def app_logout(request):
    """Log out of the managerial area."""
    request.session.pop("participant_id", None)
    request.session.pop("display_name", None)
    request.session.pop(SESSION_LEAGUE_KEY, None)
    if request.user.is_authenticated:
        auth_logout(request)
    return redirect("app_login")
