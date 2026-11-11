"""Rose: le rose di tutte le squadre della lega in una pagina.

Una sola pagina per tre cornici, con lo stesso partial (``_rose.html``):

* console (``/dashboard/rose/``) e Regia dell'app (``/app/regia/rose/``):
  l'admin della lega vede tutte le rose e le gestisce (aggiunge un giocatore
  libero, cambia costo, squadra o contratto, svincola, regola i crediti);
* app del manager (``/app/rose/``, ``views/app.py:app_rose``): la sua rosa e
  quelle di tutti, in sola lettura.

I dati arrivano da :func:`rose_context`; le azioni passano da
:func:`admin_rose_action`, che torna alla pagina da cui parte (``next``).
"""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import services
from ..models import Auction, Participant, Player
from ..providers import importers
from .admin_participants import apply_team_credits
from .common import (
    current_league, managed_or_403, manageable_leagues, mixed_leagues, page_frame, safe_next,
    staff_member_required, user_can_manage_league,
)

_FORBIDDEN = "Non hai i permessi per gestire le rose di questa lega."
ROLE_LABELS = [("P", "Portieri"), ("D", "Difensori"), ("C", "Centrocampisti"), ("A", "Attaccanti")]
REFUND_CHOICES = [
    (Auction.RefundMode.PURCHASE, "rimborsa il costo pagato"),
    (Auction.RefundMode.CURRENT, "rimborsa la quotazione attuale"),
    (Auction.RefundMode.NONE, "nessun rimborso"),
]
SEARCH_LIMIT = 20


def _role_order(player):
    return ("PDCA".find(player.role) % 5, -(player.cost or 0), player.name)


def rose_context(request, league, *, manage, mine=None):
    """Le rose della lega: tutte (panoramica) o una (``?team=``).

    ``manage``: l'admin, con i comandi; ``mine``: la squadra di chi guarda
    (in evidenza e prima nella panoramica). Con ``?team=`` e ``?cerca=``
    l'admin cerca fra i giocatori liberi della lega quello da aggiungere.
    """
    teams = list(Participant.objects.filter(league=league).order_by("display_name"))
    if not manage:
        teams = [t for t in teams if t.is_active or (mine is not None and t.id == mine.id)]
    if mine is not None:
        teams.sort(key=lambda t: t.id != mine.id)
    contracts_on = bool(league.contracts_enabled)
    is_mantra = bool(league.is_mantra)

    by_team = {}
    for p in sorted(Player.objects.filter(owner__league=league).select_related("loan_from"), key=_role_order):
        by_team.setdefault(p.owner_id, []).append(p)

    cards = []
    for team in teams:
        players = by_team.get(team.id, [])
        roster = [p for p in players if not p.abroad_list]
        groups = []
        for code, label in ROLE_LABELS:
            in_role = [p for p in roster if p.role == code]
            groups.append({
                "code": code, "label": label, "list": in_role,
                # In Mantra gli slot si contano per POR / movimento, non per ruolo.
                "slots": 0 if is_mantra else league.slots_for(code),
                "cost": sum(p.cost or 0 for p in in_role),
            })
        slots_total = (league.slots_for("P") + league.slots_for("D")) if is_mantra else \
            sum(league.slots_for(code) for code, _ in ROLE_LABELS)
        cards.append({
            "team": team,
            "is_mine": mine is not None and team.id == mine.id,
            "groups": groups,
            "abroad": [p for p in players if p.abroad_list],
            "count": len(roster),
            "slots_total": slots_total,
            "value": sum(p.cost or 0 for p in roster),
            "pending": sum(1 for p in roster if contracts_on and p.contract_years is None),
            "expired": sum(1 for p in roster if contracts_on and p.contract_years == 0),
        })

    selected = None
    raw = (request.GET.get("team") or "").strip()
    if raw.isdigit():
        selected = next((c for c in cards if c["team"].id == int(raw)), None)

    query = (request.GET.get("cerca") or "").strip() if manage and selected else ""
    found, found_total = [], 0
    if len(query) >= 2:
        needle = importers.normalize_name(query)
        for p in Player.objects.filter(league=league, owner__isnull=True).order_by("role", "name").only(
                "id", "name", "role", "team", "initial_price"):
            if needle in importers.normalize_name(p.name) or needle in importers.normalize_name(p.team or ""):
                found_total += 1
                if len(found) < SEARCH_LIMIT:
                    found.append(p)

    base = f"?league={league.id}" if manage else "?"
    return {
        "rose": {
            "manage": manage,
            # Link della riga delle squadre: nell'admin la lega va nell'indirizzo
            # (console e Regia), nell'app del manager vale quella della sessione.
            "all_url": base,
            "team_url": base + ("&" if manage else "") + "team=",
            "cards": cards,
            "selected": selected,
            "mine": mine,
            "contracts_on": contracts_on,
            "is_mantra": is_mantra,
            "contract_years": list(range(0, services.contract_max_years(league) + 1)) if contracts_on else [],
            "refund_choices": REFUND_CHOICES,
            "query": query,
            "found": found,
            "found_total": found_total,
            "search_limit": SEARCH_LIMIT,
            "free_count": Player.objects.filter(league=league, owner__isnull=True).count() if manage else 0,
        },
    }


@staff_member_required
def admin_rose(request):
    league = current_league(request)
    if league is not None and not user_can_manage_league(request.user, league):
        return HttpResponseForbidden(_FORBIDDEN)
    ctx = {
        **page_frame(request, league, own_messages=True),
        "leagues": manageable_leagues(request.user),
        "current_league": league,
        "console_section": "Rose",
        "console_active": "rose",
    }
    if league is not None:
        ctx.update(rose_context(request, league, manage=True))
    return render(request, "auctions/admin_rose.html", ctx)


def _price(raw):
    """Un prezzo dal form: intero o decimale non negativo, altrimenti None."""
    try:
        value = Decimal(str(raw).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return value if value >= 0 else None


def _error_text(res, fallback="Operazione non riuscita."):
    code = res.get("error")
    return res.get("message") or services.ERROR_LABELS.get(code) or {
        "not_owned": "Il giocatore non è in nessuna rosa.",
        "player_not_found": "Giocatore non trovato.",
        "participant_not_found": "Squadra non trovata.",
    }.get(code) or fallback


def _fm(value):
    return f"{Decimal(str(value or 0)):.0f} FM"


@staff_member_required
@require_POST
def admin_rose_action(request):
    """Le azioni della pagina Rose: ``assign`` (un giocatore libero a una
    squadra), ``edit`` (costo, squadra, contratto), ``release`` (svincolo col
    rimborso scelto) e ``credits`` (crediti della squadra)."""
    action = request.POST.get("action")
    team = player = None
    if request.POST.get("participant_id"):
        team, denied = managed_or_403(request, Participant, request.POST.get("participant_id"))
        if denied:
            return denied
    if request.POST.get("player_id"):
        player, denied = managed_or_403(request, Player, request.POST.get("player_id"))
        if denied:
            return denied
    league = (team.league if team is not None else None) or (player.league if player is not None else None)
    if league is None or not user_can_manage_league(request.user, league):
        return HttpResponseForbidden(_FORBIDDEN)
    if mixed_leagues(team, player) or (player is not None and player.league_id != league.id):
        messages.error(request, services.ERROR_LABELS["league_mismatch"])
        return redirect(safe_next(request, f"{reverse('admin_rose')}?league={league.id}"))
    who = request.user.get_username()
    back = safe_next(request, f"{reverse('admin_rose')}?league={league.id}"
                              + (f"&team={team.id}" if team is not None else ""))

    if action == "credits" and team is not None:
        messages.success(request, apply_team_credits(team, request.POST.get("mode"), request.POST.get("amount"),
                                                       request.user))
        return redirect(back)

    if player is None:
        messages.error(request, "Scegli un giocatore e riprova.")
        return redirect(back)

    if action == "assign":
        if team is None:
            messages.error(request, "Scegli la squadra a cui assegnare il giocatore e riprova.")
        elif player.owner_id is not None:
            messages.error(request, f"{player.name} è già nella rosa di {player.owner.display_name}: "
                                    "cambialo da lì con «Modifica».")
        else:
            price = _price(request.POST.get("price") or player.initial_price)
            if price is None:
                messages.error(request, "Prezzo non valido: scrivi un numero di FM (0 o più) e riprova.")
            else:
                res = services.assign_player(player.id, team.id, price=price, by_admin=True,
                                             note=f"Aggiunto dalla pagina Rose · {who}")
                if res.get("ok"):
                    messages.success(request, f"{player.name} aggiunto a «{team.display_name}» per {_fm(price)}.")
                else:
                    messages.error(request, _error_text(res))
        return redirect(back)

    if player.owner_id is None:
        messages.error(request, f"{player.name} non è più in nessuna rosa: ricarica la pagina.")
        return redirect(back)

    if action == "release":
        mode = request.POST.get("refund") or Auction.RefundMode.PURCHASE
        owner = player.owner
        res = services.release_player(player.id, by_admin=True, refund_mode=mode,
                                      note=f"pagina Rose · {who}")
        if res.get("ok"):
            messages.success(request, f"{player.name} svincolato da «{owner.display_name}»: "
                                      f"+{_fm(res.get('refund'))} di rimborso.")
        else:
            messages.error(request, _error_text(res))
        return redirect(back)

    if action == "edit":
        done = []
        owner = player.owner
        target = team or owner
        price = _price(request.POST.get("price", player.cost))
        if price is None:
            messages.error(request, "Costo non valido: scrivi un numero di FM (0 o più) e riprova.")
            return redirect(back)
        old_years = player.contract_years
        if target.id != owner.id or price != (player.cost or 0):
            res = services.assign_player(player.id, target.id, price=price, by_admin=True,
                                         note=f"Modificato dalla pagina Rose · {who}")
            if not res.get("ok"):
                messages.error(request, _error_text(res))
                return redirect(back)
            if target.id != owner.id:
                done.append(f"spostato da «{owner.display_name}» a «{target.display_name}» per {_fm(price)}")
            else:
                done.append(f"costo {_fm(price)}")
        if league.contracts_enabled and "contract" in request.POST:
            raw = request.POST.get("contract", "").strip()
            wanted = int(raw) if raw.isdigit() else None
            moved = target.id != owner.id
            # Uno spostamento è un acquisto: il contratto torna «da tirare»
            # (services.contracts.on_player_acquired), salvo durata scelta qui.
            if wanted != old_years or (moved and wanted is not None):
                res = services.set_contract(player.id, raw, note=f"Impostato dall'admin (Rose) · {who}")
                if not res.get("ok"):
                    messages.error(request, _error_text(res))
                    return redirect(back)
                done.append("contratto da tirare" if wanted is None else
                            f"contratto {wanted} ann{'o' if wanted == 1 else 'i'}")
        if done:
            messages.success(request, f"{player.name}: " + ", ".join(done) + ".")
        else:
            messages.info(request, f"{player.name}: nessuna modifica.")
        return redirect(back)

    messages.error(request, "Azione non riconosciuta: ricarica la pagina e riprova.")
    return redirect(back)
