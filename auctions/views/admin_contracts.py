"""Admin page for player contracts (regolamento 4): settings, dice, renewals, new season."""
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import services
from ..models import ContractEvent, Participant, Player
from ..providers.apifootball import is_configured as apifootball_configured
from ..providers.uefa import RANKING_PAGE as UEFA_RANKING_PAGE
from .common import current_league, manageable_leagues, staff_member_required, target_league, user_can_manage_league

_FORBIDDEN = "Non hai i permessi per gestire i contratti di questa lega."


def _url(league):
    return f"{reverse('admin_contracts')}?league={league.id}" if league else reverse("admin_contracts")


def _detected(res):
    where = (f" (ranking UEFA {res['position']}°)" if res.get("position")
             else " — posizione nel ranking da indicare")
    extra = f" Ranking UEFA da caricare: {res['ranking_problem']}." if res.get("ranking_problem") else ""
    return f"{res.get('player_name')}: destinazione {res.get('club')}{where}.{extra}"


@staff_member_required
def admin_contracts(request):
    league = current_league(request)
    if league is not None and not user_can_manage_league(request.user, league):
        return HttpResponseForbidden(_FORBIDDEN)
    teams = []
    events = []
    if league is not None:
        players = sorted(
            Player.objects.filter(owner__league=league).select_related("owner"),
            key=lambda p: ("PDCA".find(p.role) % 5, p.name),
        )
        by_team = {}
        for p in players:
            p.min_years = services.contract_min_years(league, p.cost, p.role)
            by_team.setdefault(p.owner_id, []).append(p)
        for team in Participant.objects.filter(league=league).order_by("display_name"):
            roster = by_team.get(team.id, [])
            teams.append({
                "team": team,
                "players": roster,
                "pending": sum(1 for p in roster if p.contract_years is None),
                "expired": sum(1 for p in roster if p.contract_years == 0),
                "last_year": sum(1 for p in roster if p.contract_years == 1),
            })
        events = list(ContractEvent.objects.filter(league=league)[:40])
        from ..models import UefaClubRank
        left = list(Player.objects.filter(owner__league=league, left_serie_a_at__isnull=False)
                    .select_related("owner").order_by("owner__display_name", "name"))
        for p in left:
            p.preview = None
            if p.left_rank_kind:
                p.preview = services.abroad_compensation(p.role, p.left_rank_kind, p.left_rank_pos)
        listed = list(Player.objects.filter(owner__league=league, abroad_list=True).select_related("owner"))
        flaggable = list(Player.objects.filter(owner__league=league, abroad_list=False, left_serie_a_at__isnull=True)
                         .select_related("owner").order_by("owner__display_name", "role", "name"))
    return render(request, "auctions/admin_contracts.html", {
        "leagues": manageable_leagues(request.user),
        "current_league": league,
        "teams": teams,
        "events": events,
        "left": left if league is not None else [],
        "listed": listed if league is not None else [],
        "flaggable": flaggable if league is not None else [],
        "uefa_count": UefaClubRank.objects.count() if league is not None else 0,
        "uefa_ranking_page": UEFA_RANKING_PAGE,
        "apifootball": apifootball_configured() if league is not None else False,
        "to_detect": sum(1 for p in left if not p.left_club) if league is not None else 0,
        "contract_faces": sorted(set(services.contract_faces(league))) if league else [1, 2, 3],
        "crules": services.contract_rules(league) if league else None,
        "roles": [("P", "Portieri"), ("D", "Difensori"), ("C", "Centrocampisti"), ("A", "Attaccanti")],
        "console_section": "Contratti",
        "console_active": "contracts",
    })


@staff_member_required
@require_POST
def admin_contracts_action(request):
    league = target_league(request) or current_league(request)
    if league is None or not user_can_manage_league(request.user, league):
        return HttpResponseForbidden(_FORBIDDEN)
    action = request.POST.get("action")
    player_id = request.POST.get("player_id")
    if player_id and not Player.objects.filter(pk=player_id, owner__league=league).exists():
        return HttpResponseForbidden(_FORBIDDEN)

    if action == "settings":
        league.contracts_enabled = request.POST.get("contracts_enabled") == "1"
        rules = dict(league.contract_rules or {})

        def ints(raw):
            return [int(x) for x in str(raw or "").replace(";", ",").split(",") if x.strip().isdigit()]

        faces = ints(request.POST.get("faces"))
        if faces:
            rules["faces"] = faces
        thresholds, caps = {}, {}
        for role in "PDCA":
            pair = ints(request.POST.get(f"thr_{role}"))
            if len(pair) == 2:
                thresholds[role] = pair
            cap = ints(request.POST.get(f"cap_{role}"))
            if cap:
                caps[role] = cap[0]
        if thresholds:
            rules["thresholds"] = {**services.contract_rules(league)["thresholds"], **thresholds}
        if caps:
            rules["rescind_proceeds_cap"] = {**services.contract_rules(league)["rescind_proceeds_cap"], **caps}
        league.contract_rules = rules
        try:
            league.gk_max_clubs = max(0, int(request.POST.get("gk_max_clubs") or 0))
        except ValueError:
            pass
        league.save(update_fields=["contracts_enabled", "contract_rules", "gk_max_clubs", "updated_at"])
        messages.success(request, "Impostazioni contratti salvate.")
        return redirect(_url(league))

    if action in ("left_flag", "left_detect", "left_detect_all", "left_resolve", "list_release",
                  "uefa_fetch", "uefa_paste"):
        from ..providers import uefa
        from ..services import abroad
        if action == "left_flag":
            res = abroad.flag_player(player_id)
            ok = f"{res.get('player_name')} segnalato come uscito dalla Serie A."
            if res.get("ok") and apifootball_configured():
                # Come dopo l'import del listone: la destinazione si cerca subito.
                found = abroad.detect(player_id)
                ok += " " + (_detected(found) if found.get("ok") else found.get("message", ""))
        elif action == "left_detect":
            res = abroad.detect(player_id)
            ok = _detected(res)
        elif action == "left_detect_all":
            report = abroad.detect_all(league, limit=abroad.DETECT_ALL_LIMIT)
            summary = abroad.detect_summary(report)
            res = {"ok": bool(report["found"]) or not (report["missing"] or report["error"]),
                   "message": summary}
            ok = ("Rilevamento: " + summary) if summary else "Nessun giocatore da rilevare."
        elif action == "left_resolve":
            res = abroad.resolve(player_id, request.POST.get("outcome"), club=(request.POST.get("club") or "").strip(),
                                 position=request.POST.get("position"))
            ok = ("Segnalazione archiviata." if res.get("outcome") == "dismiss" else
                  f"Messo in lista ceduti: incasserà {res.get('deferred')} FM a fine contratto o allo svincolo." if res.get("outcome") == "list"
                  else f"Giocatore perso: +{res.get('amount')} FM alla squadra.")
        elif action == "list_release":
            res = abroad.release_from_list(player_id)
            ok = f"Svincolato dalla lista ceduti: +{res.get('amount')} FM."
        elif action == "uefa_fetch":
            rows, reason = uefa.fetch()
            res = {"ok": bool(rows), "message": (
                f"Ranking UEFA non scaricato: {reason}. Apri la classifica su uefa.com, "
                "copia la tabella e incollala nel riquadro «Ranking UEFA club».")}
            ok = f"Ranking UEFA aggiornato: {abroad.store_uefa_ranking(rows) if rows else 0} club."
        else:
            rows = uefa.parse_pasted(request.POST.get("ranking"))
            res = {"ok": bool(rows), "message": (
                "Nessuna riga valida: servono posizione e club su ogni riga "
                "(es. «1;Real Madrid»), oppure la tabella copiata da uefa.com.")}
            ok = f"Ranking UEFA salvato: {abroad.store_uefa_ranking(rows) if rows else 0} club."
        if res.get("ok"):
            messages.success(request, ok)
        else:
            messages.error(request, res.get("message") or "Operazione non riuscita.")
        return redirect(_url(league))

    if action in ("new_season", "close_renewals"):
        # Passano dall'orchestratore di stagione (Decreto, tetto salariale…).
        from ..services import season
        report = season.start_new_season(league.id) if action == "new_season" else season.close_renewals(league.id)
        for step in report.get("steps", []):
            messages.success(request, step)
        for warning in report.get("warnings", []):
            messages.warning(request, warning)
        return redirect(_url(league))
    elif action == "set":
        res = services.set_contract(player_id, request.POST.get("years"), note="Impostato dall'admin")
        ok = f"Contratto impostato a {res.get('years')} anni."
    elif action == "roll":
        manual = request.POST.get("manual_face") or None
        res = services.roll_contract(player_id, by_admin=True, manual_face=manual)
        ok = (f"🎲 {res.get('player_name')}: dado {res.get('face')} → {res.get('years')} anni"
              + (" (inserito a mano)" if manual else ""))
    elif action == "renew":
        outcome = request.POST.get("manual_outcome") or ""
        manual_green = {"green": True, "red": False}.get(outcome)
        face = request.POST.get("manual_face") or None
        res = services.roll_renewal(player_id, by_admin=True, manual_green=manual_green,
                                    manual_face=face if manual_green else None)
        ok = (f"🟢 {res.get('player_name')} rinnova per {res.get('years')} anni." if res.get("green")
              else f"🔴 {res.get('player_name')} rescinde e torna svincolato.")
    else:
        res = {"ok": False, "message": "Azione non riconosciuta."}
        ok = ""

    if res.get("ok"):
        messages.success(request, ok)
    else:
        messages.error(request, res.get("message") or "Operazione non riuscita.")
    return redirect(_url(league))
