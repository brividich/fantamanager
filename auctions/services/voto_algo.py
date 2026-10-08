"""Impostazioni del voto algoritmico e anteprima degli scostamenti.

Livelli dei valori, dal più generale:

1. ``ALGO_DEFAULTS`` (``auctions/voto_algoritmico.py``): il codice;
2. impostazioni di piattaforma (``AlgoSettingsVersion`` più recente), che il
   superuser cambia dalla pagina del Supervisor;
3. eventuali ritocchi di una lega (``Season.rules["algo"]``).

Chi calcola un voto legge le regole SOLO da ``rules_for_league`` (o
``platform_rules`` quando non c'è una lega): nessun altro punto mette insieme i
livelli.

L'anteprima confronta due insiemi di regole (quelle salvate e la bozza della
pagina) sullo stesso campione di righe di partita e dice cosa cambia: voti
diversi, medie e dispersione per ruolo, distribuzione, i giocatori che si
spostano di più e l'effetto di ogni singolo parametro toccato.
"""
import copy
import math
import random
from statistics import mean, pstdev

from ..models import AlgoSample, AlgoSettingsVersion
from ..voto_algoritmico import ALGO_DEFAULTS, ROLES, STAT_KEYS, calibrate, effective_algo_rules, player_vote

TABLES = ("perf_weights", "perf_baseline")      # per ruolo → per statistica
FLAT_TABLES = ("perf_malus",)                   # per statistica

# (chiave, etichetta, aiuto, passo, minimo, massimo). ``bool`` al posto del
# passo = interruttore.
FIELD_GROUPS = [
    ("Diritto al voto", [
        ("base", "Voto di partenza", "il voto di chi gioca senza fare nulla di notevole", 0.25, 4, 8),
        ("min_minutes", "Minuti per avere il voto", "sotto è senza voto, salvo un evento decisivo", 1, 0, 90),
        ("gk_vote_if_conceded", "Portiere subentrato che subisce prende il voto", "", bool, None, None),
    ]),
    ("Risultato della squadra", [
        ("win", "Vittoria", "", 0.05, -2, 2),
        ("draw", "Pareggio", "", 0.05, -2, 2),
        ("loss", "Sconfitta", "", 0.05, -2, 2),
        ("margin_step", "Ogni gol di scarto oltre il primo", "nel verso del risultato", 0.025, 0, 1),
        ("margin_cap", "Tetto dello scarto", "", 0.05, 0, 2),
        ("result_scaled_by_minutes", "Risultato in proporzione ai minuti giocati", "", bool, None, None),
    ]),
    ("Reparto (portieri e difensori)", [
        ("clean_sheet_P", "Porta inviolata — portiere", "", 0.05, -2, 3),
        ("clean_sheet_D", "Porta inviolata — difensore", "", 0.05, -2, 3),
        ("clean_sheet_min_minutes", "Minuti per la porta inviolata", "", 1, 0, 90),
        ("conceded_P", "Ogni gol subito — portiere", "", 0.025, -2, 2),
        ("conceded_D", "Ogni gol subito — difensore", "stimato dai minuti in campo", 0.025, -2, 2),
        ("conceded_cap", "Tetto dei gol subiti", "il malus non va oltre", 0.05, -5, 0),
    ]),
    ("Eventi", [
        ("goal", "Gol (centrocampista, attaccante)", "in pagella: il +3 lo aggiungono i bonus", 0.05, -3, 3),
        ("goal_D", "Gol del difensore", "", 0.05, -3, 3),
        ("goal_P", "Gol del portiere", "", 0.05, -3, 3),
        ("assist", "Assist", "", 0.05, -3, 3),
        ("yellow", "Ammonizione", "", 0.05, -3, 3),
        ("red", "Espulsione", "assorbe l'ammonizione", 0.05, -3, 3),
        ("own_goal", "Autogol", "", 0.05, -3, 3),
        ("pen_missed", "Rigore sbagliato", "", 0.05, -3, 3),
        ("pen_saved", "Rigore parato", "", 0.05, -3, 3),
        ("pen_won", "Rigore procurato", "", 0.05, -3, 3),
        ("pen_committed", "Rigore causato", "", 0.05, -3, 3),
    ]),
    ("Rendimento (statistiche avanzate)", [
        ("perf_min_minutes", "Minuti minimi per il calcolo ogni 90'", "chi gioca meno conta come se giocasse questi", 1, 1, 90),
        ("perf_cap", "Tetto del rendimento", "in su o in giù", 0.05, 0, 3),
    ]),
    ("Uscita e standardizzazione", [
        ("min_vote", "Voto minimo", "", 0.5, 0, 10),
        ("max_vote", "Voto massimo", "", 0.5, 0, 10),
        ("step", "Arrotondamento", "0,5 = pagella classica", 0.25, 0.25, 0.5),
        ("calib_scale", "Calibrazione: scala", "1 = nessuna", 0.0001, 0.2, 3),
        ("calib_shift", "Calibrazione: spostamento", "0 = nessuno", 0.0001, -2, 2),
    ]),
]

STAT_LABELS = {
    "saves": "Parate", "tackles": "Contrasti", "interceptions": "Intercetti",
    "blocks": "Tiri respinti", "duels_won": "Duelli vinti", "key_passes": "Passaggi chiave",
    "dribbles_won": "Dribbling riusciti", "shots_on": "Tiri in porta",
    "fouls": "Falli commessi", "dribbled_past": "Volte saltato",
}
ROLE_LABELS = {"P": "Portiere", "D": "Difensore", "C": "Centrocampista", "A": "Attaccante"}

WEIGHT_RANGE = (-1.0, 1.0)
BASELINE_RANGE = (0.0, 50.0)
PREVIEW_ROW_LIMIT = 6000
PER_FIELD_LIMIT = 40


# --------------------------------------------------------------------------
# Livelli dei valori
# --------------------------------------------------------------------------

def merge_overrides(base, extra):
    """``extra`` sopra ``base``; le tabelle per ruolo si fondono voce per voce."""
    out = {k: v for k, v in (base or {}).items() if k not in TABLES + FLAT_TABLES}
    for k, v in (extra or {}).items():
        if k not in TABLES + FLAT_TABLES:
            out[k] = v
    for table in TABLES:
        merged = {}
        for src in (base or {}, extra or {}):
            for role, stats in (src.get(table) or {}).items():
                merged.setdefault(role, {}).update(stats or {})
        if merged:
            out[table] = merged
    for table in FLAT_TABLES:
        merged = {**((base or {}).get(table) or {}), **((extra or {}).get(table) or {})}
        if merged:
            out[table] = merged
    return out


def active_version():
    return AlgoSettingsVersion.objects.order_by("-created_at", "-id").first()


def platform_overrides():
    v = active_version()
    return dict(v.rules) if v else {}


def platform_rules():
    return effective_algo_rules(platform_overrides())


def rules_for_league(league_overrides=None):
    return effective_algo_rules(merge_overrides(platform_overrides(), league_overrides))


def _same(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    try:
        return math.isclose(float(a), float(b), abs_tol=1e-9)
    except (TypeError, ValueError):
        return a == b


def diff_from_defaults(full):
    """Le sole voci di ``full`` diverse da ``ALGO_DEFAULTS`` (forma sparsa)."""
    out = {}
    for key, default in ALGO_DEFAULTS.items():
        if key in TABLES + FLAT_TABLES or key not in full:
            continue
        if not _same(full[key], default):
            out[key] = full[key]
    for table in TABLES:
        for role, stats in (full.get(table) or {}).items():
            for stat, value in (stats or {}).items():
                default = ALGO_DEFAULTS[table].get(role, {}).get(stat, 0)
                if not _same(value, default):
                    out.setdefault(table, {}).setdefault(role, {})[stat] = value
    for table in FLAT_TABLES:
        for stat, value in (full.get(table) or {}).items():
            if not _same(value, ALGO_DEFAULTS[table].get(stat, 0)):
                out.setdefault(table, {})[stat] = value
    return out


def _flatten(rules):
    """{percorso: valore} per confrontare due insiemi di regole."""
    flat = {}
    for k, v in rules.items():
        if k.startswith("_"):
            continue
        if k in TABLES:
            for role, stats in v.items():
                for stat, val in stats.items():
                    flat[(k, role, stat)] = val
        elif k in FLAT_TABLES:
            for stat, val in v.items():
                flat[(k, stat)] = val
        else:
            flat[(k,)] = v
    return flat


def _with_path(rules, path, value):
    out = copy.deepcopy(rules)
    target = out
    for key in path[:-1]:
        target = target.setdefault(key, {})
    target[path[-1]] = value
    return out


def path_label(path):
    if len(path) == 1:
        for _title, items in FIELD_GROUPS:
            for key, label, *_ in items:
                if key == path[0]:
                    return label
        return path[0]
    if path[0] == "perf_malus":
        return f"Malus rendimento: {STAT_LABELS.get(path[1], path[1])}"
    kind = "Peso" if path[0] == "perf_weights" else "Media del ruolo"
    return f"{kind} {ROLE_LABELS.get(path[1], path[1]).lower()}: {STAT_LABELS.get(path[2], path[2])}"


# --------------------------------------------------------------------------
# Validazione di ciò che arriva dalla pagina
# --------------------------------------------------------------------------

def clean_rules(raw):
    """Valida le regole complete inviate dalla pagina. Ritorna
    ``(regole_complete, errori)``; ``errori`` è {percorso_testo: messaggio}."""
    errors = {}
    raw = raw if isinstance(raw, dict) else {}
    full = effective_algo_rules({})
    full.pop("_effective", None)

    for _title, items in FIELD_GROUPS:
        for key, label, _hint, step, lo, hi in items:
            if key not in raw:
                continue
            value = raw[key]
            if step is bool:
                full[key] = bool(value)
                continue
            try:
                num = float(value)
            except (TypeError, ValueError):
                errors[key] = f"{label}: non è un numero"
                continue
            if not math.isfinite(num) or num < lo or num > hi:
                errors[key] = f"{label}: deve stare fra {_fmt(lo)} e {_fmt(hi)}"
                continue
            full[key] = int(num) if isinstance(step, int) and float(num).is_integer() else num

    if "step" not in errors and float(full["step"]) not in (0.25, 0.5):
        errors["step"] = "Arrotondamento: 0,25 o 0,5"
    if "min_vote" not in errors and "max_vote" not in errors and full["min_vote"] >= full["max_vote"]:
        errors["max_vote"] = "Il voto massimo deve superare il minimo"

    for table, (lo, hi) in (("perf_weights", WEIGHT_RANGE), ("perf_baseline", BASELINE_RANGE)):
        for role, stats in (raw.get(table) or {}).items():
            if role not in ROLES or not isinstance(stats, dict):
                continue
            for stat, value in stats.items():
                if stat not in STAT_KEYS:
                    continue
                key = f"{table}.{role}.{stat}"
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    errors[key] = f"{path_label((table, role, stat))}: non è un numero"
                    continue
                if not math.isfinite(num) or num < lo or num > hi:
                    errors[key] = f"{path_label((table, role, stat))}: fra {_fmt(lo)} e {_fmt(hi)}"
                    continue
                full[table].setdefault(role, {})[stat] = num
    for stat, value in (raw.get("perf_malus") or {}).items():
        if stat not in STAT_KEYS:
            continue
        key = f"perf_malus.{stat}"
        try:
            num = float(value)
        except (TypeError, ValueError):
            errors[key] = f"{path_label(('perf_malus', stat))}: non è un numero"
            continue
        if not math.isfinite(num) or num < WEIGHT_RANGE[0] or num > WEIGHT_RANGE[1]:
            errors[key] = f"{path_label(('perf_malus', stat))}: fra -1 e 1"
            continue
        full["perf_malus"][stat] = num
    return full, errors


def _fmt(x):
    return f"{x:g}".replace(".", ",")


# --------------------------------------------------------------------------
# Campioni
# --------------------------------------------------------------------------

SIM_KEY = "sim"
SIM_NAME = "Stagione simulata (deterministica)"


def simulated_rows(matches=100, seed=7):
    """Righe di partite plausibili per provare i parametri senza API.
    Sempre uguali (seme fisso): due anteprime sono confrontabili."""
    rnd = random.Random(seed)

    def count(rate):
        # conteggio "alla Poisson" con 20 prove
        return sum(rnd.random() < rate / 20 for _ in range(20))

    rates = {
        "goals": {"P": 0, "D": .05, "C": .12, "A": .35},
        "assists": {"P": 0, "D": .06, "C": .12, "A": .15},
        "shots_on": {"P": 0, "D": .2, "C": .5, "A": 1.1},
        "key_passes": {"P": 0, "D": .5, "C": 1.2, "A": 1.0},
        "tackles": {"P": 0, "D": 1.9, "C": 1.7, "A": .6},
        "interceptions": {"P": 0, "D": 1.3, "C": .8, "A": .2},
        "blocks": {"P": 0, "D": .5, "C": .15, "A": .05},
        "duels_won": {"P": .3, "D": 4.6, "C": 4.6, "A": 4.0},
        "dribbles_won": {"P": 0, "D": .3, "C": .7, "A": 1.1},
        "fouls": {"P": .05, "D": 1.0, "C": 1.2, "A": 1.1},
        "dribbled_past": {"P": 0, "D": .7, "C": .8, "A": .3},
    }
    lineup = [("P", 1), ("D", 4), ("C", 3), ("A", 3)]
    rows, n = [], 0
    for m in range(matches):
        home, away = f"Squadra {m % 20 + 1:02d}", f"Squadra {(m * 7 + 3) % 20 + 1:02d}"
        gh, ga = count(1.45), count(1.15)
        for team, gf, gc in ((home, gh, ga), (away, ga, gh)):
            for role, k in lineup:
                for _ in range(k):
                    n += 1
                    minutes = rnd.choice([90] * 6 + [85, 75, 65, 60, 20, 15])
                    if role == "P":
                        minutes = 90
                    share = minutes / 90
                    row = {
                        "name": f"Calciatore {n:04d}", "team": team, "role": role,
                        "minutes": minutes, "team_goals_for": gf, "team_goals_against": gc,
                        "goals_conceded": gc if role == "P" else 0,
                        "saves": count(2.6) if role == "P" else 0,
                        "yellow": rnd.random() < .14 * share, "red": rnd.random() < .01 * share,
                        "own_goals": int(rnd.random() < .01), "pen_missed": int(rnd.random() < .005),
                        "pen_saved": int(role == "P" and rnd.random() < .02),
                        "pen_won": int(rnd.random() < .01), "pen_committed": int(rnd.random() < .01),
                    }
                    for stat, by_role in rates.items():
                        row[stat] = count(by_role[role] * share)
                    rows.append(row)
    return rows


def samples_list():
    items = [{"key": SIM_KEY, "name": SIM_NAME, "rows": None, "simulated": True}]
    for s in AlgoSample.objects.order_by("-created_at", "-id"):
        items.append({"key": str(s.pk), "name": s.name, "rows": len(s.rows or []),
                      "simulated": False, "created_at": s.created_at, "id": s.pk})
    return items


def sample_rows(key):
    """(nome, righe) del campione; il simulato se la chiave non esiste."""
    if key and key != SIM_KEY:
        s = AlgoSample.objects.filter(pk=key if str(key).isdigit() else None).first()
        if s:
            return s.name, list(s.rows or [])[:PREVIEW_ROW_LIMIT]
    return SIM_NAME, simulated_rows()


def import_apifootball_round(round_number, user=None):
    """Salva come campione le righe vere di una giornata di Serie A.
    Il rating di API-Football diventa ``ref_vote``: un riferimento esterno per
    capire se i parametri avvicinano o allontanano il voto da un giudizio
    indipendente (non la verità)."""
    from ..providers import apifootball
    rows = apifootball.matchday_live_rows(int(round_number))
    keep = []
    for r in rows:
        if not r.get("minutes"):
            continue
        row = {k: v for k, v in r.items() if k not in ("vote", "api_id")}
        ref = r.get("vote")
        try:
            row["ref_vote"] = round(float(ref), 2) if ref not in (None, "", "-") else None
        except (TypeError, ValueError):
            row["ref_vote"] = None
        keep.append(row)
    if not keep:
        raise ValueError("nessuna partita giocata in questa giornata")
    return AlgoSample.objects.create(name=f"Serie A, giornata {int(round_number)}",
                                     source=AlgoSample.Source.APIFOOTBALL, rows=keep,
                                     created_by=user if getattr(user, "is_authenticated", False) else None)


# --------------------------------------------------------------------------
# Anteprima
# --------------------------------------------------------------------------

def _votes(rows, rules):
    rules = effective_algo_rules(rules) if not rules.get("_effective") else rules
    out = []
    for row in rows:
        res = player_vote(row, rules=rules)
        out.append((float(res["vote"]) if res["vote"] is not None else None, res))
    return out


def _stats(values):
    if not values:
        return {"n": 0, "mean": None, "sd": None}
    return {"n": len(values), "mean": round(mean(values), 3), "sd": round(pstdev(values), 3)}


def _pearson(xs, ys):
    if len(xs) < 3:
        return None
    mx, my = mean(xs), mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if not sx or not sy:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy), 3)


def _reference(rows, votes):
    pairs = [(v, float(r["ref_vote"])) for r, (v, _res) in zip(rows, votes)
             if v is not None and r.get("ref_vote") is not None]
    if len(pairs) < 3:
        return None
    return {
        "n": len(pairs),
        "mae": round(mean(abs(v - ref) for v, ref in pairs), 3),
        "corr": _pearson([p[0] for p in pairs], [p[1] for p in pairs]),
    }


def _compare(before, after):
    changed = 0
    deltas = []
    for (b, _rb), (a, _ra) in zip(before, after):
        if b != a:
            changed += 1
        if b is not None and a is not None:
            deltas.append(a - b)
    return changed, deltas


def preview(rows, current, draft):
    """Cosa cambia passando da ``current`` a ``draft`` sulle righe ``rows``.

    ``current`` e ``draft`` sono regole complete (o sparse: si completano coi
    default). Tutto ciò che torna è serializzabile in JSON."""
    current = effective_algo_rules(current)
    draft = effective_algo_rules(draft)
    rows = list(rows)[:PREVIEW_ROW_LIMIT]
    before, after = _votes(rows, current), _votes(rows, draft)
    changed, deltas = _compare(before, after)

    by_role = []
    for role in ROLES + ("Tutti",):
        idx = [i for i, r in enumerate(rows) if role == "Tutti" or (r.get("role") or "C").upper() == role]
        b = [before[i][0] for i in idx if before[i][0] is not None]
        a = [after[i][0] for i in idx if after[i][0] is not None]
        by_role.append({"role": role, "label": ROLE_LABELS.get(role, "Tutti i ruoli"),
                        "before": _stats(b), "after": _stats(a)})

    bins = sorted({v for v, _ in before + after if v is not None})
    hist = [{"vote": v,
             "before": sum(1 for x, _ in before if x == v),
             "after": sum(1 for x, _ in after if x == v)} for v in bins]

    movers = []
    for row, (b, rb), (a, ra) in zip(rows, before, after):
        if b == a:
            continue
        delta = (a - b) if (a is not None and b is not None) else None
        movers.append({
            "name": row.get("name") or "—", "team": row.get("team") or "",
            "role": (row.get("role") or "").upper(), "minutes": row.get("minutes"),
            "before": b, "after": a, "delta": delta,
            "breakdown_before": rb["breakdown"], "breakdown_after": ra["breakdown"],
        })
    movers.sort(key=lambda m: (-(abs(m["delta"]) if m["delta"] is not None else 9), m["name"]))

    # Effetto di ogni parametro toccato, da solo (sulle regole salvate).
    flat_cur, flat_new = _flatten(current), _flatten(draft)
    per_field = []
    for path in sorted(p for p in flat_new if not _same(flat_new[p], flat_cur.get(p, 0)))[:PER_FIELD_LIMIT]:
        only = _with_path(current, path, flat_new[path])
        alone = _votes(rows, only)
        ch, ds = _compare(before, alone)
        per_field.append({
            "path": ".".join(path), "label": path_label(path),
            "from": flat_cur.get(path, 0), "to": flat_new[path],
            "changed": ch, "mean_delta": round(mean(ds), 3) if ds else 0.0,
            "up": sum(1 for d in ds if d > 0), "down": sum(1 for d in ds if d < 0),
        })

    voted_before = sum(1 for v, _ in before if v is not None)
    voted_after = sum(1 for v, _ in after if v is not None)
    return {
        "rows": len(rows),
        "summary": {
            "changed": changed,
            "changed_pct": round(100 * changed / len(rows), 1) if rows else 0.0,
            "mean_delta": round(mean(deltas), 3) if deltas else 0.0,
            "mean_abs_delta": round(mean(abs(d) for d in deltas), 3) if deltas else 0.0,
            "up": sum(1 for d in deltas if d > 0),
            "down": sum(1 for d in deltas if d < 0),
            "voted_before": voted_before, "voted_after": voted_after,
        },
        "by_role": by_role,
        "histogram": hist,
        "movers": movers[:15],
        "per_field": per_field,
        "reference": {"before": _reference(rows, before), "after": _reference(rows, after)},
    }


def calibrate_on(rows, draft, target_mean=6.0, target_sd=0.6):
    """Scala e spostamento che portano i voti della bozza (senza la sua
    calibrazione) alla media e dispersione volute sul campione."""
    plain = dict(effective_algo_rules(draft))
    plain.update({"calib_scale": 1.0, "calib_shift": 0.0})
    raws = [res["raw"] for _v, res in _votes(list(rows)[:PREVIEW_ROW_LIMIT], plain) if res["raw"] is not None]
    return calibrate(raws, target_mean=target_mean, target_sd=target_sd, base=plain["base"])
