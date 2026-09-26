"""Admin page for player contracts (regolamento 4): settings, dice, renewals, new season."""
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import services
from ..models import ContractEvent, Participant, Player
from .common import current_league, manageable_leagues, staff_member_required, target_league, user_can_manage_league

_FORBIDDEN = "Non hai i permessi per gestire i contratti di questa lega."


def _url(league):
    return f"{reverse('admin_contracts')}?league={league.id}" if league else reverse("admin_contracts")


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
            p.min_years = services.contract_min_years(league, p.cost)
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
    return render(request, "auctions/admin_contracts.html", {
        "leagues": manageable_leagues(request.user),
        "current_league": league,
        "teams": teams,
        "events": events,
        "contract_faces": [1, 2, 3, 4],
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
        for field in ("contract_min2_price", "contract_min3_price"):
            try:
                setattr(league, field, max(0, int(request.POST.get(field) or 0)))
            except ValueError:
                pass
        league.save(update_fields=["contracts_enabled", "contract_min2_price", "contract_min3_price", "updated_at"])
        messages.success(request, "Impostazioni contratti salvate.")
        return redirect(_url(league))

    if action == "new_season":
        res = services.new_season(league.id)
        ok = f"Stagione {res.get('season')} iniziata: {res.get('expired')} contratti scaduti, rinnovi aperti."
    elif action == "close_renewals":
        res = services.close_renewals(league.id)
        ok = "Finestra dei rinnovi chiusa."
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
