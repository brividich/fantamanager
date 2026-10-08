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

from .. import voto_taratura
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


# --------------------------------------------------------------------------
# Fonte del voto base di una lega
# --------------------------------------------------------------------------

# (valore, etichetta, spiegazione): ``Season.rules["vote_source"]``. I testi
# sono gli stessi in console e app (``_scoring_rules.html``).
VOTE_SOURCES = [
    ("rating", "Rating API-Football, poi file ufficiale",
     "In diretta il rating di API-Football arrotondato al mezzo punto; "
     "il file dei voti caricato dalla lega lo sostituisce."),
    ("algoritmico", "Voto algoritmico definitivo",
     "Il voto calcolato da FantaManager dai fatti della partita, in diretta e a fine giornata. "
     "Un file dei voti lo sostituisce solo se lo importi apposta."),
    ("algoritmico_provvisorio", "Voto algoritmico, poi file ufficiale",
     "In diretta il voto calcolato da FantaManager; il file dei voti caricato dalla lega lo sostituisce."),
]
DEFAULT_VOTE_SOURCE = "rating"
ALGO_SOURCES = ("algoritmico", "algoritmico_provvisorio")
# ``live_source`` di un PlayerPerformance con il voto dell'algoritmo.
ALGO_LIVE_SOURCE = "algoritmico"

# Parametri che una lega può ritoccare sopra quelli di piattaforma; tabelle di
# pesi e medie restano solo di piattaforma.
LEAGUE_FIELDS = ("win", "loss", "clean_sheet_P", "clean_sheet_D", "goal", "assist", "min_minutes")


def vote_source_for(season):
    """La fonte del voto base della lega di ``season`` (``rating`` se non scelta)."""
    value = ((season.rules or {}) if season is not None else {}).get("vote_source")
    return value if value in {v for v, *_ in VOTE_SOURCES} else DEFAULT_VOTE_SOURCE


def is_algo_source(source):
    return source in ALGO_SOURCES


def algo_rules_for(season):
    """Regole dell'algoritmo con cui gioca la lega di ``season``: piattaforma
    più i ritocchi della lega (``Season.rules["algo"]``)."""
    return rules_for_league(((season.rules or {}) if season is not None else {}).get("algo"))


def field_meta(key):
    """(etichetta, aiuto, passo, minimo, massimo) di un parametro, come nella
    pagina del Supervisor."""
    for _title, items in FIELD_GROUPS:
        for k, label, hint, step, lo, hi in items:
            if k == key:
                return label, hint, step, lo, hi
    raise KeyError(key)


def league_algo_fields(season):
    """I parametri ritoccabili dalla lega, per il form: valore di piattaforma
    accanto e l'eventuale ritocco della lega."""
    platform = platform_rules()
    own = dict(((season.rules or {}) if season is not None else {}).get("algo") or {})
    fields = []
    for key in LEAGUE_FIELDS:
        label, hint, step, lo, hi = field_meta(key)
        fields.append({
            "key": key, "label": label, "hint": hint, "step": step, "min": lo, "max": hi,
            "platform": _fmt(platform[key]),
            "value": _fmt(own[key]) if key in own else "",
            "overridden": key in own,
        })
    return fields


def clean_league_algo(existing, values):
    """Valida i ritocchi della lega. ``existing`` = ``Season.rules["algo"]``
    attuale; ``values`` = {chiave: testo, o None per «usa quello della
    piattaforma»}. Ritorna ``(nuovi_ritocchi, errori)``: le chiavi fuori da
    ``LEAGUE_FIELDS`` (es. la calibrazione del comando) restano com'erano."""
    overrides = {k: v for k, v in dict(existing or {}).items() if k not in LEAGUE_FIELDS}
    for key in LEAGUE_FIELDS:
        raw = values.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        overrides[key] = str(raw).strip().replace(",", ".")
    candidate = {k: v for k, v in platform_rules().items() if not k.startswith("_")}
    candidate = merge_overrides(candidate, overrides)
    full, errors = clean_rules(candidate)
    for key in LEAGUE_FIELDS:
        if key in overrides and key not in errors:
            overrides[key] = full[key]
    return overrides, errors


# --------------------------------------------------------------------------
# Voto di una partita salvato con il suo dettaglio
# --------------------------------------------------------------------------

# La riga minima che basta per rigenerare il voto senza richiamare l'API.
INPUT_KEYS = ("minutes", "team_goals_for", "team_goals_against", "goals", "assists",
              "own_goals", "pen_scored", "pen_missed", "pen_saved", "pen_won",
              "pen_committed", "goals_conceded", "yellow", "red") + STAT_KEYS


def _plain_value(v):
    if isinstance(v, bool) or v is None:
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def input_row(row):
    """Le sole voci di ``row`` che servono all'algoritmo, in forma JSON."""
    return {k: _plain_value(row.get(k)) for k in INPUT_KEYS if k in row}


def algo_vote(row, role, rules):
    """``(voto, vote_detail)`` del voto algoritmico di una riga di partita."""
    inp = input_row(row)
    res = player_vote(inp, role, rules)
    return res["vote"], {"source": ALGO_LIVE_SOURCE, "raw": res["raw"],
                         "breakdown": res["breakdown"], "input": inp}


def regenerate_votes(giornata, rules):
    """Rifà il voto algoritmico delle performance di ``giornata`` dalla riga
    salvata in ``vote_detail``, senza chiamate all'API. Un voto importato da
    file resta del file. Ritorna ``(rigenerati, senza_riga)``: chi non ha la
    riga d'ingresso tiene il voto che aveva."""
    done = missing = 0
    for perf in giornata.performances.select_related("player"):
        if perf.live_source == "official_upload":
            continue
        inp = (perf.vote_detail or {}).get("input")
        if not inp:
            missing += 1
            continue
        perf.vote, perf.vote_detail = algo_vote(inp, perf.player.role, rules)
        perf.save(update_fields=["vote", "vote_detail"])
        done += 1
    return done, missing


WHY_LABELS = (("base", "Base"), ("risultato", "Risultato"), ("reparto", "Reparto"),
              ("eventi", "Eventi"), ("rendimento", "Rendimento"), ("calibrazione", "Calibrazione"))


def vote_why(detail, vote):
    """«Perché questo voto»: le voci del calcolo, pronte per il pannello.
    None se il voto non viene dall'algoritmo (o è un senza voto)."""
    if not detail or detail.get("source") != ALGO_LIVE_SOURCE or detail.get("raw") is None or vote is None:
        return None
    breakdown = detail.get("breakdown") or {}
    parts = [{"key": k, "label": label, "value": round(float(breakdown[k]), 2)}
             for k, label in WHY_LABELS if k in breakdown]
    total = sum(float(breakdown[k]) for k, _ in WHY_LABELS if k in breakdown)
    rounding = round(float(vote) - total, 2)
    if rounding:
        parts.append({"key": "arrotondamento", "label": "Arrotondamento e limiti", "value": rounding})
    return {"vote": float(vote), "parts": parts}


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
# Voti di riferimento (solo taratura)
# --------------------------------------------------------------------------
#
# Il superuser carica a mano il file dei voti di una fonte esterna per una
# giornata già importata come campione. Servono solo a confrontare e tarare
# l'algoritmo su questa pagina: non entrano mai nei voti delle giornate.

REF_MAX_BYTES = 5 * 1024 * 1024
REF_MAX_ROWS = 2000
REF_EXTENSIONS = (".xlsx", ".xls", ".csv")
REPORT_LIMIT = 30
REF_MEDIA = "media"


def _ref_role(role):
    role = (role or "").strip().upper()
    return role if role in ROLES else ""


def parse_reference_file(uploaded_file):
    """Righe normalizzate ``{name, team, role, vote}`` del file caricato.
    ``vote`` è una stringa («6.5») o None (senza voto). ValueError se il file
    non va bene (estensione, dimensione, righe, contenuto)."""
    from .voti import parse_voti_file
    name = (getattr(uploaded_file, "name", "") or "").strip()
    if not name.lower().endswith(REF_EXTENSIONS):
        raise ValueError("il file deve essere .xlsx, .xls o .csv")
    size = getattr(uploaded_file, "size", None)
    if size is not None and size > REF_MAX_BYTES:
        raise ValueError("il file supera 5 MB")
    content = uploaded_file.read(REF_MAX_BYTES + 1)
    if len(content) > REF_MAX_BYTES:
        raise ValueError("il file supera 5 MB")
    try:
        parsed = parse_voti_file(content, name)
    except Exception as exc:          # file danneggiato o non un foglio di calcolo
        raise ValueError(f"il file non si legge ({exc.__class__.__name__})") from exc
    if not parsed:
        raise ValueError("nessuna riga di voti riconosciuta (servono almeno le colonne Nome e Voto)")
    if len(parsed) > REF_MAX_ROWS:
        raise ValueError(f"troppe righe: {len(parsed)}, al massimo {REF_MAX_ROWS}")
    return [{"name": r["name"], "team": r.get("team") or "", "role": _ref_role(r.get("role")),
             "vote": None if r.get("vote") is None else str(r["vote"])} for r in parsed]


def match_reference(sample_rows, ref_rows):
    """Abbina le righe del file a quelle del campione: stessa squadra
    (``apifootball.same_club``), poi il nome col punteggio di
    ``footballers.name_match_score``. Pari merito = ambiguo; una riga del
    campione si abbina una volta sola. Ritorna il dict salvato in
    ``AlgoReference.matched``."""
    from ..providers.apifootball import same_club
    from .footballers import name_match_score

    by_team = {}
    for i, sr in enumerate(sample_rows):
        by_team.setdefault(sr.get("team") or "", []).append(i)
    team_cache = {}

    def candidates(team):
        if not team:
            return range(len(sample_rows))
        if team not in team_cache:
            team_cache[team] = [i for club, idx in by_team.items() if same_club(club, team) for i in idx]
        return team_cache[team]

    used, votes = {}, {}
    unmatched_file, ambiguous = [], []
    for fr in ref_rows:
        scored = []
        for i in candidates(fr["team"]):
            sr = sample_rows[i]
            score = name_match_score(fr["name"], fr["role"], sr.get("name") or "", _ref_role(sr.get("role")))
            if score is not None:
                scored.append((score, i))
        scored.sort(key=lambda si: -si[0])
        who = {"name": fr["name"], "team": fr["team"]}
        if not scored:
            unmatched_file.append(who)
            continue
        if len(scored) > 1 and scored[1][0] == scored[0][0]:
            ties = [sample_rows[i].get("name") or "" for sc, i in scored if sc == scored[0][0]]
            ambiguous.append({**who, "candidates": ties[:5]})
            continue
        idx = scored[0][1]
        if idx in used:
            unmatched_file.append({**who, "reason": f"«{sample_rows[idx].get('name')}» già abbinato a «{used[idx]}»"})
            continue
        used[idx] = fr["name"]
        votes[str(idx)] = fr["vote"]
    unmatched_sample = [{"name": sr.get("name") or "", "team": sr.get("team") or "", "minutes": sr.get("minutes")}
                        for i, sr in enumerate(sample_rows)
                        if i not in used and (sr.get("minutes") or 0) > 0]
    return {"votes": votes, "unmatched_file": unmatched_file,
            "unmatched_sample": unmatched_sample, "ambiguous": ambiguous}


def import_reference(sample, uploaded_file, label, user=None):
    """Crea l'``AlgoReference`` di ``sample`` dal file caricato a mano."""
    from ..models import AlgoReference
    label = (label or "").strip()[:80]
    if not label:
        raise ValueError("scrivi un'etichetta per la fonte (es. il nome del giornale)")
    rows = parse_reference_file(uploaded_file)
    matched = match_reference(list(sample.rows or []), rows)
    return AlgoReference.objects.create(
        sample=sample, label=label, filename=(getattr(uploaded_file, "name", "") or "")[:200],
        rows=rows, matched=matched, created_by=user if getattr(user, "is_authenticated", False) else None)


def reference_summary(ref):
    """Conteggi e prime voci dei report di abbinamento, per la pagina."""
    m = ref.matched or {}
    return {
        "id": ref.pk, "label": ref.label, "filename": ref.filename, "created_at": ref.created_at,
        "sample_id": ref.sample_id, "sample_name": ref.sample.name,
        "matched": len(m.get("votes") or {}), "rows": len(ref.rows or []),
        "unmatched_file": len(m.get("unmatched_file") or []),
        "unmatched_sample": len(m.get("unmatched_sample") or []),
        "ambiguous": len(m.get("ambiguous") or []),
        "unmatched_file_list": (m.get("unmatched_file") or [])[:REPORT_LIMIT],
        "unmatched_sample_list": (m.get("unmatched_sample") or [])[:REPORT_LIMIT],
        "ambiguous_list": (m.get("ambiguous") or [])[:REPORT_LIMIT],
    }


def comparison_rows(sample_ids, reference_ids=None):
    """Righe di più campioni in fila e i loro riferimenti con gli indici
    riportati sulla fila unica. ``reference_ids`` None = tutti quelli dei
    campioni. Ogni riga riceve ``_g`` = id del campione (la giornata), per la
    validazione della taratura. Ritorna ``(nomi, righe, riferimenti)``."""
    from ..models import AlgoReference
    ids = list(dict.fromkeys(int(x) for x in sample_ids or [] if str(x).isdigit()))
    samples = {s.pk: s for s in AlgoSample.objects.filter(pk__in=ids)}
    refs_qs = AlgoReference.objects.filter(sample_id__in=samples.keys())
    if reference_ids is not None:
        refs_qs = refs_qs.filter(pk__in=[int(x) for x in reference_ids if str(x).isdigit()])
    by_sample = {}
    for ref in refs_qs.order_by("id"):
        by_sample.setdefault(ref.sample_id, []).append(ref)
    names, rows, refs = [], [], []
    for sid in ids:
        sample = samples.get(sid)
        if sample is None:
            continue
        offset = len(rows)
        sample_rows = list(sample.rows or [])[:PREVIEW_ROW_LIMIT - offset]
        if not sample_rows:
            continue
        names.append(sample.name)
        for r in sample_rows:
            rows.append({**r, "_g": sid})
        for ref in by_sample.get(sid, []):
            votes = {offset + int(k): v for k, v in ((ref.matched or {}).get("votes") or {}).items()
                     if str(k).isdigit() and int(k) < len(sample_rows)}
            refs.append({"id": ref.pk, "label": ref.label, "sample_id": sid, "matched": votes})
    return names, rows, refs


def _ref_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def reference_votes(refs, mode=REF_MEDIA):
    """{indice: voto|None} del riferimento scelto: le fonti con l'etichetta
    ``mode`` oppure, con ``"media"``, la media dei riferimenti disponibili per
    giocatore (s.v. solo se tutte le fonti lo danno senza voto)."""
    key = (mode or REF_MEDIA).strip().lower()
    chosen = refs if key == REF_MEDIA else [r for r in refs if r["label"].strip().lower() == key]
    acc = {}
    for ref in chosen:
        for idx, v in ref["matched"].items():
            acc.setdefault(idx, []).append(_ref_float(v))
    out = {}
    for idx, vals in acc.items():
        nums = [v for v in vals if v is not None]
        out[idx] = sum(nums) / len(nums) if nums else None
    return out


def reference_labels(refs):
    seen = {}
    for r in refs:
        seen.setdefault(r["label"].strip().lower(), r["label"].strip())
    return list(seen.values())


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


# --- confronto con i voti di riferimento ------------------------------------

GRID = [4.0 + 0.5 * i for i in range(10)]          # 4 … 8,5
GROUP_MIN = 10
DISAGREE_LIMIT = 15


def _err_metrics(pairs):
    """Errori dell'algoritmo rispetto al riferimento su coppie (alg, rif)."""
    n = len(pairs)
    if not n:
        return None
    diffs = [a - r for a, r in pairs]
    return {
        "n": n,
        "mae": round(mean(abs(d) for d in diffs), 3),
        "rmse": round(math.sqrt(mean(d * d for d in diffs)), 3),
        "bias": round(mean(diffs), 3),
        "corr": _pearson([a for a, _ in pairs], [r for _, r in pairs]),
        "exact_pct": round(100 * sum(1 for d in diffs if abs(d) < 1e-9) / n, 1),
        "within_pct": round(100 * sum(1 for d in diffs if abs(d) <= 0.5 + 1e-9) / n, 1),
    }


def _small(pairs):
    if not pairs:
        return None
    diffs = [a - r for a, r in pairs]
    return {"n": len(pairs), "mae": round(mean(abs(d) for d in diffs), 3), "bias": round(mean(diffs), 3)}


def _row_role(row):
    role = (row.get("role") or "C").upper()
    return role if role in ROLES else "C"


def _intv(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def error_groups(row):
    """I gruppi «dove sbagliamo» di una riga: [(dimensione, gruppo)]."""
    out = []
    gf, ga = row.get("team_goals_for"), row.get("team_goals_against")
    if gf is not None and ga is not None:
        d = _intv(gf) - _intv(ga)
        out.append(("Risultato della squadra", "Vittoria" if d > 0 else "Sconfitta" if d < 0 else "Pareggio"))
    m = _intv(row.get("minutes"))
    out.append(("Minuti giocati", "meno di 30'" if m < 30 else "30'–59'" if m < 60 else "60'–89'" if m < 90 else "90'"))
    goals, assists = _intv(row.get("goals")), _intv(row.get("assists"))
    card = bool(row.get("yellow") or row.get("red"))
    if goals:
        out.append(("Eventi", "con gol"))
    if assists:
        out.append(("Eventi", "con assist"))
    if card:
        out.append(("Eventi", "con cartellino"))
    if not (goals or assists or card):
        out.append(("Eventi", "nessun evento"))
    role = _row_role(row)
    if role == "P":
        out.append(("Porta inviolata (P, D)", "sì" if _intv(row.get("goals_conceded")) == 0 else "no"))
    elif role == "D" and ga is not None:
        out.append(("Porta inviolata (P, D)", "sì" if _intv(ga) == 0 else "no"))
    return out


def _grid_index(v):
    return max(0, min(len(GRID) - 1, int(round((v - GRID[0]) / 0.5))))


def compare_reference(rows, before, after, ref):
    """Voto algoritmico (in uso e bozza) contro il riferimento ``ref``
    ({indice: voto|None}) sui soli giocatori abbinati. Serializzabile JSON."""
    idx = sorted(i for i in ref if 0 <= i < len(rows))
    out = {"matched": len(idx)}
    both = {}
    for tag, votes in (("before", before), ("after", after)):
        pairs = [(votes[i][0], ref[i]) for i in idx if votes[i][0] is not None and ref[i] is not None]
        both[tag] = pairs
        out[tag] = _err_metrics(pairs)
        counts = {"both": 0, "alg_only": 0, "ref_only": 0, "none": 0}
        for i in idx:
            a, r = votes[i][0] is not None, ref[i] is not None
            counts["both" if a and r else "alg_only" if a else "ref_only" if r else "none"] += 1
        out[f"sv_{tag}"] = counts

    out["by_role"] = []
    for role in ROLES:
        sel = [i for i in idx if _row_role(rows[i]) == role and ref[i] is not None]
        b = [(before[i][0], ref[i]) for i in sel if before[i][0] is not None]
        a = [(after[i][0], ref[i]) for i in sel if after[i][0] is not None]
        if b or a:
            out["by_role"].append({"role": role, "label": ROLE_LABELS[role], "before": _small(b), "after": _small(a)})

    groups = {}
    for i in idx:
        if ref[i] is None:
            continue
        for key in error_groups(rows[i]):
            g = groups.setdefault(key, {"before": [], "after": []})
            if before[i][0] is not None:
                g["before"].append((before[i][0], ref[i]))
            if after[i][0] is not None:
                g["after"].append((after[i][0], ref[i]))
    out["groups"] = sorted(
        ({"dim": dim, "group": name, "before": _small(g["before"]), "after": _small(g["after"])}
         for (dim, name), g in groups.items() if len(g["after"]) >= GROUP_MIN),
        key=lambda g: -abs(g["after"]["bias"]))

    out["grid"] = GRID
    for tag in ("before", "after"):
        matrix = [[0] * len(GRID) for _ in GRID]
        for a, r in both[tag]:
            matrix[_grid_index(a)][_grid_index(r)] += 1
        out[f"heatmap_{tag}"] = matrix

    worst = sorted(((abs(after[i][0] - ref[i]), i) for i in idx
                    if after[i][0] is not None and ref[i] is not None), key=lambda di: -di[0])
    out["disagreements"] = [{
        "name": rows[i].get("name") or "—", "team": rows[i].get("team") or "",
        "role": _row_role(rows[i]), "minutes": rows[i].get("minutes"),
        "before": before[i][0], "after": after[i][0], "ref": round(ref[i], 2),
        "breakdown_before": before[i][1]["breakdown"], "breakdown_after": after[i][1]["breakdown"],
    } for _d, i in worst[:DISAGREE_LIMIT]]
    return out


def preview(rows, current, draft, references=None, reference_mode=REF_MEDIA):
    """Cosa cambia passando da ``current`` a ``draft`` sulle righe ``rows``.

    ``current`` e ``draft`` sono regole complete (o sparse: si completano coi
    default). ``references`` = [{id, label, matched: {indice: voto|None}}]
    (``comparison_rows``) aggiunge il confronto con i voti di riferimento,
    della fonte ``reference_mode`` (etichetta) o la loro media. Tutto ciò che
    torna è serializzabile in JSON."""
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
    feats = None
    for path in sorted(p for p in flat_new if not _same(flat_new[p], flat_cur.get(p, 0)))[:PER_FIELD_LIMIT]:
        only = _with_path(current, path, flat_new[path])
        dotted = ".".join(path)
        if voto_taratura.group_of(dotted) and dotted != "base":
            # Parametro lineare: cambia solo i voti delle righe in cui conta
            # (o con un tetto attivo); le altre restano come prima.
            if feats is None:
                feats = [voto_taratura._analyze(row, _row_role(row), current) if b[0] is not None else None
                         for row, b in zip(rows, before)]
            alone = list(before)
            for i, f in enumerate(feats):
                if f is not None and (dotted in f[0] or f[1]):
                    res = player_vote(rows[i], rules=only)
                    alone[i] = (float(res["vote"]) if res["vote"] is not None else None, res)
        else:
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
        "references": _references_block(rows, before, after, references, reference_mode),
    }


def _references_block(rows, before, after, references, mode):
    if not references:
        return None
    labels = reference_labels(references)
    if (mode or REF_MEDIA).strip().lower() not in {REF_MEDIA} | {l.lower() for l in labels}:
        mode = REF_MEDIA
    ref = reference_votes(references, mode)
    return {"sources": labels, "mode": mode, **compare_reference(rows, before, after, ref)}


def calibrate_on(rows, draft, target_mean=6.0, target_sd=0.6):
    """Scala e spostamento che portano i voti della bozza (senza la sua
    calibrazione) alla media e dispersione volute sul campione."""
    plain = dict(effective_algo_rules(draft))
    plain.update({"calib_scale": 1.0, "calib_shift": 0.0})
    raws = [res["raw"] for _v, res in _votes(list(rows)[:PREVIEW_ROW_LIMIT], plain) if res["raw"] is not None]
    return calibrate(raws, target_mean=target_mean, target_sd=target_sd, base=plain["base"])


# --------------------------------------------------------------------------
# Taratura automatica (auctions/voto_taratura.py)
# --------------------------------------------------------------------------

def tuning_ranges(rules):
    """Range ammessi di ogni parametro tarabile: quelli dei campi della pagina."""
    ranges = {}
    for _title, items in FIELD_GROUPS:
        for key, _label, _hint, step, lo, hi in items:
            if step is not bool:
                ranges[key] = (lo, hi)
    for role, stats in rules["perf_weights"].items():
        for stat in stats:
            ranges[f"perf_weights.{role}.{stat}"] = WEIGHT_RANGE
    for stat in rules["perf_malus"]:
        ranges[f"perf_malus.{stat}"] = WEIGHT_RANGE
    return ranges


def tune_on(rows, references, reference_mode, draft, groups=None, lam=None):
    """La proposta di ``voto_taratura.tune`` con le etichette della pagina."""
    from .. import voto_taratura
    if not references:
        raise ValueError("servono voti di riferimento abbinati ai campioni scelti")
    rules = effective_algo_rules(draft)
    ref = reference_votes(references, reference_mode)
    result = voto_taratura.tune(rows, ref, rules, groups=groups,
                                lam=voto_taratura.PRUDENZA["media"] if lam is None else lam,
                                ranges=tuning_ranges(rules))
    for p in result["params"]:
        p["label"] = path_label(tuple(p["path"].split(".")))
    result["group_labels"] = dict(voto_taratura.GROUPS)
    return result
