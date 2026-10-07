"""Lineup builder and module layout logic (Classic & Mantra)."""
from django.db import transaction
from django.utils import timezone

from .. import mantra
from ..models import Formation, Giornata, MatchdayFormation, Participant, Player

# Modulo Classic -> titolari per reparto (il portiere e' sempre 1).
FORMATION_MODULES = {
    "3-4-3": (1, 3, 4, 3),
    "3-5-2": (1, 3, 5, 2),
    "4-3-3": (1, 4, 3, 3),
    "4-4-2": (1, 4, 4, 2),
    "4-5-1": (1, 4, 5, 1),
    "5-3-2": (1, 5, 3, 2),
    "5-4-1": (1, 5, 4, 1),
}
DEFAULT_MODULE = "4-3-3"
_ROLE_LABELS_LONG = {"P": "Portiere", "D": "Difensori", "C": "Centrocampisti", "A": "Attaccanti"}
_CLASSIC_LINES = {"P": "Portiere", "D": "Difesa", "C": "Centrocampo", "A": "Attacco"}


def _is_mantra(participant):
    league = getattr(participant, "league", None)
    return league is not None and league.is_mantra


def _modules_for(is_mantra):
    return list(mantra.MODULES) if is_mantra else list(FORMATION_MODULES)


def _default_module(is_mantra):
    return mantra.DEFAULT_MODULE if is_mantra else DEFAULT_MODULE


def _module_slots(module, is_mantra):
    """Il modulo come lista ordinata di slot; ogni slot e' la tupla dei ruoli ammessi."""
    if is_mantra:
        return list(mantra.MODULES.get(module) or mantra.MODULES[mantra.DEFAULT_MODULE])
    counts = FORMATION_MODULES.get(module) or FORMATION_MODULES[DEFAULT_MODULE]
    slots = [("P",)]
    for role, n in zip("DCA", counts[1:]):
        slots += [(role,)] * n
    return slots


def _module_lines(module, is_mantra):
    """Righe da disegnare in campo: ``[(etichetta, [indici slot]), ...]``."""
    if is_mantra:
        return mantra.module_lines(module)
    counts = FORMATION_MODULES.get(module) or FORMATION_MODULES[DEFAULT_MODULE]
    lines, cursor = [], 0
    for role, n in zip("PDCA", counts):
        lines.append((_CLASSIC_LINES[role], list(range(cursor, cursor + n))))
        cursor += n
    return lines


def _slot_accepts(slot, player, is_mantra):
    """Questo giocatore puo' stare in questo slot?"""
    if not is_mantra:
        return player.role == slot[0]
    roles = player.role_list
    if roles:
        return mantra.slot_accepts(slot, roles)
    # Rosa Mantra con un listone importato senza colonna RM: senza questa
    # ricaduta la pagina sarebbe inutilizzabile, nessuno idoneo da nessuna
    # parte. Si ripiega sul ruolo classico dello slot: piu' permissivo del
    # dovuto, ma permette di schierare invece di bloccare.
    return any(mantra.ROLES.get(r, ("", "", ""))[2] == player.role for r in slot)


def _slot_role_class(slot, is_mantra):
    """Il ruolo classico che da' il colore allo slot (P/D/C/A)."""
    if not is_mantra:
        return slot[0]
    return mantra.ROLES.get(slot[0], ("", "", "A"))[2]


# Una giornata si schiera finche' non parte: poi la sua formazione resta com'era.
EDITABLE = (Giornata.Status.SCHEDULED, Giornata.Status.OPEN)


def is_editable(giornata):
    return giornata is None or giornata.status in EDITABLE


def target_giornata(league):
    """La giornata per cui si schiera adesso: la prima ancora aperta dopo
    l'ultima gia' partita (bloccata, live o calcolata) della stagione corrente.
    Le giornate rimaste indietro senza essere bloccate non contano: se e' partita
    la 5, la 3 non si schiera piu'. None se la lega non ha calendario."""
    if league is None:
        return None
    giornate = Giornata.objects.filter(season__league=league, season__is_current=True)
    started = giornate.exclude(status__in=EDITABLE).order_by("-number").values_list("number", flat=True).first()
    upcoming = giornate.filter(status__in=EDITABLE)
    if started is not None:
        upcoming = upcoming.filter(number__gt=started)
    return upcoming.order_by("number").first()


def _saved_lineup(participant, giornata=None):
    """La formazione salvata: quella della giornata se c'e', se no l'ultima
    salvata (che fa da modello per le giornate successive).

    ``frozen`` e' vero quando la giornata e' partita e ha la sua copia: vale
    com'era al blocco, anche se poi la rosa e' cambiata."""
    is_mantra = _is_mantra(participant)
    valid = mantra.MODULES if is_mantra else FORMATION_MODULES
    f = None
    if giornata is not None:
        f = MatchdayFormation.objects.filter(giornata=giornata, participant=participant).first()
    frozen = f is not None and not is_editable(giornata)
    if f is None:
        f = Formation.objects.filter(participant=participant).first()
    module = f.module if (f and f.module in valid) else _default_module(is_mantra)
    return {
        "module": module,
        "starter_ids": list(f.starter_ids or []) if f else [],
        "bench_ids": list(getattr(f, "bench_ids", None) or []) if f else [],
        "frozen": frozen,
    }


def _formation_saved(participant, giornata=None):
    """``(module, starter_ids)`` salvati, con ricaduta sul modulo predefinito."""
    saved = _saved_lineup(participant, giornata)
    return saved["module"], saved["starter_ids"]


def _ordered_bench(owned, starter_ids, bench_ids):
    """La panchina: prima nell'ordine scelto, poi gli altri per ruolo e nome."""
    taken = {pid for pid in starter_ids if pid}
    by_id = {p.id: p for p in owned}
    bench, seen = [], set(taken)
    for pid in bench_ids:
        p = by_id.get(pid)
        if p is not None and pid not in seen:
            bench.append(p)
            seen.add(pid)
    bench += [p for p in owned if p.id not in seen]
    return bench


def _owned(participant):
    # P, D, C, A (not alphabetical by role code); within a role, by name.
    return sorted(
        Player.objects.filter(owner=participant, abroad_list=False),
        key=lambda p: ("PDCA".find(p.role) % 5, p.name),
    )


def formation_state(participant, giornata=None):
    """Stato della pagina Formazione: modulo, slot in campo, panchina.
    Supporta anche la visualizzazione/modifica per una specifica giornata.
    """
    is_mantra = _is_mantra(participant)
    saved = _saved_lineup(participant, giornata)
    module, starter_ids = saved["module"], saved["starter_ids"]
    slots = _module_slots(module, is_mantra)
    owned = _owned(participant)
    by_id = {p.id: p for p in owned}

    # Assegnazione posizionale, con una rete per le formazioni salvate in
    # formato compatto (o rimaste da un altro modulo): un id che non sta nel suo
    # slot viene ricollocato nel primo slot libero che lo accetta.
    placed = [None] * len(slots)
    leftovers = []
    for i, pid in enumerate(starter_ids):
        p = by_id.get(pid) if pid else None
        if p is None:
            continue
        if i < len(slots) and placed[i] is None and _slot_accepts(slots[i], p, is_mantra):
            placed[i] = p
        else:
            leftovers.append(p)
    for p in leftovers:
        for i, slot in enumerate(slots):
            if placed[i] is None and _slot_accepts(slot, p, is_mantra):
                placed[i] = p
                break

    assigned_ids = {p.id for p in placed if p is not None}
    rows = []
    for line_label, idxs in _module_lines(module, is_mantra):
        row_slots = []
        for i in idxs:
            slot = slots[i]
            row_slots.append({
                "index": i,
                "key": mantra.slot_label(slot) if is_mantra else slot[0],
                "role_class": _slot_role_class(slot, is_mantra),
                "player": placed[i],
                "options": [p for p in owned if _slot_accepts(slot, p, is_mantra)],
            })
        rows.append({"label": line_label, "slots": row_slots})

    bench = _ordered_bench(owned, list(assigned_ids), saved["bench_ids"])
    return {
        "is_mantra": is_mantra,
        "module": module,
        "modules": _modules_for(is_mantra),
        "rows": rows,
        "bench": bench,
        "owned_count": len(owned),
        "starters_count": len(assigned_ids),
        "starters_target": len(slots),
    }


def _clean_lineup(participant, module, raw_ids, raw_bench_ids):
    """``(module, slot_ids, bench_ids)`` validati contro la rosa attuale.

    Scarta chi non e' in rosa, chi e' gia' schierato altrove e chi finisce in
    uno slot che non lo accetta - il controllo di compatibilita' sta qui e non
    solo nel browser, perche' una form si puo' rispedire a mano. Gli slot
    rifiutati restano vuoti invece di far fallire il salvataggio: una formazione
    parziale e' comunque lavoro da non perdere.
    """
    is_mantra = _is_mantra(participant)
    valid = mantra.MODULES if is_mantra else FORMATION_MODULES
    if module not in valid:
        module = _default_module(is_mantra)
    slots = _module_slots(module, is_mantra)
    owned = {p.id: p for p in Player.objects.filter(owner=participant, abroad_list=False)}

    ordered, seen = [None] * len(slots), set()
    for i, rid in enumerate(list(raw_ids)[:len(slots)]):
        try:
            pid = int(rid)
        except (TypeError, ValueError):
            continue
        p = owned.get(pid)
        if p is None or pid in seen or not _slot_accepts(slots[i], p, is_mantra):
            continue
        seen.add(pid)
        ordered[i] = pid

    bench = []
    for rid in raw_bench_ids or []:
        try:
            pid = int(rid)
        except (TypeError, ValueError):
            continue
        if pid in owned and pid not in seen:
            bench.append(pid)
            seen.add(pid)
    return module, ordered, bench


def save_formation(participant, module, raw_ids, raw_bench_ids=None, giornata=None):
    """Salva una formazione posizionale: ``raw_ids[i]`` e' lo slot i-esimo,
    ``raw_bench_ids`` l'ordine della panchina.

    Diventa l'ultima formazione salvata (il modello per le giornate a venire)
    e, se ``giornata`` e' ancora da giocare, la formazione di quella giornata.
    Una giornata gia' partita non si tocca: torna None.
    """
    if not is_editable(giornata):
        return None
    module, ordered, bench = _clean_lineup(participant, module, raw_ids, raw_bench_ids)
    formation, _ = Formation.objects.update_or_create(
        participant=participant, defaults={"module": module, "starter_ids": ordered, "bench_ids": bench}
    )
    if giornata is not None:
        MatchdayFormation.objects.update_or_create(
            giornata=giornata, participant=participant,
            defaults={"module": module, "starter_ids": ordered, "bench_ids": bench},
        )
    return formation


def lock_formations(giornata):
    """Blocca le formazioni di una giornata che parte: ogni squadra attiva
    della lega ne ha una copia propria, presa com'e' adesso (giocatori ceduti
    tolti, panchina completa nell'ordine scelto), e da li' non cambia piu' -
    nemmeno se la giornata si ricalcola settimane dopo con un'altra rosa.

    Su una giornata gia' partita copia solo per chi non l'aveva ancora (dati
    di prima di questo blocco). Ritorna quante copie ha scritto.
    """
    league = giornata.season.league if giornata.season_id else None
    teams = Participant.objects.filter(is_active=True)
    teams = teams.filter(league=league) if league is not None else teams.filter(league__isnull=True)
    editable = is_editable(giornata)
    written = 0
    with transaction.atomic():
        have = set(MatchdayFormation.objects.filter(giornata=giornata).values_list("participant_id", flat=True))
        for team in teams:
            if not editable and team.id in have:
                continue
            saved = _saved_lineup(team, giornata if team.id in have else None)
            owned = _owned(team)
            owned_ids = {p.id for p in owned}
            slots = [pid if pid in owned_ids else None for pid in saved["starter_ids"]]
            bench = [p.id for p in _ordered_bench(owned, slots, saved["bench_ids"])]
            MatchdayFormation.objects.update_or_create(
                giornata=giornata, participant=team,
                defaults={"module": saved["module"], "starter_ids": slots, "bench_ids": bench},
            )
            written += 1
        if editable:
            giornata.status = Giornata.Status.LOCKED
            giornata.locked_at = timezone.now()
            giornata.save(update_fields=["status", "locked_at"])
    return written
