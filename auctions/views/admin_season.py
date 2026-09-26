"""Admin 'Stagione': classifiche, tetto salariale, Decreto Salvacalcio e passaggi di stagione."""
import json

from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..models import CapPhase, DecreeAward, League, LeagueRanking, Participant
from ..services import salary, season
from .common import current_league, manageable_leagues, staff_member_required, target_league, user_can_manage_league

_FORBIDDEN = "Non hai i permessi per gestire la stagione di questa lega."
_TABLES = ("base_by_rank", "winter_by_rank", "decree_mid", "decree_final")
_NUMBERS = ("max_per_year", "budget_max", "renewal_lost_bonus", "extra_cap_cost", "extra_cap_gain")


def _url(league):
    return f"{reverse('admin_season')}?league={league.id}" if league else reverse("admin_season")


def _order_from_post(post, prefix, teams):
    """Posizioni scritte a mano (pos_<id>) → lista di id in ordine, o None."""
    positions = {}
    for t in teams:
        raw = (post.get(f"{prefix}_{t.id}") or "").strip()
        if not raw:
            return None
        if not raw.isdigit():
            raise ValueError("Le posizioni devono essere numeri.")
        positions[t.id] = int(raw)
    if sorted(positions.values()) != list(range(1, len(teams) + 1)):
        raise ValueError("Le posizioni devono andare da 1 al numero di squadre, senza doppioni.")
    return [pid for pid, _ in sorted(positions.items(), key=lambda kv: kv[1])]


@staff_member_required
def admin_season(request):
    league = current_league(request)
    if league is not None and not user_can_manage_league(request.user, league):
        return HttpResponseForbidden(_FORBIDDEN)
    ctx = {
        "leagues": manageable_leagues(request.user),
        "current_league": league,
        "console_section": "Stagione",
        "console_active": "season",
    }
    if league is not None:
        teams = list(Participant.objects.filter(league=league, is_active=True).order_by("display_name"))
        final = LeagueRanking.objects.filter(league=league, season=league.season_number - 1,
                                             kind=LeagueRanking.Kind.FINAL).first()
        current_final = LeagueRanking.objects.filter(league=league, season=league.season_number,
                                                     kind=LeagueRanking.Kind.FINAL).first()
        mid = LeagueRanking.objects.filter(league=league, season=league.season_number,
                                           kind=LeagueRanking.Kind.MIDSEASON).first()
        caps = []
        for t in teams:
            status = salary.cap_status(t)
            caps.append({
                "team": t,
                "status": status,
                "final_pos": final.position_of(t.id) if final else None,
                "mid_pos": mid.position_of(t.id) if mid else None,
                "end_pos": current_final.position_of(t.id) if current_final else None,
            })
        r = salary.rules(league)
        ctx.update({
            "teams": teams,
            "caps": caps,
            "phases": list(CapPhase.objects.filter(league=league, season=league.season_number)),
            "has_winter": CapPhase.objects.filter(league=league, season=league.season_number,
                                                  kind=CapPhase.Kind.WINTER).exists(),
            "decrees": list(DecreeAward.objects.filter(league=league)[:6]),
            "rules": r,
            "rule_tables": [(k, ",".join(str(x) for x in r[k])) for k in _TABLES],
            "rule_numbers": [(k, r[k]) for k in _NUMBERS],
            "lost_points": ",".join(f"{k}={v}" for k, v in r["lost_points"].items()),
            "lost_bonus": ";".join(f"{a}:{b}" for a, b in r["lost_bonus"]),
            "spend_modes": League.CapSpend.choices,
            "app_ranking_available": bool(salary.app_ranking(league)),
        })
    return render(request, "auctions/admin_season.html", ctx)


def _report(request, report):
    if not report.get("ok", True):
        messages.error(request, report.get("message") or "Operazione non riuscita.")
        return
    for step in report.get("steps", []):
        messages.success(request, step)
    for warning in report.get("warnings", []):
        messages.warning(request, warning)


@staff_member_required
@require_POST
def admin_season_action(request):
    league = target_league(request) or current_league(request)
    if league is None or not user_can_manage_league(request.user, league):
        return HttpResponseForbidden(_FORBIDDEN)
    action = request.POST.get("action")
    teams = list(Participant.objects.filter(league=league, is_active=True).order_by("display_name"))
    try:
        if action == "settings":
            league.salary_cap_enabled = request.POST.get("salary_cap_enabled") == "1"
            mode = request.POST.get("salary_cap_spend")
            if mode in League.CapSpend.values:
                league.salary_cap_spend = mode
            league.standings_url = (request.POST.get("standings_url") or "").strip()
            rules = dict(league.salary_rules or {})
            for key in _TABLES:
                values = [int(x) for x in (request.POST.get(key) or "").replace(";", ",").split(",") if x.strip().isdigit()]
                if values:
                    rules[key] = values
            for key in _NUMBERS:
                raw = (request.POST.get(key) or "").strip()
                if raw.isdigit():
                    rules[key] = int(raw)
            points = {}
            for part in (request.POST.get("lost_points") or "").split(","):
                if "=" in part:
                    role, value = part.split("=", 1)
                    points[role.strip().upper()] = float(value.replace(",", "."))
            if points:
                rules["lost_points"] = points
            bonus = []
            for part in (request.POST.get("lost_bonus") or "").split(";"):
                if ":" in part:
                    a, b = part.split(":", 1)
                    bonus.append([float(a.replace(",", ".")), int(b)])
            if bonus:
                rules["lost_bonus"] = bonus
            league.salary_rules = rules
            league.save(update_fields=["salary_cap_enabled", "salary_cap_spend", "standings_url", "salary_rules", "updated_at"])
            messages.success(request, "Impostazioni economia salvate.")
        elif action == "new_season":
            _report(request, season.start_new_season(league.id, _order_from_post(request.POST, "final", teams)))
        elif action == "close_renewals":
            _report(request, season.close_renewals(league.id))
        elif action == "midseason":
            _report(request, season.midseason(league.id, _order_from_post(request.POST, "mid", teams)))
        elif action == "previous_ranking":
            order = _order_from_post(request.POST, "prev", teams)
            if not order:
                raise ValueError("Inserisci la posizione di tutte le squadre.")
            salary.save_ranking(league, league.season_number - 1, LeagueRanking.Kind.FINAL, order)
            messages.success(request, "Classifica finale dell'anno precedente salvata.")
        elif action == "open_summer":
            prev = LeagueRanking.objects.filter(league=league, season=league.season_number - 1,
                                                kind=LeagueRanking.Kind.FINAL).first()
            salary.open_phase(league, CapPhase.Kind.SUMMER, prev)
            messages.success(request, "Fase estiva aperta: tetti calcolati.")
        elif action == "test_standings":
            from ..providers.standings import fetch_remote_ranking
            order = fetch_remote_ranking(league)
            if order:
                names = dict((t.id, t.display_name) for t in teams)
                messages.success(request, "Classifica letta: " + ", ".join(
                    f"{i}° {names.get(pid, pid)}" for i, pid in enumerate(order, 1)))
            else:
                messages.error(request, "Non riesco a leggere la classifica da quel link (pagina irraggiungibile, "
                                        "serve il login o i nomi delle squadre non corrispondono): inseriscila a mano.")
        elif action == "adjust":
            team = next(t for t in teams if str(t.id) == request.POST.get("participant_id"))
            salary.adjust_cap(league, team, request.POST.get("amount") or 0, request.POST.get("note") or "Rettifica admin")
            messages.success(request, f"Tetto di {team.display_name} rettificato.")
        else:
            messages.error(request, "Azione non riconosciuta.")
    except (ValueError, StopIteration, json.JSONDecodeError) as exc:
        messages.error(request, str(exc) or "Dati non validi.")
    return redirect(_url(league))
