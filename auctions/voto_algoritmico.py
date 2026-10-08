"""Voto algoritmico — un voto in pagella calcolato dai fatti della partita.

Perché esiste: i voti dei quotidiani (Gazzetta, Corriere dello Sport, …) e i
rating delle app (SofaScore, Diretta, …) sono contenuti di terzi che non si
possono ridistribuire senza licenza. Gli *eventi* di una partita (chi ha
giocato e quanto, gol, assist, cartellini, risultato) sono fatti. Questo modulo
li trasforma in un voto con un algoritmo nostro, trasparente e configurabile
per lega, disponibile appena finisce la partita.

Il voto NON sostituisce i bonus/malus: è la "pagella" su cui ``scoring.py``
somma poi gol +3, assist +1, ecc. Per questo il peso degli eventi qui è
piccolo (un gol alza il voto di mezzo punto, non di tre).

Come ``scoring.py``: puro, senza modelli né DB, tutto guidato da un dict di
regole (``ALGO_DEFAULTS`` + quelle della lega in ``Season.rules["algo"]``).

Calcolo, per un giocatore che ha diritto al voto:

    grezzo = base
           + risultato della squadra   (vittoria/sconfitta, scarto)
           + reparto                   (porta inviolata, gol subiti in campo)
           + eventi                    (gol, assist, cartellini, rigori, autogol)
           + rendimento                (statistiche avanzate per 90', con tetto)
    voto   = arrotonda_al_mezzo( limita( calibra(grezzo) ) )

Ogni voce finisce in ``breakdown``: l'app può mostrare "perché 6,5".

Le statistiche avanzate (tiri in porta, passaggi chiave, contrasti, parate…)
sono facoltative: se la fonte non le dà, la voce "rendimento" vale 0 e il voto
dipende solo da risultato, reparto ed eventi. Così funziona sia con una fonte
gratuita povera sia con API-Football.
"""
from decimal import ROUND_HALF_UP, Decimal
from statistics import mean, pstdev

ROLES = ("P", "D", "C", "A")

ALGO_DEFAULTS = {
    # --- diritto al voto -------------------------------------------------
    "base": 6.0,
    # Sotto questi minuti è "senza voto" (s.v.), salvo un evento decisivo.
    "min_minutes": 25,
    # Il portiere che subentra e subisce gol prende il voto comunque.
    "gk_vote_if_conceded": True,

    # --- risultato della squadra ----------------------------------------
    "win": 0.25,
    "draw": 0.0,
    "loss": -0.25,
    # Per ogni gol di scarto oltre il primo, nel verso del risultato.
    "margin_step": 0.125,
    "margin_cap": 0.25,
    # Il risultato pesa in proporzione ai minuti giocati (un subentrato al
    # 80' non "vince" quanto chi ha giocato 90').
    "result_scaled_by_minutes": True,

    # --- reparto ---------------------------------------------------------
    # Porta inviolata: solo con almeno ``clean_sheet_min_minutes`` in campo.
    "clean_sheet_P": 0.5,
    "clean_sheet_D": 0.25,
    "clean_sheet_min_minutes": 60,
    # Gol subiti mentre era in campo (per il portiere il dato è esatto, per i
    # difensori si stima dai gol della squadra pesati sui minuti).
    "conceded_P": -0.25,
    "conceded_D": -0.125,
    "conceded_cap": -1.0,

    # --- eventi (piccoli: i bonus/malus veri li aggiunge scoring.py) ------
    "goal": 0.5,
    "goal_D": 0.75,          # il gol di un difensore pesa di più in pagella
    "goal_P": 1.0,
    "assist": 0.25,
    "yellow": -0.25,
    "red": -1.0,
    "own_goal": -0.5,
    "pen_missed": -0.5,
    "pen_saved": 0.5,
    "pen_won": 0.25,         # rigore procurato
    "pen_committed": -0.5,   # rigore causato

    # --- rendimento (statistiche avanzate, per 90') ------------------------
    # peso per ogni unità della statistica ogni 90 minuti, per ruolo.
    # Chiavi delle statistiche: vedi ``STAT_KEYS``.
    "perf_weights": {
        "P": {"saves": 0.15},
        "D": {"tackles": 0.06, "interceptions": 0.08, "blocks": 0.06,
              "duels_won": 0.03, "key_passes": 0.08},
        "C": {"key_passes": 0.12, "tackles": 0.05, "interceptions": 0.05,
              "dribbles_won": 0.06, "shots_on": 0.08, "duels_won": 0.02},
        "A": {"shots_on": 0.12, "key_passes": 0.1, "dribbles_won": 0.06,
              "duels_won": 0.02},
    },
    # Malus di rendimento validi per tutti i ruoli (per 90').
    "perf_malus": {"fouls": -0.03, "dribbled_past": -0.04},
    # Produzione "da 6" per 90' di un giocatore medio del ruolo: il rendimento
    # conta lo scarto da qui, così la media resta 6 e una statistica può anche
    # abbassare il voto (un difensore con zero contrasti non è "neutro").
    # Valori indicativi per la Serie A: vanno ritarati a fine stagione.
    "perf_baseline": {
        "P": {"saves": 2.5},
        "D": {"tackles": 1.8, "interceptions": 1.2, "blocks": 0.5,
              "duels_won": 4.5, "key_passes": 0.5, "fouls": 1.0, "dribbled_past": 0.7},
        "C": {"key_passes": 1.1, "tackles": 1.6, "interceptions": 0.8,
              "dribbles_won": 0.7, "shots_on": 0.4, "duels_won": 4.5,
              "fouls": 1.2, "dribbled_past": 0.8},
        "A": {"shots_on": 1.0, "key_passes": 0.9, "dribbles_won": 1.0,
              "duels_won": 4.0, "fouls": 1.1, "dribbled_past": 0.3},
    },
    # Sotto questi minuti il "per 90'" si calcola come se fossero questi,
    # così un subentrato con un tiro in 10' non vale 9 tiri a partita.
    "perf_min_minutes": 30,
    # La voce rendimento non sposta il voto più di così, in su o in giù.
    "perf_cap": 0.75,

    # --- standardizzazione ------------------------------------------------
    # Trasformazione lineare del grezzo: voto = (grezzo - 6) * scale + 6 + shift.
    # ``calibrate()`` stima scale/shift su una stagione per avere la
    # distribuzione voluta; di default l'algoritmo resta com'è.
    "calib_scale": 1.0,
    "calib_shift": 0.0,

    # --- uscita ------------------------------------------------------------
    "min_vote": 4.0,
    "max_vote": 8.5,
    "step": 0.5,             # arrotondamento: 0.5 = pagella classica
}

# Statistiche avanzate riconosciute (tutte facoltative, interi >= 0).
STAT_KEYS = ("saves", "tackles", "interceptions", "blocks", "duels_won",
             "key_passes", "dribbles_won", "shots_on", "fouls", "dribbled_past")


def effective_algo_rules(raw=None):
    """Regole dell'algoritmo per una lega: i default con sopra le sue.
    ``perf_weights`` si fonde per ruolo, così una lega può cambiare un solo
    peso senza riscrivere la tabella."""
    raw = dict(raw or {})
    rules = {**ALGO_DEFAULTS, **{k: v for k, v in raw.items()
                                 if k not in ("perf_weights", "perf_malus", "perf_baseline")}}
    for table in ("perf_weights", "perf_baseline"):
        merged = {r: dict(w) for r, w in ALGO_DEFAULTS[table].items()}
        for role, w in (raw.get(table) or {}).items():
            merged.setdefault(role, {}).update(w or {})
        rules[table] = merged
    rules["perf_malus"] = {**ALGO_DEFAULTS["perf_malus"], **(raw.get("perf_malus") or {})}
    rules["_effective"] = True
    return rules


def _i(v):
    try:
        return max(int(v or 0), 0)
    except (TypeError, ValueError):
        return 0


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _round_step(x, step):
    """Arrotonda al multiplo di ``step`` più vicino, metà in su (6.25 → 6.5)."""
    step = Decimal(str(step))
    q = (Decimal(str(x)) / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (q * step).quantize(Decimal("0.1"))


def is_decisive(row, role, rules):
    """Un evento che dà diritto al voto anche sotto i minuti minimi."""
    if any(_i(row.get(k)) for k in ("goals", "assists", "own_goals", "pen_missed",
                                    "pen_saved", "pen_committed")):
        return True
    if row.get("red"):
        return True
    if role == "P" and rules.get("gk_vote_if_conceded") and _i(row.get("goals_conceded")):
        return True
    return False


def has_vote(row, role, rules):
    minutes = _i(row.get("minutes"))
    if minutes <= 0:
        return False
    return minutes >= rules["min_minutes"] or is_decisive(row, role, rules)


def _result_part(row, rules):
    gf, ga = row.get("team_goals_for"), row.get("team_goals_against")
    if gf is None or ga is None:
        return 0.0
    gf, ga = _i(gf), _i(ga)
    diff = gf - ga
    if diff > 0:
        part = rules["win"]
    elif diff < 0:
        part = rules["loss"]
    else:
        part = rules["draw"]
    if abs(diff) > 1:
        extra = min((abs(diff) - 1) * rules["margin_step"], rules["margin_cap"])
        part += extra if diff > 0 else -extra
    if rules.get("result_scaled_by_minutes"):
        part *= min(_i(row.get("minutes")), 90) / 90
    return part


def _conceded_on_pitch(row, role):
    """Gol subiti con il giocatore in campo. Per il portiere la fonte lo dà
    (``goals_conceded``); per gli altri si stima dai gol della squadra."""
    if role == "P":
        return float(_i(row.get("goals_conceded")))
    ga = row.get("team_goals_against")
    if ga is None:
        return 0.0
    return _i(ga) * min(_i(row.get("minutes")), 90) / 90


def _defence_part(row, role, rules):
    if role not in ("P", "D"):
        return 0.0
    conceded = _conceded_on_pitch(row, role)
    part = 0.0
    if conceded == 0 and _i(row.get("minutes")) >= rules["clean_sheet_min_minutes"]:
        # senza dati sul risultato non si può dire che sia porta inviolata
        if role == "P" or row.get("team_goals_against") is not None:
            part += rules[f"clean_sheet_{role}"]
    part += max(rules[f"conceded_{role}"] * conceded, rules["conceded_cap"])
    return part


def _events_part(row, role, rules):
    goal_w = rules.get(f"goal_{role}", rules["goal"])
    part = goal_w * _i(row.get("goals"))
    part += rules["assist"] * _i(row.get("assists"))
    part += rules["own_goal"] * _i(row.get("own_goals"))
    part += rules["pen_missed"] * _i(row.get("pen_missed"))
    part += rules["pen_won"] * _i(row.get("pen_won"))
    part += rules["pen_committed"] * _i(row.get("pen_committed"))
    if role == "P":
        part += rules["pen_saved"] * _i(row.get("pen_saved"))
    if row.get("red"):
        part += rules["red"]          # il rosso assorbe il giallo
    elif row.get("yellow"):
        part += rules["yellow"]
    return part


def _perf_part(row, role, rules):
    stats = {k: row.get(k) for k in STAT_KEYS if row.get(k) not in (None, "")}
    if not stats:
        return 0.0
    per90 = 90 / max(_i(row.get("minutes")), rules["perf_min_minutes"])
    weights = {**rules["perf_weights"].get(role, {}), **rules["perf_malus"]}
    baseline = rules["perf_baseline"].get(role, {})
    # Una statistica che la fonte non dà vale "nella media" (contributo 0).
    part = sum(w * (_i(stats[k]) * per90 - baseline.get(k, 0))
               for k, w in weights.items() if k in stats)
    # Il per-90 gonfia chi ha giocato poco: il contributo si riporta ai minuti.
    part *= min(_i(row.get("minutes")), 90) / 90
    return _clamp(part, -rules["perf_cap"], rules["perf_cap"])


def player_vote(row, role=None, rules=None):
    """Voto algoritmico di un giocatore in una partita.

    ``row`` è una riga nel formato di ``apifootball.fixture_player_rows`` più:
    ``minutes``, ``team_goals_for``, ``team_goals_against`` e, se ci sono, le
    statistiche di ``STAT_KEYS``, ``pen_won``, ``pen_committed``.
    ``role`` (P/D/C/A) di default è ``row["role"]``.

    Ritorna ``{"vote": Decimal | None, "raw": float | None, "breakdown": {...}}``.
    ``vote`` None = senza voto.
    """
    if not (rules or {}).get("_effective"):
        rules = effective_algo_rules(rules)
    role = (role or row.get("role") or "").upper()
    if role not in ROLES:
        role = "C"
    if not has_vote(row, role, rules):
        return {"vote": None, "raw": None, "breakdown": {"sv": True}}

    parts = {
        "base": rules["base"],
        "risultato": _result_part(row, rules),
        "reparto": _defence_part(row, role, rules),
        "eventi": _events_part(row, role, rules),
        "rendimento": _perf_part(row, role, rules),
    }
    raw = sum(parts.values())
    calibrated = (raw - rules["base"]) * rules["calib_scale"] + rules["base"] + rules["calib_shift"]
    bounded = _clamp(calibrated, rules["min_vote"], rules["max_vote"])
    vote = _round_step(bounded, rules["step"])
    breakdown = {k: round(v, 3) for k, v in parts.items()}
    if rules["calib_scale"] != 1.0 or rules["calib_shift"] != 0.0:
        breakdown["calibrazione"] = round(calibrated - raw, 3)
    return {"vote": vote, "raw": round(raw, 3), "breakdown": breakdown}


def match_votes(rows, rules=None):
    """Applica ``player_vote`` a tutte le righe di una o più partite.
    Ritorna le righe con ``vote`` sostituito dal voto algoritmico e
    ``algo`` con raw e breakdown; non tocca bonus/malus."""
    rules = effective_algo_rules(rules)
    out = []
    for row in rows:
        res = player_vote(row, rules=rules)
        out.append({**row, "vote": res["vote"],
                    "algo": {"raw": res["raw"], "breakdown": res["breakdown"]}})
    return out


def calibrate(raw_scores, target_mean=6.0, target_sd=0.6, base=6.0):
    """Standardizzazione: stima ``calib_scale``/``calib_shift`` perché i voti
    grezzi di un campione (es. le giornate già giocate di una stagione) abbiano
    la media e la deviazione standard volute.

    Si usa una volta (o a inizio stagione) e il risultato si salva nelle regole
    della lega: ricalibrare a ogni giornata renderebbe i voti non confrontabili.
    """
    scores = [float(s) for s in raw_scores if s is not None]
    if len(scores) < 30:
        raise ValueError("servono almeno 30 voti per calibrare")
    m, sd = mean(scores), pstdev(scores)
    scale = target_sd / sd if sd > 0 else 1.0
    # voto = (raw - base) * scale + base + shift  →  media = target_mean
    shift = target_mean - ((m - base) * scale + base)
    return {"calib_scale": round(scale, 4), "calib_shift": round(shift, 4)}
