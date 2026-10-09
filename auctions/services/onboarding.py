"""Dalla registrazione alla lega che gioca: elenco delle squadre incollato,
collegamento di una squadra a un account (invito, codice), stato degli inviti,
la card «Prepara la lega» e i traguardi della lega per il Supervisor.

Il collegamento passa sempre da ``link_team``: invito (/invito/<token>/),
codice squadra in onboarding e in app_login, una sola regola (``claims_team``).
"""
import re

from django.db.models import Q
from django.utils import timezone

# Un indirizzo email dentro una riga qualsiasi.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# I separatori ammessi fra nome ed email (solo vicino all'email): ; tab - , :
EDGE_SEP = re.compile(r"^[\s;,\t:\-–—|<>()\[\]]+|[\s;,\t:\-–—|<>()\[\]]+$")
MAX_NAME = 80


def _name_from_email(email):
    """«mario.rossi@x.it» → «Mario Rossi»: una proposta, modificabile."""
    local = email.split("@", 1)[0]
    words = [w for w in re.split(r"[._+\-]+", local) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)[:MAX_NAME] or local[:MAX_NAME]


def parse_team_lines(text):
    """L'elenco «una squadra per riga» del wizard, come lista di dizionari
    ``{"name", "email", "name_from_email", "warnings"}``.

    - l'email si riconosce ovunque nella riga, il resto è il nome;
    - ``;``, tab, ``-``, ``,`` separano solo quando c'è un'email: «Nome, con
      virgola» resta un nome solo;
    - una riga con la sola email propone il nome dalla parte prima della @;
    - righe vuote saltate; nomi o email ripetuti segnalati (``warnings``),
      la seconda riga con lo stesso nome non crea una squadra in più.
    Lo stesso parser gira in JavaScript nel wizard (setup_wizard.html).
    """
    rows, seen_names, seen_emails = [], {}, {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = EMAIL_RE.search(line)
        email = ""
        name = line
        if m:
            email = m.group(0).strip(".")
            name = (line[:m.start()] + " " + line[m.end():]).strip()
            name = EDGE_SEP.sub("", name).strip()
        name = re.sub(r"\s+", " ", name)[:MAX_NAME]
        from_email = False
        if not name and email:
            name, from_email = _name_from_email(email), True
        if not name:
            continue
        row = {"name": name, "email": email.lower(), "name_from_email": from_email, "warnings": []}
        key = name.lower()
        if key in seen_names:
            row["warnings"].append("Nome già in elenco")
            row["duplicate"] = True
        seen_names.setdefault(key, len(rows))
        if email:
            if email.lower() in seen_emails:
                row["warnings"].append("Email già in elenco")
            seen_emails.setdefault(email.lower(), len(rows))
        rows.append(row)
    return rows


# --- Collegare una squadra a un account --------------------------------------

def claims_team(user, team):
    """Se aprire ``team`` da un link o un codice la collega a ``user``.

    Solo la prima squadra di quell'account in quella lega: un admin che apre
    le squadre col codice (o un manager che ne ha già una lì) sta visitando,
    non prendendo. Un collegamento sbagliato teneva la squadra sull'account
    sbagliato per sempre.
    """
    from ..models import Participant
    from ..views.common import user_can_manage_scope

    if user_can_manage_scope(user, team.league):
        return False
    return not Participant.objects.filter(user=user, league_id=team.league_id).exists()


TAKEN_MESSAGE = ("Questa squadra è già collegata a un altro account. Se è la tua, entra con quell'account; "
                 "altrimenti chiedi al presidente della lega.")


def link_team(user, team):
    """Collega ``team`` all'account ``user`` se si può.

    Restituisce ``"linked"`` (collegata adesso), ``"mine"`` (lo era già),
    ``"taken"`` (è di un altro account: nessun cambio) o ``"visit"`` (l'account
    gestisce la lega o ha già una squadra lì: si entra senza collegare).
    """
    if team.user_id == user.id:
        return "mine"
    if team.user_id is not None:
        return "taken"
    if not claims_team(user, team):
        return "visit"
    now = timezone.now()
    team.user = user
    fields = ["user"]
    if team.invite_accepted_at is None:
        team.invite_accepted_at = now
        fields.append("invite_accepted_at")
    team.save(update_fields=fields)
    league = team.league
    if league is not None and league.first_team_joined_at is None:
        league.first_team_joined_at = now
        league.save(update_fields=["first_team_joined_at"])
    if league is not None:
        refresh_ready(league)
    return "linked"


def enter_team(request, team):
    """La sessione dell'app sulla squadra (chi gioca senza account, o subito
    dopo il collegamento)."""
    from ..views.common import SESSION_LEAGUE_KEY

    request.session["participant_id"] = team.id
    request.session["display_name"] = team.display_name
    if team.league_id is not None:
        request.session[SESSION_LEAGUE_KEY] = team.league_id


# --- Stato degli inviti ------------------------------------------------------

def mark_invite_sent(teams, channel):
    """Segna l'invito mandato (email, WhatsApp, link copiato, QR)."""
    now = timezone.now()
    teams = list(teams)
    for t in teams:
        t.invite_sent_at = now
        t.invite_last_channel = channel
        t.save(update_fields=["invite_sent_at", "invite_last_channel"])
    leagues = {t.league for t in teams if t.league_id}
    for league in leagues:
        if league.first_invite_at is None:
            league.first_invite_at = now
            league.save(update_fields=["first_invite_at"])


def mark_invite_opened(team):
    if team.invite_opened_at is None:
        team.invite_opened_at = timezone.now()
        team.save(update_fields=["invite_opened_at"])


def invite_status(team):
    """``(codice, testo)`` del badge della squadra nella pagina Squadre."""
    if team.user_id is not None or team.invite_accepted_at is not None:
        return "in", "Entrata ✓"
    if team.invite_opened_at is not None:
        return "open", f"Aperto il {timezone.localtime(team.invite_opened_at):%d/%m}"
    if team.invite_sent_at is not None:
        return "sent", f"Invitata il {timezone.localtime(team.invite_sent_at):%d/%m}"
    return "todo", "Da invitare"


def not_joined(league):
    """Le squadre attive della lega che non sono ancora entrate con un account."""
    from ..models import Participant

    return Participant.objects.filter(league=league, is_active=True, user__isnull=True)


def invite_text(team, link, sender=""):
    """Il messaggio da condividere per una squadra: solo il suo link."""
    who = f"{sender} ti ha invitato" if sender else "Sei invitato"
    league = team.league.name if team.league_id else "la lega"
    return (f"{who} in {league} su FantaManager, con la squadra {team.display_name}. "
            f"Entra da qui (il link è solo tuo, non inoltrarlo): {link}")


def invite_path(team):
    from django.urls import reverse

    return reverse("invite", args=[team.public_token])


# --- «Prepara la lega» --------------------------------------------------------

def setup_steps(league):
    """I passi della card «Prepara la lega», nell'ordine in cui si fanno.

    Ogni passo: ``key``, ``title``, ``done``, ``text``, ``optional``. Il primo
    non fatto è quello «adesso».
    """
    from ..models import Auction, MarketSession, Participant

    state = league.setup_state or {}
    teams = list(Participant.objects.filter(league=league, is_active=True))
    n = len(teams)
    joined = sum(1 for t in teams if t.user_id is not None)
    pending = n - joined
    has_auction = (Auction.objects.filter(league=league).exists()
                   or MarketSession.objects.filter(league=league).exists())
    steps = [
        {"key": "teams", "title": "Squadre", "done": n >= 2,
         "text": f"{n} squadr{'a' if n == 1 else 'e'} nella lega." if n else "Nessuna squadra: aggiungile."},
        {"key": "invites", "title": "Inviti", "done": n >= 2 and pending == 0,
         "text": f"Entrate {joined} su {n}." + (f" Mancano {pending}." if pending else ""),
         "joined": joined, "total": n, "pending": pending},
        {"key": "rules", "title": "Regole controllate", "done": bool(state.get("rules_checked")),
         "text": "Budget, rosa, scambi e punteggi: dai un'occhiata in Impostazioni."},
        {"key": "auction", "title": "Prima asta o mercato", "done": has_auction,
         "text": "Crea l'asta (o un mercato a buste) quando le squadre ci sono."},
        {"key": "coadmin", "title": "Co-admin", "done": league.admins.exists(), "optional": True,
         "text": "Facoltativo: qualcuno che ti aiuti a gestire la lega."},
    ]
    now_set = False
    for s in steps:
        s.setdefault("optional", False)
        s["now"] = False
        if not s["done"] and not s["optional"] and not now_set:
            s["now"] = now_set = True
    return steps


def setup_card(league):
    """Il contesto della card «Prepara la lega», o None se non serve più
    (tutto fatto o nascosta)."""
    if league is None:
        return None
    state = league.setup_state or {}
    if state.get("hidden"):
        return None
    steps = setup_steps(league)
    if all(s["done"] for s in steps if not s["optional"]):
        refresh_ready(league, steps)
        return None
    return {"league": league, "steps": steps,
            "done": sum(1 for s in steps if s["done"] and not s["optional"]),
            "total": sum(1 for s in steps if not s["optional"])}


def refresh_ready(league, steps=None):
    """Segna ``ready_at`` la prima volta che tutti i passi obbligatori sono fatti."""
    if league.ready_at is not None:
        return
    steps = steps or setup_steps(league)
    if all(s["done"] for s in steps if not s["optional"]):
        league.ready_at = timezone.now()
        league.save(update_fields=["ready_at"])


def set_setup_flag(league, key, value=True):
    state = dict(league.setup_state or {})
    state[key] = value
    league.setup_state = state
    league.save(update_fields=["setup_state"])
    refresh_ready(league)


# --- Misure per il Supervisor -------------------------------------------------

def funnel():
    """Leghe create, % con almeno un invito, % con metà delle squadre entrate,
    tempo mediano fino a «pronta». Solo dati del sito."""
    from statistics import median

    from ..models import League, Participant

    leagues = list(League.objects.all())
    total = len(leagues)
    if not total:
        return {"total": 0, "invited_pct": 0, "half_joined_pct": 0, "median_ready_hours": None, "by_via": []}
    invited = sum(1 for lg in leagues if lg.first_invite_at)
    counts = {}
    for row in Participant.objects.filter(is_active=True, league__isnull=False).values("league_id", "user_id"):
        c = counts.setdefault(row["league_id"], [0, 0])
        c[0] += 1
        c[1] += 1 if row["user_id"] else 0
    half = sum(1 for lg in leagues if counts.get(lg.id, [0, 0])[0] and counts[lg.id][1] * 2 >= counts[lg.id][0])
    ready_hours = [(lg.ready_at - lg.created_at).total_seconds() / 3600 for lg in leagues if lg.ready_at]
    vias = {}
    for lg in leagues:
        vias[lg.get_created_via_display() if lg.created_via else "Prima delle misure"] = \
            vias.get(lg.get_created_via_display() if lg.created_via else "Prima delle misure", 0) + 1
    return {
        "total": total,
        "invited_pct": round(100 * invited / total),
        "half_joined_pct": round(100 * half / total),
        "median_ready_hours": round(median(ready_hours), 1) if ready_hours else None,
        "ready": len(ready_hours),
        "by_via": sorted(vias.items(), key=lambda kv: -kv[1]),
    }


def leagues_of(user):
    """Le leghe di cui ``user`` è presidente o co-admin."""
    from ..models import League

    return League.objects.filter(Q(owner=user) | Q(admins=user)).distinct()
