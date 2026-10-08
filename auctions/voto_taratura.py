"""Taratura automatica del voto algoritmico sui voti di riferimento.

Puro come ``voto_algoritmico``: niente modelli, niente DB, nessuna dipendenza.

Il voto grezzo è lineare nei parametri:

    grezzo = base + Σ parametro × contributo_per_unità

(il «contributo per unità» di ``win`` è la quota di minuti se la squadra ha
vinto, quello di ``goal`` il numero di gol, quello di un peso del rendimento lo
scarto della statistica per 90' dalla media del ruolo, …). Fanno eccezione i
tetti (``margin_cap``, ``conceded_cap``, ``perf_cap``): le righe in cui un
tetto è attivo si escludono dalla regressione, con i valori di partenza o con
quelli stimati (la stima si ripete finché quell'insieme non cambia, vedi
``_fit``). Limiti min/max e arrotondamento stanno dopo il grezzo e non entrano
nella stima.

La stima è una ridge regression verso i valori attuali:

    (XᵀX/n + λD) w = Xᵀy/n + λD·w_attuale

con D diagonale = varianza di ogni contributo (la scala di una statistica non
conta). λ è la «Prudenza»: più è alta, meno la proposta si allontana dai valori
attuali. Poi i vincoli: range dei campi, spostamento massimo, segno di bonus e
malus. La proposta si verifica su giornate (o partite) non usate per tararla.

I voti di riferimento servono a capire dove l'algoritmo sbaglia, non a
copiarli: per questo la regolarizzazione, i tetti allo spostamento e la
verifica fuori campione.
"""
import math
import random
from statistics import mean, pstdev

from .voto_algoritmico import (ROLES, STAT_KEYS, _conceded_on_pitch, _i, calibrate, effective_algo_rules,
                               has_vote, player_vote)

# (chiave, titolo): gruppi di parametri che il superuser può escludere.
GROUPS = [
    ("base", "Voto di partenza"),
    ("risultato", "Risultato della squadra"),
    ("reparto", "Reparto"),
    ("eventi", "Eventi"),
    ("rendimento", "Rendimento (pesi e malus)"),
]
RESULT_KEYS = ("win", "draw", "loss", "margin_step")
DEFENCE_KEYS = ("clean_sheet_P", "clean_sheet_D", "conceded_P", "conceded_D")
EVENT_KEYS = ("goal", "goal_D", "goal_P", "assist", "yellow", "red", "own_goal",
              "pen_missed", "pen_saved", "pen_won", "pen_committed")
# Segno fisso: un malus resta ≤ 0, un bonus ≥ 0 (``base`` e ``draw`` liberi).
MALUS_KEYS = {"loss", "conceded_P", "conceded_D", "yellow", "red", "own_goal", "pen_missed", "pen_committed"}
BONUS_KEYS = {"win", "margin_step", "clean_sheet_P", "clean_sheet_D", "goal", "goal_D", "goal_P",
              "assist", "pen_saved", "pen_won"}
# Spostamento massimo rispetto al valore attuale.
MOVE_FLAT = 0.5
MOVE_PERF = 0.15
# «Prudenza»: λ della ridge.
PRUDENZA = {"bassa": 0.1, "media": 0.5, "alta": 2.0}
LAMBDA_RANGE = (0.01, 20.0)
MIN_ROWS = 300
# Stima ripetuta finché le righe con un tetto attivo non cambiano.
MAX_ITERATIONS = 4
MIN_GAIN = 0.02
SPLIT_TRAIN = 0.7
SPLIT_SEED = 7


# --------------------------------------------------------------------------
# Percorsi dei parametri
# --------------------------------------------------------------------------

def get_value(rules, path):
    target = rules
    for key in path.split("."):
        target = target[key]
    return target


def set_value(rules, path, value):
    keys = path.split(".")
    target = rules
    for key in keys[:-1]:
        target = target.setdefault(key, {})
    target[keys[-1]] = value


def group_of(path):
    if path == "base":
        return "base"
    if path in RESULT_KEYS:
        return "risultato"
    if path in DEFENCE_KEYS:
        return "reparto"
    if path in EVENT_KEYS:
        return "eventi"
    if path.startswith(("perf_weights.", "perf_malus.")):
        return "rendimento"
    return None


def tunable_paths(rules, groups=None):
    """I parametri tarabili, nei gruppi scelti. Non si tarano medie del ruolo,
    minuti, tetti, limiti, arrotondamento e calibrazione."""
    groups = set(groups if groups is not None else (g for g, _ in GROUPS))
    paths = ["base", *RESULT_KEYS, *DEFENCE_KEYS, *EVENT_KEYS]
    for role in ROLES:
        for stat in rules["perf_weights"].get(role, {}):
            if stat not in rules["perf_malus"]:          # il malus vince sul peso del ruolo
                paths.append(f"perf_weights.{role}.{stat}")
    paths += [f"perf_malus.{stat}" for stat in rules["perf_malus"]]
    return [p for p in paths if group_of(p) in groups]


# --------------------------------------------------------------------------
# Matrice delle feature
# --------------------------------------------------------------------------

def _analyze(row, role, rules):
    """({percorso: contributo per unità}, [tetti attivi]) con le formule di
    ``voto_algoritmico``. ``base`` non c'è: vale sempre 1."""
    x, capped = {}, []

    def add(path, value):
        if value:
            x[path] = x.get(path, 0.0) + value

    minutes = _i(row.get("minutes"))
    share = min(minutes, 90) / 90

    gf, ga = row.get("team_goals_for"), row.get("team_goals_against")
    if gf is not None and ga is not None:
        diff = _i(gf) - _i(ga)
        scale = share if rules.get("result_scaled_by_minutes") else 1.0
        add("win" if diff > 0 else "loss" if diff < 0 else "draw", scale)
        if abs(diff) > 1:
            steps = abs(diff) - 1
            if steps * rules["margin_step"] > rules["margin_cap"]:
                capped.append("margin_cap")
            else:
                add("margin_step", (1 if diff > 0 else -1) * steps * scale)

    if role in ("P", "D"):
        conceded = _conceded_on_pitch(row, role)
        if conceded == 0 and minutes >= rules["clean_sheet_min_minutes"]:
            if role == "P" or ga is not None:
                add(f"clean_sheet_{role}", 1.0)
        if rules[f"conceded_{role}"] * conceded < rules["conceded_cap"]:
            capped.append("conceded_cap")
        else:
            add(f"conceded_{role}", conceded)

    goal_key = f"goal_{role}" if f"goal_{role}" in rules else "goal"
    add(goal_key, _i(row.get("goals")))
    add("assist", _i(row.get("assists")))
    add("own_goal", _i(row.get("own_goals")))
    add("pen_missed", _i(row.get("pen_missed")))
    add("pen_won", _i(row.get("pen_won")))
    add("pen_committed", _i(row.get("pen_committed")))
    if role == "P":
        add("pen_saved", _i(row.get("pen_saved")))
    if row.get("red"):
        add("red", 1.0)
    elif row.get("yellow"):
        add("yellow", 1.0)

    stats = {k: row.get(k) for k in STAT_KEYS if row.get(k) not in (None, "")}
    if stats:
        per90 = 90 / max(minutes, rules["perf_min_minutes"])
        weights = {**rules["perf_weights"].get(role, {}), **rules["perf_malus"]}
        baseline = rules["perf_baseline"].get(role, {})
        part, contrib = 0.0, {}
        for stat, w in weights.items():
            if stat not in stats:
                continue
            unit = (_i(stats[stat]) * per90 - baseline.get(stat, 0)) * share
            part += w * unit
            path = f"perf_malus.{stat}" if stat in rules["perf_malus"] else f"perf_weights.{role}.{stat}"
            contrib[path] = unit
        if abs(part) > rules["perf_cap"]:
            capped.append("perf_cap")
        else:
            for path, unit in contrib.items():
                add(path, unit)
    return x, capped


def _role(row, role=None):
    role = (role or row.get("role") or "").upper()
    return role if role in ROLES else "C"


def feature_row(row, role=None, rules=None):
    """{percorso_parametro: contributo per unità} di una riga: senza tetti
    attivi, ``base + Σ valore(percorso) × contributo`` è il grezzo di
    ``player_vote``."""
    rules = rules if (rules or {}).get("_effective") else effective_algo_rules(rules)
    return _analyze(row, _role(row, role), rules)[0]


def cap_active(row, role=None, rules=None):
    rules = rules if (rules or {}).get("_effective") else effective_algo_rules(rules)
    return bool(_analyze(row, _role(row, role), rules)[1])


# --------------------------------------------------------------------------
# Algebra
# --------------------------------------------------------------------------

def solve(a, b):
    """Risolve a·x = b (eliminazione di Gauss con pivot parziale)."""
    n = len(b)
    m = [list(map(float, a[i])) + [float(b[i])] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise ValueError("sistema singolare")
        m[col], m[pivot] = m[pivot], m[col]
        p = m[col][col]
        for r in range(col + 1, n):
            f = m[r][col] / p
            if f:
                row_r, row_c = m[r], m[col]
                for c in range(col, n + 1):
                    row_r[c] -= f * row_c[c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


# --------------------------------------------------------------------------
# Stima
# --------------------------------------------------------------------------

def _bounds(path, anchor, ranges):
    """[(limite, motivo)] inferiore e superiore di un parametro."""
    move = MOVE_PERF if path.startswith(("perf_weights.", "perf_malus.")) else MOVE_FLAT
    lows = [(anchor - move, "spostamento")]
    highs = [(anchor + move, "spostamento")]
    if path in ranges:
        lo, hi = ranges[path]
        lows.append((lo, "range"))
        highs.append((hi, "range"))
    if path in MALUS_KEYS or path.startswith("perf_malus."):
        highs.append((0.0, "segno"))
    if path in BONUS_KEYS or path.startswith("perf_weights."):
        lows.append((0.0, "segno"))
    return max(lows), min(highs)


def _sums(data, cols):
    """Somme per la regressione su ``data`` = [(x_sparsa, y)] con ``x``
    già ridotta alle colonne ``cols`` (indici)."""
    k = len(cols)
    sxx = [[0.0] * k for _ in range(k)]
    sxy = [0.0] * k
    sx = [0.0] * k
    for xs, y in data:
        items = list(xs.items())
        for j, v in items:
            sxy[j] += v * y
            sx[j] += v
            row = sxx[j]
            for jj, vv in items:
                row[jj] += v * vv
    return sxx, sxy, sx, len(data)


def _encode(entries, anchor_rules, paths):
    """Riduce le righe alle colonne tarabili: ``y`` toglie i contributi dei
    parametri fissi. Ritorna (colonne attive, dati codificati)."""
    paths_set = set(paths)
    fixed_base = 0.0 if "base" in paths_set else anchor_rules["base"]
    used = set()
    for x, _y in entries:
        used.update(p for p in x if p in paths_set)
    cols = (["base"] if "base" in paths_set else []) + [p for p in paths if p != "base" and p in used]
    index = {p: j for j, p in enumerate(cols)}
    data = []
    for x, y in entries:
        y_adj = y - fixed_base - sum(get_value(anchor_rules, p) * v for p, v in x.items() if p not in index)
        xs = {index[p]: v for p, v in x.items() if p in index}
        if "base" in index:
            xs[index["base"]] = 1.0
        data.append((xs, y_adj))
    return cols, data


def _estimate(entries, anchor_rules, paths, lam, ranges):
    """Parametri stimati con ridge + vincoli: ({percorso: valore}, {percorso: [motivi]})."""
    cols, data = _encode(entries, anchor_rules, paths)
    if not cols or not data:
        return {}, {}
    sxx, sxy, sx, n = _sums(data, cols)
    k = len(cols)
    w0 = [get_value(anchor_rules, p) for p in cols]
    d = []
    for j in range(k):
        m1, m2 = sx[j] / n, sxx[j][j] / n
        var = m2 - m1 * m1
        d.append(var if var > 1e-12 else max(m2, 1e-12))   # base: costante, si usa il momento secondo
    bounds = [_bounds(p, w0[j], ranges) for j, p in enumerate(cols)]
    fixed, flags = {}, {}
    for j, ((lo, why_lo), (hi, why_hi)) in enumerate(bounds):
        if lo > hi:                       # vincoli incompatibili: resta com'è
            fixed[j] = w0[j]
            flags[cols[j]] = sorted({why_lo, why_hi})
    w = list(w0)
    for _ in range(len(cols) + 1):
        free = [j for j in range(k) if j not in fixed]
        if not free:
            break
        a = [[sxx[i][j] / n + (lam * d[i] if i == j else 0.0) for j in free] for i in free]
        b = []
        for i in free:
            rhs = sxy[i] / n + lam * d[i] * w0[i]
            rhs -= sum(sxx[i][j] * v for j, v in fixed.items()) / n
            b.append(rhs)
        try:
            sol = solve(a, b)
        except ValueError:
            sol = [w0[i] for i in free]
        newly = False
        for i, v in zip(free, sol):
            (lo, why_lo), (hi, why_hi) = bounds[i]
            if v < lo - 1e-12:
                fixed[i], flags[cols[i]], newly = lo, [why_lo], True
            elif v > hi + 1e-12:
                fixed[i], flags[cols[i]], newly = hi, [why_hi], True
            else:
                w[i] = v
        if not newly:
            break
    for j, v in fixed.items():
        w[j] = v
    return {p: w[j] for j, p in enumerate(cols)}, flags


def _apply(anchor_rules, values):
    rules = {k: (dict(v) if isinstance(v, dict) else v) for k, v in anchor_rules.items()}
    for table in ("perf_weights", "perf_baseline"):
        rules[table] = {r: dict(w) for r, w in anchor_rules[table].items()}
    for path, v in values.items():
        set_value(rules, path, v)
    return rules


def _neutral(rules):
    rules = effective_algo_rules({k: v for k, v in rules.items() if not k.startswith("_")})
    rules.update({"calib_scale": 1.0, "calib_shift": 0.0})
    return rules


# --------------------------------------------------------------------------
# Stima con i tetti
# --------------------------------------------------------------------------
#
# Una riga con un tetto attivo non è lineare nei parametri e va tolta dalla
# regressione. Ma il tetto dipende dai valori: una riga libera con quelli di
# partenza può finire al tetto con quelli stimati (un portiere che subisce 3
# gol: -0,25 × 3 = -0,75, sopra il tetto di -1; -0,45 × 3 = -1,35, al tetto).
# Lasciarla dentro attenua la stima proprio dei parametri con un tetto
# (gol subiti, scarto). Per questo la stima si ripete escludendo le righe al
# tetto con i valori di partenza OPPURE con quelli proposti, finché l'insieme
# escluso non cambia. La regolarizzazione resta ancorata ai valori di partenza.

def _candidates(rows, ref, anchor, indices=None):
    """Le righe utili alla taratura: voto di riferimento e voto
    dell'algoritmo. Ogni voce: (indice, riga, ruolo, rif, contributi, tetto
    attivo con i valori di partenza)."""
    out = []
    for i in (range(len(rows)) if indices is None else indices):
        r = ref.get(i)
        row = rows[i]
        role = _role(row)
        if r is None or not has_vote(row, role, anchor):
            continue
        x, caps = _analyze(row, role, anchor)
        out.append((i, row, role, r, x, bool(caps)))
    return out


def _fit(cands, anchor, paths, lam, ranges, max_iterations=MAX_ITERATIONS):
    """Stima ripetuta sui candidati. Ritorna {values, flags, excluded (indici
    delle righe escluse per un tetto), iterations, trace (gli insiemi esclusi
    a ogni iterazione), stopped ("stabile" | "oscilla" | "limite")}."""
    anchor_capped = frozenset(i for i, *_rest, capped in cands if capped)
    excluded = anchor_capped
    seen, records = [], []
    stopped = "limite"
    for _ in range(max_iterations):
        entries = [(x, r) for i, _row, _role, r, x, _c in cands if i not in excluded]
        values, flags = _estimate(entries, anchor, paths, lam, ranges)
        proposed = _apply(anchor, values)
        records.append({"values": values, "flags": flags, "excluded": excluded, "rules": proposed})
        seen.append(excluded)
        nxt = anchor_capped | frozenset(i for i, row, role, _r, _x, _c in cands
                                        if _analyze(row, role, proposed)[1])
        if nxt == excluded:
            stopped = "stabile"
            break
        if nxt in seen:
            stopped = "oscilla"
            break
        excluded = nxt
    chosen = records[-1]
    if stopped != "stabile" and len(records) > 1:
        # Nessun punto fermo: la proposta con l'errore di taratura più basso.
        rows = [row for _i, row, *_rest in cands]
        ref = {n: c[3] for n, c in enumerate(cands)}
        chosen = min(records, key=lambda rec: (_mae(_vote_pairs(rows, ref, rec["rules"])) or 0.0))
    return {"values": chosen["values"], "flags": chosen["flags"], "excluded": chosen["excluded"],
            "iterations": len(records), "trace": [rec["excluded"] for rec in records], "stopped": stopped}


# --------------------------------------------------------------------------
# Dati, validazione, proposta
# --------------------------------------------------------------------------

def match_keys(rows):
    """Chiave di partita di ogni riga: ``fixture_id`` se c'è, altrimenti le
    squadre a coppie nell'ordine delle righe (API-Football elenca i giocatori
    di casa e poi quelli in trasferta, partita per partita)."""
    keys, block, prev = [], -1, None
    for r in rows:
        if r.get("fixture_id") is not None:
            keys.append(("f", r.get("_g"), r["fixture_id"]))
            continue
        team = (r.get("_g"), r.get("team") or "")
        if team != prev:
            block += 1
            prev = team
        keys.append(("b", r.get("_g"), block // 2))
    return keys


def _mae(pairs):
    return round(mean(abs(a - b) for a, b in pairs), 4) if pairs else None


def _vote_pairs(rows, ref, rules):
    pairs = []
    for i, row in enumerate(rows):
        r = ref.get(i)
        if r is None:
            continue
        v = player_vote(row, rules=rules)["vote"]
        if v is not None:
            pairs.append((float(v), r))
    return pairs


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _calibration(rows, ref, rules):
    """Scala e spostamento che portano i voti di ``rules`` su media e
    dispersione dei voti di riferimento."""
    raws, targets = [], []
    for i, row in enumerate(rows):
        r = ref.get(i)
        if r is None:
            continue
        res = player_vote(row, rules=rules)
        if res["raw"] is not None:
            raws.append(res["raw"])
            targets.append(r)
    if len(raws) < 30:
        return None
    tm, tsd = mean(targets), pstdev(targets)
    cal = calibrate(raws, target_mean=tm, target_sd=tsd if tsd > 0 else 0.6, base=rules["base"])
    return {"calib_scale": round(_clamp(cal["calib_scale"], 0.2, 3.0), 4),
            "calib_shift": round(_clamp(cal["calib_shift"], -2.0, 2.0), 4),
            "target_mean": round(tm, 3), "target_sd": round(tsd, 3)}


def tune(rows, ref, rules, *, groups=None, lam=PRUDENZA["media"], ranges=None, min_rows=MIN_ROWS):
    """Proposta di taratura dei parametri ``rules`` (la bozza) verso i voti
    di riferimento ``ref`` = {indice riga: voto|None}.

    Le righe hanno ``_g`` (la giornata): con 2 o più giornate la verifica è
    «una giornata fuori» a turno, con una sola 70/30 per partita (seme fisso).
    ValueError sotto ``min_rows`` giocatori con voto in entrambi."""
    anchor = _neutral(rules)
    ranges = ranges or {}
    lam = _clamp(float(lam), *LAMBDA_RANGE)
    paths = tunable_paths(anchor, groups)

    cands = _candidates(rows, ref, anchor)
    both = len(cands)
    if both < min_rows:
        raise ValueError(f"servono almeno {min_rows} giocatori abbinati con voto in entrambi "
                         f"(algoritmo e riferimento): ce ne sono {both}")
    if not paths:
        raise ValueError("scegli almeno un gruppo di parametri da tarare")

    fit = _fit(cands, anchor, paths, lam, ranges)
    values, flags = fit["values"], fit["flags"]
    proposed = _apply(anchor, values)

    # Verifica fuori campione: ogni fold rifà la stessa stima ripetuta sulle
    # sole righe di taratura, così misura esattamente quello che si propone.
    giornate = sorted({row.get("_g") for row in rows if row.get("_g") is not None}, key=str)
    if len(giornate) >= 2:
        mode = "giornate"
        folds = [({i for i, row in enumerate(rows) if row.get("_g") == g}) for g in giornate]
    else:
        mode = "partite"
        keys = match_keys(rows)
        uniq = sorted(set(keys), key=str)
        random.Random(SPLIT_SEED).shuffle(uniq)
        test_keys = set(uniq[int(len(uniq) * SPLIT_TRAIN):])
        folds = [{i for i, k in enumerate(keys) if k in test_keys}]
    val_before, val_after = [], []
    for test in folds:
        train = [c for c in cands if c[0] not in test]
        if not train:
            continue
        fold_rules = _apply(anchor, _fit(train, anchor, paths, lam, ranges)["values"])
        test_rows = [rows[i] for i in sorted(test)]
        test_ref = {n: ref.get(i) for n, i in enumerate(sorted(test))}
        val_before += _vote_pairs(test_rows, test_ref, anchor)
        val_after += _vote_pairs(test_rows, test_ref, fold_rules)

    fit_before, fit_after = _vote_pairs(rows, ref, anchor), _vote_pairs(rows, ref, proposed)
    validation = {"mode": mode, "folds": len(folds), "n": len(val_after),
                  "mae_before": _mae(val_before), "mae_after": _mae(val_after)}
    gain = (validation["mae_before"] - validation["mae_after"]
            if validation["mae_before"] is not None and validation["mae_after"] is not None else None)
    validation["gain"] = round(gain, 4) if gain is not None else None
    warning = None
    if gain is None or gain < MIN_GAIN:
        warning = "non migliora sulle giornate di verifica: probabile adattamento eccessivo"

    params = []
    for path in paths:
        current = get_value(anchor, path)
        new = round(values.get(path, current), 4)
        params.append({"path": path, "group": group_of(path), "current": current, "proposed": new,
                       "delta": round(new - current, 4), "flags": flags.get(path, []),
                       "fitted": path in values})
    params.sort(key=lambda p: -abs(p["delta"]))
    rounded = _apply(anchor, {p["path"]: p["proposed"] for p in params})
    return {
        "lambda": lam,
        "groups": sorted({group_of(p) for p in paths}),
        "rows_both": both, "rows_used": both - len(fit["excluded"]), "rows_capped": len(fit["excluded"]),
        "iterations": fit["iterations"], "iterations_stop": fit["stopped"],
        "params": params,
        "proposal": {p["path"]: p["proposed"] for p in params if abs(p["delta"]) > 1e-9},
        "fit": {"n": len(fit_after), "mae_before": _mae(fit_before), "mae_after": _mae(fit_after)},
        "validation": validation,
        "warning": warning,
        "calibration": _calibration(rows, ref, rounded),
        "calibration_current": _calibration(rows, ref, anchor),
    }
