"""Il sistema Mantra: ruoli dettagliati, moduli e compatibilità slot/giocatore.

Nel fantacalcio Classic un giocatore è P, D, C o A e un modulo è solo un conto
di quanti ne schieri per reparto. Mantra sostituisce quei quattro ruoli con
dodici posizioni reali (un terzino destro non è un centrale), assegna a ogni
giocatore *uno o più* di quei ruoli, e definisce ogni modulo come una sequenza
ordinata di slot: ciascuno accetta un insieme preciso di ruoli.

Questo modulo è puro: nessun import di Django, nessuna query. Tiene solo i dati
del regolamento e le funzioni che rispondono a "questo giocatore può stare in
questo slot?" — così la tabella resta leggibile e correggibile da chiunque
apra il file, senza doverla inseguire dentro viste e template.

Fonti della tabella moduli: il regolamento ufficiale Fantacalcio.it (che elenca
gli undici schemi e la regola dei 5 difensivi + 5 offensivi) e le guide che ne
riportano la composizione slot per slot. Il 4-1-4-1 è stato verificato su due
fonti separate perché la prima lo riportava a dieci slot invece di undici.
"""

# --- Ruoli ------------------------------------------------------------------
# code -> (etichetta estesa, reparto, ruolo classico corrispondente)
# Il reparto ("D" difensivo / "O" offensivo) è la regola cardine del Mantra:
# ogni modulo schiera esattamente 5 di movimento difensivi e 5 offensivi.
ROLES = {
    "Por": ("Portiere",              "P", "P"),
    "Dd":  ("Difensore destro",      "D", "D"),
    "Dc":  ("Difensore centrale",    "D", "D"),
    "Ds":  ("Difensore sinistro",    "D", "D"),
    "B":   ("Braccetto",             "D", "D"),
    "E":   ("Esterno",               "D", "D"),
    "M":   ("Mediano",               "D", "C"),
    "C":   ("Centrocampista",        "O", "C"),
    "W":   ("Ala",                   "O", "C"),
    "T":   ("Trequartista",          "O", "C"),
    "A":   ("Attaccante",            "O", "A"),
    "Pc":  ("Punta centrale",        "O", "A"),
}

# Ordine di reparto, usato ovunque si debbano mostrare più ruoli in fila.
ROLE_ORDER = ["Por", "Dd", "Dc", "Ds", "B", "E", "M", "C", "W", "T", "A", "Pc"]

# Colore del badge. Segue la scala già usata in regia per i ruoli classici
# (portiere ambra, difesa verde, centrocampo azzurro, attacco rosso), con una
# sfumatura per reparto: chi guarda il maxischermo riconosce il reparto dal
# colore prima ancora di leggere la sigla.
ROLE_COLORS = {
    "Por": "#f4b740",
    "Dd": "#3fb950", "Dc": "#2ea043", "Ds": "#3fb950", "B": "#56d364", "E": "#7ee787",
    "M": "#58a6ff", "C": "#4493f8", "W": "#79c0ff", "T": "#a5d6ff",
    "A": "#f85149", "Pc": "#ff7b72",
}

# Il separatore usato dal listone ufficiale nella colonna RM ("Dd;Ds;E").
_SEP = ";"


def parse_roles(raw):
    """``"Dd;Ds;E"`` → ``["Dd", "Ds", "E"]``. Ignora sigle sconosciute.

    Tollera separatori diversi (``/``, ``,``, spazi) e differenze di maiuscole,
    perché questa stringa può arrivare anche da un file rifatto a mano e non
    solo dall'export ufficiale. L'ordine dei ruoli del giocatore è conservato:
    il primo elencato è il ruolo naturale, e alcune leghe ci contano.
    """
    if not raw:
        return []
    text = str(raw).replace("/", _SEP).replace(",", _SEP).replace(" ", _SEP)
    out = []
    lookup = {code.lower(): code for code in ROLES}
    for chunk in text.split(_SEP):
        code = lookup.get(chunk.strip().lower())
        if code and code not in out:
            out.append(code)
    return out


def classic_role(roles):
    """Il ruolo classico (P/D/C/A) equivalente a una lista di ruoli Mantra.

    Serve da rete di sicurezza quando il listone porta la colonna RM ma non la
    R: si prende il ruolo classico del primo Mantra elencato. Vuoto → ``""``,
    così chi chiama decide cosa farne invece di ricevere una "A" inventata.
    """
    for code in roles:
        if code in ROLES:
            return ROLES[code][2]
    return ""


def is_goalkeeper(roles):
    return "Por" in roles


def label(code):
    return ROLES.get(code, (code,))[0]


# --- Moduli -----------------------------------------------------------------
# Ogni modulo è una sequenza ORDINATA di slot; ogni slot è la tupla dei ruoli
# che può ospitare. L'ordine conta: è quello in cui gli slot vengono disegnati
# in campo (portiere, poi difesa, poi centrocampo, poi trequarti, poi attacco)
# ed è l'ordine in cui la formazione viene salvata.
#
# La struttura per reparti serve solo a impaginare il campo; la validazione
# lavora sulla lista piatta.
MODULES = {
    "3-4-3":   [("Por",), ("Dc",), ("Dc",), ("Dc",),
                ("E",), ("M", "C"), ("C",), ("E",),
                ("W", "A"), ("A", "Pc"), ("W", "A")],
    "3-4-1-2": [("Por",), ("Dc",), ("Dc",), ("Dc",),
                ("E",), ("M", "C"), ("C",), ("E",),
                ("T",), ("A", "Pc"), ("A", "Pc")],
    "3-4-2-1": [("Por",), ("Dc",), ("Dc",), ("Dc",),
                ("E", "W"), ("M", "C"), ("C",), ("E", "W"),
                ("T",), ("T", "A"), ("A", "Pc")],
    "3-5-2":   [("Por",), ("Dc",), ("Dc",), ("Dc",),
                ("E",), ("M",), ("C",), ("M", "C"), ("E", "W"),
                ("Pc",), ("Pc",)],
    "3-5-1-1": [("Por",), ("Dc",), ("Dc",), ("Dc",),
                ("E", "W"), ("M",), ("M",), ("C",), ("E", "W"),
                ("T",), ("A", "Pc")],
    "4-3-3":   [("Por",), ("Dd",), ("Dc",), ("Dc",), ("Ds",),
                ("M",), ("C",), ("M", "C"),
                ("W", "A"), ("A", "Pc"), ("W", "A")],
    "4-3-1-2": [("Por",), ("Dd",), ("Dc",), ("Dc",), ("Ds",),
                ("M",), ("C",), ("M", "C"),
                ("T",), ("A", "Pc"), ("A", "Pc")],
    "4-4-2":   [("Por",), ("Dd",), ("Dc",), ("Dc",), ("Ds",),
                ("C",), ("C", "M"), ("E",), ("E", "W"),
                ("A", "Pc"), ("A", "Pc")],
    "4-1-4-1": [("Por",), ("Dd",), ("Dc",), ("Dc",), ("Ds",),
                ("M",),
                ("E", "W"), ("C", "T"), ("T",), ("W",),
                ("A", "Pc")],
    "4-4-1-1": [("Por",), ("Dd",), ("Dc",), ("Dc",), ("Ds",),
                ("M",), ("C",), ("E", "W"), ("E", "W"),
                ("T", "A"), ("A", "Pc")],
    "4-2-3-1": [("Por",), ("Dd",), ("Dc",), ("Dc",), ("Ds",),
                ("M",), ("M", "C"),
                ("W",), ("T",), ("W", "A"),
                ("A", "Pc")],
}

DEFAULT_MODULE = "4-3-3"

# Come spezzare la lista piatta in righe da disegnare in campo. Il numero di
# slot per reparto si ricava dal nome del modulo (il portiere è sempre il primo
# slot), quindi non c'è una seconda tabella da tenere allineata alla prima.
LINE_LABELS = ["Portiere", "Difesa", "Centrocampo", "Trequarti", "Attacco"]


def module_lines(module):
    """Il modulo spezzato in righe ``(etichetta, [indici slot])`` per il campo.

    ``"4-2-3-1"`` → Portiere [0], Difesa [1..4], Centrocampo [5,6],
    Trequarti [7,8,9], Attacco [10]. Le etichette centrali si adattano a quanti
    reparti ha davvero il modulo: un 4-4-2 non ha la riga trequarti.
    """
    if module not in MODULES:
        module = DEFAULT_MODULE
    counts = [int(n) for n in module.split("-") if n.isdigit()]
    # Nome del reparto: il primo è sempre il portiere, l'ultimo l'attacco, e i
    # reparti in mezzo scalano dalla difesa in avanti.
    names = ["Portiere"]
    middle = ["Difesa", "Centrocampo", "Trequarti"]
    for i in range(len(counts)):
        if i == len(counts) - 1:
            names.append("Attacco")
        else:
            names.append(middle[i] if i < len(middle) else "Centrocampo")
    lines, cursor = [], 0
    for name, n in zip(names, [1] + counts):
        lines.append((name, list(range(cursor, cursor + n))))
        cursor += n
    return lines


def slot_label(slot):
    """``("E", "W")`` → ``"E/W"``: come il regolamento scrive uno slot doppio."""
    return "/".join(slot)


def slot_accepts(slot, roles):
    """Il giocatore con questi ruoli può occupare lo slot senza adattamenti?

    Compatibilità stretta: basta che *uno* dei ruoli del giocatore sia tra
    quelli ammessi dallo slot. È la regola dello schieramento iniziale — gli
    adattamenti fuori posizione col malus riguardano le sostituzioni a partita
    in corso, che qui non gestiamo.
    """
    return any(r in slot for r in roles)


def eligible_slots(module, roles):
    """Indici degli slot del modulo che questo giocatore può occupare."""
    slots = MODULES.get(module) or MODULES[DEFAULT_MODULE]
    return [i for i, slot in enumerate(slots) if slot_accepts(slot, roles)]


def module_requirements(module):
    """Quanti giocatori per ruolo servono *come minimo* per schierare il modulo.

    Conta solo gli slot a ruolo unico: uno slot ``E/W`` non obbliga a possedere
    né un E né una W in particolare, mentre tre slot ``Dc`` obbligano a tre
    difensori centrali. È quello che serve sapere *durante l'asta*, per capire
    se la rosa che stai costruendo potrà davvero schierare il modulo.
    """
    slots = MODULES.get(module) or MODULES[DEFAULT_MODULE]
    need = {}
    for slot in slots:
        if len(slot) == 1:
            need[slot[0]] = need.get(slot[0], 0) + 1
    return need
