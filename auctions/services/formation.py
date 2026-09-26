"""Lineup builder and module layout logic (Classic & Mantra)."""
from .. import mantra
from ..models import Formation, Player

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


def _formation_saved(participant):
    """``(module, starter_ids)`` salvati, con ricaduta sul modulo predefinito.

    ``starter_ids`` e' posizionale: l'elemento i-esimo e' il giocatore nello
    slot i-esimo, ``None`` se lo slot e' vuoto. Le formazioni salvate prima del
    Mantra erano una lista compatta di soli id: restano leggibili perche' in
    Classic gli slot sono comunque in ordine di reparto, e un eventuale
    disallineamento si corregge da se' al primo salvataggio.
    """
    is_mantra = _is_mantra(participant)
    f = Formation.objects.filter(participant=participant).first()
    valid = mantra.MODULES if is_mantra else FORMATION_MODULES
    module = f.module if (f and f.module in valid) else _default_module(is_mantra)
    return module, (list(f.starter_ids or []) if f else [])


def formation_state(participant):
    """Stato della pagina Formazione: modulo, slot in campo, panchina.

    Ogni slot porta con se' i propri candidati, cosi' il menu a tendina non
    propone mai un giocatore che quello slot non puo' ospitare - in Mantra e' la
    differenza fra una pagina usabile e un regolamento da tenere aperto a
    fianco. Rose parziali o sovrabbondanti sono gestite: contano solo i
    giocatori posseduti e ogni slot ne prende al massimo uno.
    """
    is_mantra = _is_mantra(participant)
    module, starter_ids = _formation_saved(participant)
    slots = _module_slots(module, is_mantra)
    # P, D, C, A (not alphabetical by role code); within a role, by name as
    # before — bench order only matters between players of the same role.
    owned = sorted(
        Player.objects.filter(owner=participant, abroad_list=False),
        key=lambda p: ("PDCA".find(p.role) % 5, p.name),
    )
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

    bench = [p for p in owned if p.id not in assigned_ids]
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


def save_formation(participant, module, raw_ids):
    """Salva una formazione posizionale: ``raw_ids[i]`` e' lo slot i-esimo.

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

    formation, _ = Formation.objects.update_or_create(
        participant=participant, defaults={"module": module, "starter_ids": ordered}
    )
    return formation
