"""Product shell (mobile-first FantaManager app; session-participant identity)."""
import json
from django.core.paginator import Paginator
from django.db.models import Case, F, Q, Value, When
from django.db.models.functions import Coalesce
from django.contrib import messages
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import (
    Auction,
    Competition,
    Fixture,
    Giornata,
    GiornataScore,
    MarketBid,
    MarketSession,
    Participant,
    Player,
    Season,
    Trade,
)
from .. import scoring, services, throttle
from ..models.participant import AMBIGUOUS_CODE_MESSAGE, custom_code_error, find_team_by_code
from ..services import mail, sala
from ..services.market import buyout_price, fa_period_start, session_moves, waiver_order
from .admin_market import rule_choices, session_labels
from .auth import authenticate_identifier
from .common import (
    SESSION_LEAGUE_KEY,
    _ROLE_LABELS,
    app_admin_leagues,
    _app_ctx,
    _app_standings,
    _session_participant,
    safe_next,
    user_can_manage_league,
    user_can_manage_scope,
    visible_leagues,
)


def _cap_ctx(participant):
    """Tetto salariale della squadra per le pagine dell'app (None se non attivo)."""
    from ..services import salary

    status = salary.cap_status(participant)
    if status is None:
        return None
    r = salary.rules(participant.league)
    status["extra_open"] = not salary.extra_cap_locked(participant.league)
    status["extra_cost"] = r["extra_cap_cost"]
    status["extra_gain"] = r["extra_cap_gain"]
    return status


@require_POST
def app_extra_cap(request):
    participant, ctx = _app_ctx(request, "mercato")
    if participant is None:
        return _redirect_login(request, ctx)
    from ..services import salary

    res = salary.convert_budget(participant.id, request.POST.get("blocks"))
    if res.get("ok"):
        messages.success(request, f"Convertiti {res['cost']} FM di budget in {res['gain']} FM di tetto salariale.")
    else:
        messages.error(request, res.get("message"))
    return redirect("app_mercato")


def _redirect_login(request, ctx=None):
    """Nobody's team on a team page. A league admin without a team of their own
    goes to the Regia — the console's dashboard inside the app — where "Vedi
    come" opens any team; everyone else goes to the login."""
    if ctx is not None and ctx.get("is_app_admin"):
        messages.info(request, "Questa pagina è di una squadra: in Regia scegli «Vedi come» su quella che vuoi aprire.")
        return redirect("app_regia")
    return redirect(f"{reverse('app_login')}?next={request.path}")


def app_home(request):
    participant, ctx = _app_ctx(request, "home")
    if participant is None:
        if ctx is not None and ctx.get("is_app_admin"):
            return redirect("app_regia")
        return _redirect_login(request, ctx)
    plan = services.roster_plan(participant)
    next_giornata = services.target_giornata(participant.league)
    fstate = services.formation_state(participant, giornata=next_giornata)
    ctx.update({
        "plan": plan,
        "next_giornata": next_giornata,
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
        "cap": _cap_ctx(participant),
        # Asta in sala sul PC: da qui si entra (se il PC ha aperto l'accesso
        # da internet), altrimenti la home dice cosa aspettare.
        "sala_entry": bool(sala.live_entry_url(participant)),
        "sala_locked": sala.is_locked(league),
    })
    if ctx.get("manages_app_league"):
        # The admin's own team lives in a league they run: the home also says
        # what the league needs from them, with a door to the Regia.
        from .app_admin import league_admin_digest

        ctx["admin_todo"] = [t for t in league_admin_digest(league) if t["level"] != "ok"]
    return render(request, "auctions/app_home.html", ctx)


def app_sala_enter(request):
    """Entra nell'asta che si gioca sul PC in sala: il PC ha dato al sito il suo
    indirizzo e il codice di questa squadra, si arriva già riconosciuti."""
    participant, ctx = _app_ctx(request, "home")
    if participant is None:
        return _redirect_login(request, ctx)
    url = sala.live_entry_url(participant)
    if not url:
        messages.info(request, "L'asta in sala non è raggiungibile da internet in questo momento: "
                               "riprova tra poco o chiedi a chi la conduce di attivare l'accesso da internet.")
        return redirect("app_home")
    return redirect(url)


def app_rosa(request):
    participant, ctx = _app_ctx(request, "rosa")
    if participant is None:
        return _redirect_login(request, ctx)
    league = participant.league

    target_id = request.GET.get("team")
    viewed_participant = participant
    if target_id and league:
        found = league.participants.filter(id=target_id).first()
        if found:
            viewed_participant = found

    is_mine = (viewed_participant.id == participant.id)
    roster = list(Player.objects.filter(owner=viewed_participant, abroad_list=False).select_related("loan_from")
                  .order_by("role", "-cost", "name"))
    is_mantra = bool(league and league.is_mantra)
    groups = []
    for code, label in _ROLE_LABELS:
        players = [p for p in roster if p.role == code]
        # Mantra counts slots per bucket (POR / movimento), not per role: the
        # per-role cap would be misleading there, so only the count is shown.
        slots = league.slots_for(code) if league is not None and not is_mantra else 0
        groups.append({
            "code": code, "label": label, "list": players,
            "slots": slots, "cost": sum(p.cost for p in players),
        })
    fms = [p.fanta_avg for p in roster if p.fanta_avg is not None]
    league_teams = list(league.participants.order_by("display_name")) if league else []
    ctx.update({
        "viewed_participant": viewed_participant,
        "is_mine": is_mine,
        "league_teams": league_teams,
        "roster": roster,
        "roster_count": len(roster),
        "roster_groups": groups,
        "roster_value": sum(p.cost for p in roster),
        "roster_fm": (sum(fms) / len(fms)) if fms else None,
        "plan": services.roster_plan(viewed_participant),
        "cap": _cap_ctx(viewed_participant),
        "is_mantra": is_mantra,
        "contracts_on": bool(league and league.contracts_enabled),
        "renewals_open": bool(league and league.contracts_enabled and league.renewals_open),
        "expiring": services.expiring_contracts(viewed_participant) if league and league.contracts_enabled else [],
        "abroad_listed": list(Player.objects.filter(owner=viewed_participant, abroad_list=True)),
        "loaned_out": list(Player.objects.filter(loan_from=viewed_participant).select_related("owner")),
    })
    return render(request, "auctions/app_rosa.html", ctx)


def app_live(request):
    participant, ctx = _app_ctx(request, "live")
    if participant is None:
        return _redirect_login(request, ctx)
    league = participant.league

    season = Season.objects.filter(league=league, is_current=True).first() if league else None
    current_giornata = None
    all_giornate = []
    my_score = None
    lineup_performances = []
    my_live = None
    opp_live = None
    match_fixture = None
    opponent = None
    leaderboard = []
    manual = {}

    if season:
        all_giornate = list(season.giornate.all().order_by("number"))
        selected_g_num = request.GET.get("giornata")
        if selected_g_num and selected_g_num.isdigit():
            current_giornata = next((g for g in all_giornate if g.number == int(selected_g_num)), None)

        if not current_giornata:
            current_giornata = (
                season.giornate.filter(status__in=[Giornata.Status.LIVE, Giornata.Status.LOCKED, Giornata.Status.OPEN]).order_by("number").first()
                or season.giornate.filter(status=Giornata.Status.SCORED).order_by("-number").first()
                or (all_giornate[0] if all_giornate else None)
            )

        if current_giornata:
            my_score = GiornataScore.objects.filter(giornata=current_giornata, participant=participant).first()
            perf_map = services.giornata_perf_map(current_giornata)
            rules = scoring.effective_rules(season.rules if season else None)
            # Totals typed in by the admin (another site gives no votes): they win
            # over the engine, which has nothing to count.
            manual = {gs.participant_id: gs for gs in GiornataScore.objects.filter(giornata=current_giornata)
                      if (gs.breakdown or {}).get("manual")}

            def _get_team_live(part):
                starters, bench = services.lineup_io(part, giornata=current_giornata)
                captain_id, vice_id = services.lineup_captains(part, current_giornata)
                res = scoring.score_lineup(starters, bench, perf_map, rules,
                                           captain_id=captain_id, vice_id=vice_id)

                player_ids = set()
                for l in res["lines"]:
                    player_ids.add(l["id"])
                    if l.get("sub_in"):
                        player_ids.add(l["sub_in"])
                for b in bench:
                    player_ids.add(b["id"])

                players_dict = {p.id: p for p in Player.objects.filter(id__in=player_ids)}

                detailed_starters = []
                for l in res["lines"]:
                    starter_p = players_dict.get(l["id"])
                    sub_p = players_dict.get(l.get("sub_in")) if l.get("sub_in") else None
                    active_p = sub_p if sub_p else starter_p
                    active_perf = perf_map.get(active_p.id if active_p else l["id"], {})

                    detailed_starters.append({
                        "starter_player": starter_p,
                        "sub_player": sub_p,
                        "player": active_p,  # for generic template access
                        "role": l["role"],
                        "vote": l["vote"],
                        "fantavoto": l["fantavoto"],
                        "has_vote": l["has_vote"],
                        "is_subbed": bool(sub_p),
                        "is_captain": res["captain"]["id"] is not None and res["captain"]["id"] == l["id"] and not sub_p,
                        "perf": active_perf,
                        "goals": active_perf.get("goals", 0),
                        "assists": active_perf.get("assists", 0),
                        "yellow": active_perf.get("yellow", False),
                        "red": active_perf.get("red", False),
                    })

                subbed_in_pids = {l["sub_in"] for l in res["lines"] if l.get("sub_in")}
                detailed_bench = []
                for b in bench:
                    bp = players_dict.get(b["id"])
                    if not bp:
                        continue
                    b_perf = perf_map.get(b["id"], {})
                    b_fv, b_has = scoring.player_fantavoto(b_perf, b["role"], rules)
                    detailed_bench.append({
                        "player": bp,
                        "role": b["role"],
                        "vote": b_perf.get("vote"),
                        "fantavoto": b_fv,
                        "has_vote": b_has,
                        "subbed_in": b["id"] in subbed_in_pids,
                        "perf": b_perf,
                        "goals": b_perf.get("goals", 0),
                        "assists": b_perf.get("assists", 0),
                        "yellow": b_perf.get("yellow", False),
                        "red": b_perf.get("red", False),
                    })

                typed = manual.get(part.id)
                return {
                    "participant": part,
                    "total": typed.total if typed else res["total"],
                    "goals": typed.goals if typed else res["goals"],
                    "modificatore": res["modificatore"],
                    "captain_bonus": res["captain"]["bonus"],
                    "subs": res["subs"],
                    "starters": detailed_starters,
                    "bench": detailed_bench,
                }

            # Head-to-head match fixture
            fixture_id_param = request.GET.get("fixture")
            match_fixture = None
            if fixture_id_param and fixture_id_param.isdigit():
                match_fixture = current_giornata.fixtures.filter(
                    id=int(fixture_id_param)
                ).select_related("home", "away", "competition").first()

            if not match_fixture:
                match_fixture = current_giornata.fixtures.filter(
                    Q(home=participant) | Q(away=participant)
                ).select_related("home", "away", "competition").first()

            if match_fixture:
                if participant.id == match_fixture.away_id:
                    team_me = match_fixture.away
                    team_opp = match_fixture.home
                elif participant.id == match_fixture.home_id:
                    team_me = match_fixture.home
                    team_opp = match_fixture.away
                else:
                    team_me = match_fixture.home
                    team_opp = match_fixture.away

                my_live = _get_team_live(team_me)
                lineup_performances = my_live["starters"]
                opponent = team_opp
                if opponent:
                    opp_live = _get_team_live(opponent)
            else:
                team_me = participant
                my_live = _get_team_live(participant)
                lineup_performances = my_live["starters"]

            # Matchday leaderboard
            active_teams = list(participant.league.participants.filter(is_active=True)) if participant.league else [participant]
            for t in active_teams:
                s, b = services.lineup_io(t, giornata=current_giornata)
                c, v = services.lineup_captains(t, current_giornata)
                r = scoring.score_lineup(s, b, perf_map, rules, captain_id=c, vice_id=v)
                typed = manual.get(t.id)
                leaderboard.append({
                    "participant": t,
                    "total": typed.total if typed else r["total"],
                    "goals": typed.goals if typed else r["goals"],
                    "is_me": t.id == participant.id,
                })
            leaderboard.sort(key=lambda x: x["total"], reverse=True)

    ctx.update({
        "season": season,
        "giornate": all_giornate,
        "giornata": current_giornata,
        "my_score": my_score,
        "my_live": my_live,
        "opp_live": opp_live,
        "fixture": match_fixture,
        "opponent": opponent,
        "active_team": team_me if match_fixture else participant,
        "leaderboard": leaderboard,
        "lineup_performances": lineup_performances,
        "manual_scores": bool(current_giornata and manual),
    })
    return render(request, "auctions/app_live.html", ctx)



def app_lega(request):
    participant, ctx = _app_ctx(request, "lega")
    if ctx is None:
        return _redirect_login(request, ctx)
    # A league admin without a team still reads the league they run.
    league = ctx["app_league"]
    auctions = Auction.objects.all()
    auctions = auctions.filter(league=league) if league is not None else auctions.filter(league__isnull=True)
    auctions = [a for a in auctions.order_by("-id") if a.status != Auction.Status.DRAFT]

    from ..services.competitions import (
        ensure_league_season_and_competitions,
        compute_competition_standings,
        get_competition_matchdays,
    )
    season, competitions = ensure_league_season_and_competitions(league)
    current_competition = None
    competition_data = None
    competition_matchdays = []

    if season and competitions:
        comp_id = request.GET.get("comp")
        if comp_id:
            current_competition = next((c for c in competitions if str(c.id) == comp_id), competitions[0] if competitions else None)
        else:
            current_competition = competitions[0] if competitions else None

        if current_competition:
            competition_data = compute_competition_standings(current_competition)
            competition_matchdays = get_competition_matchdays(
                current_competition,
                participant_id=participant.id if participant else None,
            )

    active_tab = request.GET.get("tab", "classifica")
    target_giornata = request.GET.get("giornata", "")

    teams = list(league.participants.filter(is_active=True).order_by("display_name")) if league else []
    giornate = list(season.giornate.all().order_by("number")) if season else []

    ctx.update({
        "standings": _app_standings(league, participant.id if participant else None),
        "auctions": auctions,
        "season": season,
        "competitions": competitions,
        "current_competition": current_competition,
        "goal_rules": scoring.effective_rules(season.rules if season else None),
        "competition_data": competition_data,
        "competition_matchdays": competition_matchdays,
        "active_tab": active_tab,
        "target_giornata": target_giornata,
        "teams": teams,
        "giornate": giornate,
        "competition_types": Competition.Type.choices,
    })
    return render(request, "auctions/app_lega.html", ctx)


def app_mercato(request):
    participant, ctx = _app_ctx(request, "mercato")
    if participant is None:
        return _redirect_login(request, ctx)
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

    # Market sessions
    # Only this league's sessions: a market opened elsewhere isn't this team's.
    if league is not None:
        services.sync_market_schedule(league)
    sessions_qs = MarketSession.objects.filter(league=league).exclude(status=MarketSession.Status.DRAFT) if league else MarketSession.objects.none()
    
    # Session selection
    requested_session_id = request.GET.get("session_id")
    market_session = None
    if requested_session_id:
        try:
            market_session = sessions_qs.filter(id=int(requested_session_id)).first()
        except (ValueError, TypeError):
            market_session = None
    if not market_session:
        market_session = (
            sessions_qs.filter(status=MarketSession.Status.OPEN).first()
            or sessions_qs.order_by("-updated_at").first()
        )

    # Build detailed sessions list for the hub
    sessions_list = []
    status_order = {MarketSession.Status.OPEN: 0, MarketSession.Status.CLOSED: 1, MarketSession.Status.RESOLVED: 2}
    for s in sessions_qs:
        s_bids = services.get_participant_market_bids(s.id, participant.id)
        s_bids_count = len(s_bids)
        s_bids_total = sum(b["amount"] for b in s_bids)
        s_won_count = sum(1 for b in s_bids if b["status"] == MarketBid.Status.WON)
        sessions_list.append({
            "session": s,
            "id": s.id,
            "title": s.title,
            "status": s.status,
            "status_display": s.get_status_display(),
            "mk_labels": session_labels(s),
            "is_open": s.is_open,
            "closes_at": s.closes_at,
            "opens_at": s.opens_at,
            "bids_count": s_bids_count,
            "bids_total": s_bids_total,
            "won_count": s_won_count,
            "max_bids": s.max_bids,
            "budget_rule": s.budget_rule,
            "tie_break": s.tie_break,
            "allow_conditional_release": s.allow_conditional_release,
            "require_same_role_release": s.require_same_role_release,
            "is_selected": bool(market_session and s.id == market_session.id),
            "session_type": getattr(s, "session_type", MarketSession.SessionType.SEALED_BIDS),
            "session_type_display": s.get_session_type_display() if hasattr(s, "get_session_type_display") else "Buste Segrete",
            "config": s.config or {},
            "max_acquisitions_p": s.max_acquisitions_p,
            "max_acquisitions_d": s.max_acquisitions_d,
            "max_acquisitions_c": s.max_acquisitions_c,
            "max_acquisitions_a": s.max_acquisitions_a,
        })
    sessions_list.sort(key=lambda item: (status_order.get(item["status"], 3), -(item["closes_at"].timestamp() if item["closes_at"] else 0)))
    active_sessions = [s for s in sessions_list if s["is_open"]]
    past_sessions = [s for s in sessions_list if not s["is_open"]]

    market_open = bool(market_session and market_session.is_open)
    my_bids = []
    if market_session:
        my_bids = services.get_participant_market_bids(market_session.id, participant.id)
        for rp in my_roster:
            rp.market_refund = int(services.market_release_refund(market_session, rp))
    my_bid_player_ids = {b["player_id"]: b for b in my_bids}
    my_bids_total = sum(b["amount"] for b in my_bids)
    bids_left = None
    if market_session and market_session.max_bids:
        bids_left = max(0, market_session.max_bids - len(my_bids))

    active_auc = ctx.get("active_auction")
    active_markets_count = len(active_sessions) + (1 if (league and league.trades_enabled) else 0) + (1 if active_auc else 0)

    # Mode-specific data for Free Agency, Buyout Clause, and Waiver Wire
    moves_this_week = 0
    moves_left = None
    opponents_players = []
    waiver_claims = []
    waiver_order_preview = []
    
    session_type_val = getattr(market_session, "session_type", MarketSession.SessionType.SEALED_BIDS)
    is_free_agency = bool(market_session and session_type_val == MarketSession.SessionType.FREE_AGENCY)
    is_buyout_clause = bool(market_session and session_type_val == MarketSession.SessionType.BUYOUT_CLAUSE)
    is_waiver_wire = bool(market_session and session_type_val == MarketSession.SessionType.WAIVER_WIRE)
    is_live_auction = bool(market_session and session_type_val == MarketSession.SessionType.LIVE_AUCTION)
    is_renewals = bool(market_session and session_type_val == MarketSession.SessionType.RENEWALS)
    is_buste = bool(market_session and session_type_val in (MarketSession.SessionType.SEALED_BIDS, MarketSession.SessionType.REPAIR))

    if market_session and is_free_agency:
        moves_this_week = session_moves(market_session).filter(
            participant=participant, created_at__gte=fa_period_start(market_session)).count()
        max_m = int((market_session.config or {}).get("fa_max_moves") or 0)
        if max_m > 0:
            moves_left = max(0, max_m - moves_this_week)

    if market_session and is_buyout_clause:
        opp_qs = Player.objects.filter(league=league, owner__isnull=False).exclude(owner=participant).select_related("owner").order_by("owner__display_name", "role", "-cost", "name")
        hold_cfg = (market_session.config or {}).get("buyout_min_hold_days")
        hold_days = int(hold_cfg) if hold_cfg is not None else 7  # 0 = nessuna protezione
        now = timezone.now()
        for pl in opp_qs:
            pl.buyout_price = int(buyout_price(market_session, pl))
            if hold_days > 0 and pl.acquired_at:
                days_held = (now - pl.acquired_at).total_seconds() / 86400.0
                if days_held < hold_days:
                    pl.is_protected = True
                    pl.protected_days_left = max(1, int(hold_days - days_held) + 1)
                else:
                    pl.is_protected = False
                    pl.protected_days_left = 0
            else:
                pl.is_protected = False
                pl.protected_days_left = 0
            opponents_players.append(pl)

    if market_session and is_waiver_wire:
        waiver_claims = services.get_participant_market_bids(market_session.id, participant.id)
        teams = {t.id: t for t in Participant.objects.filter(league=league, is_active=True)}
        waiver_order_preview = [teams[pid].display_name for pid in waiver_order(market_session, teams)]

    my_expiring = []
    my_to_roll = []
    renewals_declared = False
    has_undecided_renewals = False
    league_expiring_summary = []
    contract_rules_info = None
    contract_faces_list = [1, 2, 3]

    if league and league.contracts_enabled:
        contract_rules_info = services.contract_rules(league)
        contract_faces_list = sorted(set(services.contract_faces(league)))
        my_expiring = list(Player.objects.filter(owner=participant, contract_years=0, abroad_list=False).order_by("role", "name"))
        my_to_roll = list(Player.objects.filter(owner=participant, contract_years__isnull=True, abroad_list=False).order_by("role", "name"))
        for p in my_to_roll:
            p.min_years = services.contract_min_years(league, p.cost, p.role)
        renewals_declared = len(my_expiring) > 0 and all(p.renewal_declared is not None for p in my_expiring)
        has_undecided_renewals = any(p.renewal_declared is None for p in my_expiring)

        for t in Participant.objects.filter(league=league, is_active=True).order_by("display_name"):
            t_exp = list(Player.objects.filter(owner=t, contract_years=0, abroad_list=False).order_by("role", "name"))
            if t_exp:
                league_expiring_summary.append({
                    "team": t,
                    "players": t_exp,
                    "declared": all(p.renewal_declared is not None for p in t_exp),
                    "count": len(t_exp),
                })

    buste_results = None
    if market_session and market_session.status == MarketSession.Status.RESOLVED:
        summary = market_session.results_summary or {}
        won_list = summary.get("won", [])
        lost_list = summary.get("lost", [])
        tied_list = summary.get("tied", [])
        my_won = [w for w in won_list if w.get("winner_id") == participant.id or w.get("winner_name") == participant.display_name]
        my_lost = [l for l in lost_list if l.get("participant_name") == participant.display_name]
        buste_results = {
            "won": won_list,
            "lost": lost_list,
            "tied": tied_list,
            "my_won": my_won,
            "my_lost": my_lost,
            "total_acquisitions": summary.get("total_acquisitions", len(won_list)),
            "resolved_at": summary.get("resolved_at"),
            "renewed": summary.get("renewed", []),
            "rescinded": summary.get("rescinded", []),
            "released": summary.get("released", []),
        }

    # Initial view logic: entering /app/mercato/ lands on the Hub.
    # Selecting a session (?session_id=X) or listone (?view=listone) lands on the workspace.
    requested_view = request.GET.get("view")
    if requested_session_id:
        initial_view = "workspace"
    elif requested_view in ("workspace", "listone"):
        initial_view = requested_view
    elif q or role or in_budget or request.GET.get("page"):
        initial_view = "workspace" if market_session else "listone"
    else:
        initial_view = "hub"
    if initial_view == "listone":
        # The listone is a plain list to browse: no session's tabs (an open
        # buyout window used to open it on «Rose & Clausole») and no buttons.
        is_free_agency = is_buyout_clause = is_waiver_wire = is_live_auction = False
        is_renewals = is_buste = market_open = False

    ctx.update({
        "free_agents": page.object_list,
        "page": page,
        "base_query": base_query.urlencode(),
        "in_budget": in_budget,
        "initial_view": initial_view,
        "is_listone_view": (initial_view == "listone"),
        "active_markets_count": active_markets_count,
        "role_filters": [("", "Tutti"), ("P", "Portieri"), ("D", "Difensori"), ("C", "Centrocampisti"), ("A", "Attaccanti")],
        "plan": services.roster_plan(participant),
        "cap": _cap_ctx(participant),
        "my_roster": my_roster,
        "market_session": market_session,
        "sessions_list": sessions_list,
        "active_sessions": active_sessions,
        "past_sessions": past_sessions,
        "market_open": market_open,
        "my_bids": my_bids,
        "my_bid_player_ids": my_bid_player_ids,
        "my_bids_total": my_bids_total,
        "bids_left": bids_left,
        "moves_this_week": moves_this_week,
        "moves_left": moves_left,
        "opponents_players": opponents_players,
        "waiver_claims": waiver_claims,
        "waiver_order_preview": waiver_order_preview,
        "is_free_agency": is_free_agency,
        "is_buyout_clause": is_buyout_clause,
        "is_waiver_wire": is_waiver_wire,
        "is_live_auction": is_live_auction,
        "is_renewals": is_renewals,
        "is_buste": is_buste,
        "my_expiring": my_expiring,
        "my_to_roll": my_to_roll,
        "renewals_declared": renewals_declared,
        "has_undecided_renewals": has_undecided_renewals,
        "league_expiring_summary": league_expiring_summary,
        "contract_rules_info": contract_rules_info,
        "contract_faces_list": contract_faces_list,
        "buste_results": buste_results,
        "mk_labels": session_labels(market_session) if market_session else None,
        # Choices of the «Nuovo Mercato» wizard (_market_wizard.html).
        **rule_choices(),
        "trades_enabled": bool(league and league.trades_enabled),
        "incoming_trades": Trade.objects.filter(receiver=participant, status=Trade.Status.PENDING).count(),
        "role": role,
        "q": q,
        "sort": sort,
        "refund_mode": refund_mode,
        "is_mantra": is_mantra,
        "market_session_types": MarketSession.SessionType.choices,
        # The «Nuovo Mercato» wizard, shared with the console (_market_wizard.html).
        "mail_ready": mail.is_ready(),
        "open_wizard": request.GET.get("open_wizard") == "1",
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


@require_POST
def app_market_free_agency_buy(request):
    """Instant acquisition endpoint for Free Agency."""
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
    release_player_id = body.get("release_player_id") or None

    res = services.acquire_free_agent(
        session_id=session_id,
        participant_id=participant.id,
        player_id=player_id,
        release_player_id=release_player_id,
    )
    return JsonResponse(res)


@require_POST
def app_market_buyout_execute(request):
    """Buyout clause execution endpoint."""
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
    release_player_id = body.get("release_player_id") or None

    res = services.execute_buyout(
        session_id=session_id,
        buyer_id=participant.id,
        player_id=player_id,
        release_player_id=release_player_id,
    )
    return JsonResponse(res)


@require_POST
def app_market_waiver_claim(request):
    """Submit or update a waiver wire claim."""
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
    priority = body.get("priority", 1)
    release_player_id = body.get("release_player_id") or None

    res = services.place_waiver_claim(
        session_id=session_id,
        participant_id=participant.id,
        player_id=player_id,
        priority=priority,
        release_player_id=release_player_id,
    )
    if res.get("ok") and session_id:
        res["my_claims"] = services.get_participant_market_bids(session_id, participant.id)
    return JsonResponse(res)


@require_POST
def app_market_waiver_delete(request):
    """Delete a waiver wire claim."""
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
    claim_id = body.get("claim_id") or body.get("bid_id")

    res = services.delete_waiver_claim(
        session_id=session_id,
        participant_id=participant.id,
        claim_id=claim_id,
    )
    if res.get("ok") and session_id:
        res["my_claims"] = services.get_participant_market_bids(session_id, participant.id)
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
        return _redirect_login(request, ctx)
    league = participant.league
    mine = Trade.objects.filter(Q(proposer=participant) | Q(receiver=participant)).select_related(
        "proposer", "receiver"
    ).prefetch_related("proposer_players", "receiver_players")

    window_open, window = services.trade_window_status(league) if league is not None else (True, None)
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
        "trades_same_roles": bool(league and league.trades_same_roles),
        "trade_window_open": window_open,
        "trade_window": window,
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
    participant, ctx = _app_ctx(request, "mercato")
    if participant is None:
        return _redirect_login(request, ctx)
    res = services.propose_trade(
        participant.id,
        request.POST.get("receiver_id"),
        give_ids=request.POST.getlist("give"),
        get_ids=request.POST.getlist("get"),
        give_credits=request.POST.get("give_credits") or 0,
        get_credits=request.POST.get("get_credits") or 0,
        message=request.POST.get("message") or "",
        kind=request.POST.get("kind") or "definitive",
        loan_sessions=request.POST.get("loan_sessions") or 1,
    )
    return _trade_feedback(request, res, "Proposta di prestito inviata." if request.POST.get("kind") == "loan"
                           else "Proposta di scambio inviata.")


@require_POST
def app_trade_respond(request, trade_id):
    participant, ctx = _app_ctx(request, "mercato")
    if participant is None:
        return _redirect_login(request, ctx)
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
    participant, ctx = _app_ctx(request, "mercato")
    if participant is None:
        return _redirect_login(request, ctx)
    return _trade_feedback(request, services.cancel_trade(trade_id, participant.id), "Proposta ritirata.")


def _contract_feedback(request, res, ok_message):
    is_ajax = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or request.content_type == "application/json"
        or "application/json" in request.headers.get("accept", "")
    )
    if is_ajax:
        msg = ok_message(res) if res.get("ok") else (res.get("message") or "Operazione non riuscita.")
        res["feedback_message"] = msg
        return JsonResponse(res)

    if res.get("ok"):
        messages.success(request, ok_message(res))
    else:
        messages.error(request, res.get("message") or "Operazione non riuscita.")

    target = request.POST.get("next") or request.GET.get("next")
    if target == "mercato":
        return redirect("app_mercato")
    return redirect("app_rosa")


@require_POST
def app_contract_roll(request, player_id):
    participant, ctx = _app_ctx(request, "rosa")
    if participant is None:
        return _redirect_login(request, ctx)
    res = services.roll_contract(player_id, participant_id=participant.id)
    return _contract_feedback(request, res, lambda r: (
        f"🧤 {r['player_name']}: blocco portieri, stesso contratto di {r['block']} "
        f"({r['years']} ann{'o' if r['years'] == 1 else 'i'})" if r.get("block") else
        f"🎲 Dado contratti per {r['player_name']}: {r['face']} "
        + (f"→ {r['years']} anni (minimo {r['floor']} per la clausola)" if r["years"] != r["face"] else
           f"ann{'o' if r['years'] == 1 else 'i'} di contratto")))


@require_POST
def app_contract_u21(request, player_id):
    participant, ctx = _app_ctx(request, "rosa")
    if participant is None:
        return _redirect_login(request, ctx)
    res = services.declare_u21(player_id, participant_id=participant.id)
    return _contract_feedback(request, res, lambda r: (
        f"Scommessa Under 21 dichiarata: {r['player_name']} ha {r['years']} anni di contratto."))


@require_POST
def app_list_release(request, player_id):
    """5.09: in sede d'asta si svincola un giocatore dalla lista ceduti e si incassa."""
    participant, ctx = _app_ctx(request, "rosa")
    if participant is None:
        return _redirect_login(request, ctx)
    if ctx.get("active_auction") is None:
        messages.error(request, "Dalla lista ceduti si svincola solo in sede d'asta (estiva o invernale).")
        return redirect("app_rosa")
    from ..services.abroad import release_from_list

    res = release_from_list(player_id, participant_id=participant.id)
    return _contract_feedback(request, res, lambda r: f"Svincolato dalla lista ceduti: +{r['amount']} FM.")


@require_POST
def app_renewals_declare(request):
    participant, ctx = _app_ctx(request, "rosa")
    if participant is None:
        return _redirect_login(request, ctx)
    res = services.declare_renewals(participant.id, request.POST.getlist("renew"))
    return _contract_feedback(request, res, lambda r: (
        f"Rinnovi dichiarati: {r['renewing']} da rinnovare"
        + (f", svincolati {', '.join(r['released'])}" if r["released"] else "") + "."))


@require_POST
def app_renewal_roll(request, player_id):
    participant, ctx = _app_ctx(request, "rosa")
    if participant is None:
        return _redirect_login(request, ctx)
    res = services.roll_renewal(player_id, participant_id=participant.id)
    return _contract_feedback(request, res, lambda r: (
        f"🟢 Dado rinnovo verde: {r['player_name']} rinnova per {r['years']} ann{'o' if r['years'] == 1 else 'i'}."
        if r["green"] else f"🔴 Dado rinnovo rosso: {r['player_name']} rescinde e torna svincolato."))


def app_altro(request):
    participant, ctx = _app_ctx(request, "altro")
    if ctx is None:
        return _redirect_login(request, ctx)
    return render(request, "auctions/app_altro.html", ctx)


@require_POST
def app_update_pin(request):
    """Allow manager to update their team's access_code (PIN)."""
    participant, ctx = _app_ctx(request, "altro")
    if participant is None:
        return _redirect_login(request, ctx)

    new_code = (request.POST.get("access_code") or "").strip().upper()[:20]
    # Le stesse regole dei codici scelti dall'admin: lunghezza minima e
    # unico in tutte le leghe (il codice da solo fa entrare nella squadra).
    code_error = custom_code_error(new_code, exclude_pk=participant.pk)
    if code_error:
        messages.error(request, code_error)
        return redirect("app_altro")

    participant.access_code = new_code
    participant.save(update_fields=["access_code"])
    messages.success(request, f"Codice di accesso aggiornato: {new_code}")
    return redirect("app_altro")


def app_formazione(request):
    """Lineup builder (inside Rosa): pick a module, assign starters per slot,
    order the bench, save — for the next giornata still to be played (see
    ``target_giornata``); once it starts its lineup is frozen."""
    participant, ctx = _app_ctx(request, "rosa")
    if participant is None:
        return _redirect_login(request, ctx)
    giornata = services.target_giornata(participant.league)
    if request.method == "POST":
        posted = request.POST.get("giornata") or ""
        if posted and posted != str(giornata.id if giornata else ""):
            # The page was for a giornata that has started since it was opened.
            messages.error(request, "Quella giornata è iniziata e la sua formazione è bloccata. "
                                    + (f"Ora schieri per la Giornata {giornata.number}." if giornata else ""))
            return redirect("app_formazione")
        services.save_formation(participant, request.POST.get("module", ""), request.POST.getlist("starter"),
                                request.POST.getlist("bench"), giornata=giornata,
                                captain=request.POST.get("captain"), vice=request.POST.get("vice"))
        if request.POST.get("save"):
            messages.success(request, f"Formazione salvata per la Giornata {giornata.number}." if giornata
                             else "Formazione salvata.")
        return redirect("app_formazione")
    ctx.update(services.formation_state(participant, giornata=giornata))
    ctx["giornata"] = giornata
    return render(request, "auctions/app_formazione.html", ctx)


def _claims_team(user, team):
    """Whether opening ``team`` from the login links it to ``user``'s account.

    Only a manager's first team in that league: an admin checking teams by code
    (or a manager who already has a team there) is visiting, not claiming. A
    stray link kept the team on the wrong account for good: the admin's next
    login opened it instead of their own, and its manager could no longer pick
    it from the list.
    """
    if user_can_manage_scope(user, team.league):
        return False
    return not Participant.objects.filter(user=user, league_id=team.league_id).exists()


def app_login(request):
    """Dedicated login for the managerial area (FantaManager).

    Supports:
    1. Direct tokenized access via ?t=<token>
    2. Account login (Username / Email + Password)
    3. Quick access code entry (e.g. 'DRAGO23')
    4. Guided team selection (League -> Team) with optional access code check
    """
    next_url = safe_next(request, reverse("app_home"))

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
        mine = list(Participant.objects.filter(user=request.user, is_active=True)[:2])
        current = request.session.get("participant_id")
        participant = None
        if len(mine) == 1:
            participant = mine[0]
        elif mine and Participant.objects.filter(pk=current, user=request.user, is_active=True).exists():
            return redirect(next_url)          # already playing one of their teams
        # Two or more teams (in different leagues): the page below asks which.
        if participant:
            request.session["participant_id"] = participant.id
            request.session["display_name"] = participant.display_name
            if participant.league_id is not None:
                request.session[SESSION_LEAGUE_KEY] = participant.league_id
            return redirect(next_url)
        # A league admin with no team of their own: the app opens on the
        # Regia, not on a login form they have nothing to type into.
        if app_admin_leagues(request.user):
            return redirect("app_regia")

    if request.method == "POST":
        login_mode = request.POST.get("login_mode", "")
        identifier = (request.POST.get("identifier") or request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""
        access_code = (request.POST.get("access_code") or "").strip()
        participant_id = (request.POST.get("participant_id") or "").strip()

        # Deduce mode if not explicitly tagged
        if not login_mode:
            if identifier or password.strip():
                login_mode = "account"
            elif participant_id:
                login_mode = "select"
            elif access_code:
                login_mode = "code"

        active_mode = login_mode or "account"
        participant = None

        if login_mode == "account":
            if not identifier or not password.strip():
                error = "Inserisci nome utente / email e password."
            elif throttle.blocked(request, "login"):
                error = throttle.MESSAGE
            else:
                user = authenticate_identifier(request, identifier, password)
                if user is None:
                    throttle.failure(request, "login")
                    error = "Credenziali non valide. Verifica username/email e password."
                elif not user.is_active:
                    error = "Questo account è disattivato. Contatta l'amministratore."
                else:
                    auth_login(request, user)
                    teams = list(Participant.objects.filter(user=user, is_active=True)[:2])
                    if len(teams) > 1:
                        # Teams in more than one league: the account picks, never the database order.
                        request.session.pop("participant_id", None)
                        request.session.pop("display_name", None)
                        messages.info(request, "Hai più squadre: scegli con quale entrare.")
                        from urllib.parse import urlencode
                        return redirect(f"{reverse('app_login')}?{urlencode({'switch': 1, 'next': next_url})}")
                    if teams:
                        participant = teams[0]
                    elif app_admin_leagues(user):
                        # No team, but a league to run: straight to the Regia,
                        # where "Vedi come" opens any team on purpose instead
                        # of dropping the admin into an arbitrary one.
                        request.session.pop("participant_id", None)
                        request.session.pop("display_name", None)
                        return redirect("app_regia")
                    else:
                        error = "Nessuna squadra associata a questo account. Usa la scheda 'Codice Squadra' per collegare la tua rosa."

        elif login_mode == "code" or (access_code and not participant_id):
            if not access_code:
                error = "Inserisci il codice della tua squadra."
            elif throttle.blocked(request, "code"):
                error = throttle.MESSAGE
            else:
                participant, ambiguous = find_team_by_code(access_code)
                if ambiguous:
                    error = AMBIGUOUS_CODE_MESSAGE
                elif not participant:
                    throttle.failure(request, "code")
                    error = "Codice squadra non valido o non riconosciuto."
                elif participant.user_id is not None and request.user.is_authenticated and request.user.id != participant.user_id:
                    error = "Questa squadra è già associata a un altro account utente."
                elif request.user.is_authenticated and participant.user is None and _claims_team(request.user, participant):
                    # Link unassigned team to the currently logged-in user
                    participant.user = request.user
                    participant.save(update_fields=["user"])

        elif login_mode == "select" or participant_id:
            if participant_id and participant_id.isdigit():
                candidate = Participant.objects.filter(pk=int(participant_id), is_active=True).first()
                if candidate:
                    if request.user.is_authenticated and candidate.user_id == request.user.id:
                        participant = candidate              # one of the account's own teams
                    elif candidate.user_id is not None and (not request.user.is_authenticated or request.user.id != candidate.user_id):
                        error = f"{candidate.display_name} è associata all'account di un utente. Accedi con Username e Password."
                    elif candidate.access_code and throttle.blocked(request, "code"):
                        error = throttle.MESSAGE
                    elif candidate.access_code and candidate.access_code.lower() != access_code.lower():
                        throttle.failure(request, "code")
                        error = f"Codice di accesso errato per {candidate.display_name}."
                    elif not candidate.access_code and not request.user.is_authenticated:
                        error = f"Per gestire {candidate.display_name} accedi al tuo account o inserisci il codice squadra."
                    elif not candidate.access_code and not user_can_manage_scope(request.user, candidate.league):
                        # Registration is open: "logged in" alone would let any
                        # account take (and keep) any league's team without a code.
                        error = (f"{candidate.display_name} non ha un codice squadra: "
                                 "chiedi a chi organizza la lega di collegarla al tuo account.")
                    else:
                        participant = candidate
                        if request.user.is_authenticated and participant.user is None and _claims_team(request.user, participant):
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

    leagues = visible_leagues(request).prefetch_related("participants").order_by("name")
    user_teams = []
    if request.user.is_authenticated:
        user_teams = list(Participant.objects.filter(user=request.user, is_active=True)
                          .select_related("league").order_by("league__name", "display_name"))

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


def app_fixture_detail(request, fixture_id):
    """JSON API endpoint returning the full match sheet details for a fixture:
    starters, benches, votes, fantavoti, substitutions, cards, goals, modifier.
    """
    from django.shortcuts import get_object_or_404
    from ..services.competitions import get_fixture_details

    fixture = get_object_or_404(
        Fixture.objects.select_related("giornata", "giornata__season", "giornata__season__league",
                                       "home", "away", "competition"),
        id=fixture_id,
    )
    # Lineups and votes belong to the league: its teams and its admins only.
    league = fixture.giornata.season.league
    participant = _session_participant(request)
    in_league = participant is not None and league is not None and participant.league_id == league.id
    if not (in_league or user_can_manage_league(request.user, league)):
        return JsonResponse({"success": False, "error": "Partita non disponibile."}, status=404)
    details = get_fixture_details(fixture)
    return JsonResponse({"success": True, "fixture": details})


def app_print_team_sheet(request, participant_id=None):
    """Visualizza e stampa la Scheda Squadra ufficiale (formato A4 identico al PDF di lega)."""
    participant, ctx = _app_ctx(request, "rosa")
    if participant is None:
        return _redirect_login(request, ctx)
    league = participant.league
    target_id = participant.id
    if participant_id:
        target_p = Participant.objects.filter(pk=participant_id, league=league).first()
        if target_p:
            target_id = target_p.id
    elif request.GET.get("team"):
        try:
            target_p = Participant.objects.filter(pk=int(request.GET.get("team")), league=league).first()
            if target_p:
                target_id = target_p.id
        except (ValueError, TypeError):
            pass
    from .. import team_sheets
    sheets = team_sheets.team_sheets(league, [target_id])
    back_url = reverse("app_rosa") if target_id == participant.id else f"{reverse('app_rosa')}?team={target_id}"
    export_xlsx_url = reverse("app_export_team_sheet_xlsx", args=[target_id])
    return render(request, "auctions/print_team_sheets.html", {
        "league": league,
        "sheets": sheets,
        "back_url": back_url,
        "export_xlsx_url": export_xlsx_url,
    })


def app_export_team_sheet_xlsx(request, participant_id=None):
    """Download del foglio Excel della Scheda Squadra."""
    participant, ctx = _app_ctx(request, "rosa")
    if participant is None:
        return _redirect_login(request, ctx)
    league = participant.league
    target_p = participant
    if participant_id:
        p = Participant.objects.filter(pk=participant_id, league=league).first()
        if p:
            target_p = p
    elif request.GET.get("team"):
        try:
            p = Participant.objects.filter(pk=int(request.GET.get("team")), league=league).first()
            if p:
                target_p = p
        except (ValueError, TypeError):
            pass
    from .. import team_sheets
    data = team_sheets.build_team_sheets_xlsx(league, [target_p.id])
    slug_name = "".join(c if c.isalnum() else "_" for c in target_p.display_name).strip("_")
    resp = HttpResponse(data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{slug_name}_scheda.xlsx"'
    return resp


def app_print_renewals(request):
    """Visualizza e stampa il Report Ufficiale Rinnovi Contratti della lega."""
    participant, ctx = _app_ctx(request, "mercato")
    if participant is None:
        return _redirect_login(request, ctx)
    league = participant.league
    from .. import team_sheets
    season = team_sheets.season_label(team_sheets.season_start())
    blocks = team_sheets.renewal_rows(league)
    return render(request, "auctions/print_renewals.html", {
        "league": league,
        "season": season,
        "blocks": blocks,
        "back_url": f"{reverse('app_mercato')}?tab=rinnovi",
        "export_xlsx_url": reverse("app_export_renewals_xlsx"),
    })


def app_export_renewals_xlsx(request):
    """Download del foglio Excel ufficiale dei rinnovi della lega."""
    participant, ctx = _app_ctx(request, "mercato")
    if participant is None:
        return _redirect_login(request, ctx)
    league = participant.league
    from .. import team_sheets
    data = team_sheets.build_renewals_xlsx(league)
    season = team_sheets.season_label(team_sheets.season_start())
    resp = HttpResponse(data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="Rinnovi_{season}.xlsx"'
    return resp


def app_print_buste_report(request, session_id):
    """Visualizza e stampa il Verbale Ufficiale di Spoglio Buste."""
    participant, ctx = _app_ctx(request, "mercato")
    if participant is None:
        return _redirect_login(request, ctx)
    session = MarketSession.objects.filter(pk=session_id, league=participant.league).first()
    if not session:
        return redirect("app_mercato")
    ctx_report = services.get_buste_report_context(session.id)
    if not ctx_report:
        return redirect("app_mercato")
    ctx_report["back_url"] = f"{reverse('app_mercato')}?session_id={session.id}&tab=esito_spoglio"
    ctx_report["csv_url"] = reverse("app_export_buste_csv", args=[session.id])
    return render(request, "auctions/print_market_buste.html", ctx_report)


def app_export_buste_csv(request, session_id):
    """Scarica il file CSV del Verbale Ufficiale di Spoglio Buste."""
    participant, ctx = _app_ctx(request, "mercato")
    if participant is None:
        return _redirect_login(request, ctx)
    session = MarketSession.objects.filter(pk=session_id, league=participant.league).first()
    if not session:
        return redirect("app_mercato")
    csv_bytes = services.build_buste_csv(session)
    name = f"Verbale_Spoglio_{session.id}.csv"
    resp = HttpResponse(csv_bytes, content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp

