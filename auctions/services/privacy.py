"""Privacy e GDPR: consenso ai testi legali, verifica dell'email, diritti
dell'interessato (export, eliminazione), disiscrizione dalle email della lega,
conservazione dei dati e registro delle azioni sensibili.

Riassunto per chi gestisce l'istanza in docs/PRIVACY.md.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .. import legal

logger = logging.getLogger("auctions.privacy")

VERIFY_SALT = "fm.email-verify"
VERIFY_MAX_AGE = 48 * 3600
UNSUBSCRIBE_SALT = "fm.unsubscribe"


# --- Consenso ai testi legali ------------------------------------------------

def legal_required():
    """Consenso, email obbligatoria e riaccettazione: sul server sì, nell'app
    del PC (i dati restano su quel PC) no, salvo FM_LEGAL_REQUIRED."""
    forced = getattr(settings, "FM_LEGAL_REQUIRED", None)
    if forced is None:
        return not getattr(settings, "DESKTOP_APP", False)
    return bool(forced)


def record_acceptance(user, ip=None, docs=("privacy", "terms", "age")):
    """Salva l'accettazione delle versioni correnti (una riga per documento)."""
    from ..models import LegalAcceptance

    now = timezone.now()
    versions = {"privacy": legal.PRIVACY_VERSION, "terms": legal.TERMS_VERSION, "age": str(legal.MIN_AGE)}
    ip = ip if ip and ip != "unknown" else None
    LegalAcceptance.objects.bulk_create([
        LegalAcceptance(user=user, doc=doc, version=versions[doc], accepted_at=now, ip=ip) for doc in docs
    ])
    user._fm_legal_state = None


def acceptance_state(user):
    """``"ok"``, ``"outdated"`` (ha accettato una versione vecchia: si chiede di
    nuovo) o ``"missing"`` (account nato prima del consenso, o creato dall'admin
    per una squadra: un avviso lo invita, senza bloccare)."""
    from ..models import LegalAcceptance

    if user is None or not user.is_authenticated:
        return "ok"
    cached = getattr(user, "_fm_legal_state", None)   # una query per richiesta
    if cached is not None:
        return cached
    user._fm_legal_state = state = _acceptance_state(user)
    return state


def _acceptance_state(user):
    from ..models import LegalAcceptance

    latest = {}
    for doc, version in (LegalAcceptance.objects.filter(user=user, doc__in=legal.CURRENT)
                         .order_by("accepted_at", "id").values_list("doc", "version")):
        latest[doc] = version
    if not latest:
        return "missing"
    if any(latest.get(doc) != version for doc, version in legal.CURRENT.items()):
        return "outdated"
    return "ok"


# --- Stato dell'email ---------------------------------------------------------

def privacy_row(user):
    try:
        return user.privacy
    except (ObjectDoesNotExist, AttributeError):
        return None


def email_unverified(user):
    """True per chi si è registrato e non ha ancora confermato l'email. Gli
    account senza riga (nati prima, o creati dall'admin) valgono verificati."""
    row = privacy_row(user)
    return bool(row is not None and row.self_registered and user.email and row.email_verified_at is None)


# Gli account a cui il sito scrive (reset password, avvisi): tutti tranne chi
# si è registrato da solo e non ha ancora confermato l'email.
VERIFIED_Q = Q(privacy__isnull=True) | Q(privacy__self_registered=False) | Q(privacy__email_verified_at__isnull=False)


def _email_digest(email):
    # Nel link c'è solo un'impronta dell'indirizzo: il token firmato si legge
    # in chiaro (base64), e gli indirizzi finiscono nei log dei proxy.
    import hashlib

    return hashlib.sha256(f"{settings.SECRET_KEY}:{email.strip().lower()}".encode()).hexdigest()[:20]


def verify_token(user, email):
    return signing.dumps({"u": user.pk, "h": _email_digest(email)}, salt=VERIFY_SALT)


def check_verify_token(token):
    """``(user, impronta dell'email, error)``; ``error`` è "", "expired" o "invalid"."""
    try:
        data = signing.loads(token, salt=VERIFY_SALT, max_age=VERIFY_MAX_AGE)
    except signing.SignatureExpired:
        return None, "", "expired"
    except signing.BadSignature:
        return None, "", "invalid"
    if not isinstance(data, dict):
        return None, "", "invalid"
    user = get_user_model().objects.filter(pk=data.get("u"), is_active=True).first()
    if user is None or not data.get("h"):
        return None, "", "invalid"
    return user, data["h"], ""


def apply_verification(user, digest):
    """Conferma ``email`` per ``user``: quella attuale, o la nuova in attesa
    (cambio email). ``(messaggio, errore)``: l'errore se il link non
    corrisponde più a nessuna delle due."""
    from ..models import AccountPrivacy

    row, _ = AccountPrivacy.objects.get_or_create(user=user)
    now = timezone.now()
    if row.pending_email and _email_digest(row.pending_email) == digest:
        if get_user_model().objects.filter(email__iexact=row.pending_email).exclude(pk=user.pk).exists():
            return "", "Questa email è già usata da un altro account."
        old = user.email
        user.email = row.pending_email
        user.save(update_fields=["email"])
        # Le squadre che scrivevano al vecchio indirizzo dell'account seguono il nuovo.
        if old:
            user.teams.filter(email__iexact=old).update(email=row.pending_email)
        row.pending_email = ""
        row.email_verified_at = now
        row.save(update_fields=["pending_email", "email_verified_at"])
        return f"Email cambiata: ora usiamo {user.email}.", ""
    if user.email and _email_digest(user.email) == digest:
        if row.email_verified_at is None:
            row.email_verified_at = now
            row.save(update_fields=["email_verified_at"])
        return "Email confermata: grazie! Ora ricevi avvisi e puoi recuperare la password.", ""
    return "", "Questo link è per un'email che l'account non usa più: chiedine uno nuovo da «Il mio account»."


def send_verification(request, user, email=None):
    """Manda il link di conferma a ``email`` (default: quella dell'account).
    ``(ok, errore)``."""
    from . import mail

    email = email or user.email
    if not email:
        return False, "L'account non ha un'email."
    if not mail.is_ready():
        return False, "L'invio email non è attivo su questo sito: la conferma arriverà quando lo sarà."
    base = mail.link_base(request)
    if not base:
        return False, "Il sito non ha ancora un indirizzo pubblico per i link (FM_SITE_URL)."
    link = base + reverse("account_verify", args=[verify_token(user, email)])
    ctx = {"user": user, "link": link, "email": email, "hours": VERIFY_MAX_AGE // 3600,
           **legal_links(base)}
    return mail.send("Conferma la tua email — FantaManager", email,
                     render_to_string("auctions/email/verify_email.txt", ctx))


def legal_links(base):
    """Gli indirizzi completi di informativa e termini, per le email."""
    return {"privacy_url": base + reverse("privacy"), "terms_url": base + reverse("terms")}


# --- Registro delle azioni sensibili -----------------------------------------

def audit(actor, action, target_user=None, league=None, detail="", target_name=""):
    from ..models import AuditLog

    try:
        AuditLog.objects.create(
            actor=actor if actor is not None and actor.is_authenticated else None,
            actor_name=getattr(actor, "username", "") or "",
            action=action,
            target_user=target_user,
            target_name=target_name or (getattr(target_user, "username", "") if target_user else ""),
            league=league,
            detail=detail[:200],
        )
    except Exception:  # noqa: BLE001 — il registro non deve mai bloccare l'azione
        logger.exception("Riga di audit non scritta (%s)", action)


def audit_for(user):
    """Le righe del registro che riguardano ``user`` (fatte da lui o su di lui)."""
    from ..models import AuditLog

    return AuditLog.objects.filter(Q(target_user=user) | Q(actor=user)).select_related("league")


# --- Export dei dati (art. 15 e 20 GDPR) -------------------------------------

def _dt(value):
    return value.isoformat() if value else None


def export_user_data(user):
    """Tutto quello che il sito tiene su ``user``, come dizionario JSON.

    Solo i suoi dati: delle altre squadre compare al massimo il nome, dove
    serve a capire un'offerta o uno scambio; mai email o IP di altri.
    """
    from ..models import (
        AuditLog,
        Bid,
        LegalAcceptance,
        MarketBid,
        MatchdayFormation,
        Participant,
        SealedBid,
        Trade,
    )

    row = privacy_row(user)
    teams = list(Participant.objects.filter(user=user).select_related("league"))
    team_ids = [t.id for t in teams]
    data = {
        "generato_il": timezone.now().isoformat(),
        "account": {
            "username": user.username,
            "nome": user.first_name,
            "cognome": user.last_name,
            "email": user.email,
            "email_verificata_il": _dt(row.email_verified_at) if row else None,
            "registrato_il": _dt(user.date_joined),
            "ultimo_accesso": _dt(user.last_login),
            "leghe_che_gestisci": [
                {"lega": lg.name, "ruolo": "presidente" if lg.owner_id == user.id else "co-admin"}
                for lg in _managed_leagues(user)
            ],
        },
        "accettazioni_legali": [
            {"documento": a.doc, "versione": a.version, "data": _dt(a.accepted_at), "ip": a.ip}
            for a in LegalAcceptance.objects.filter(user=user)
        ],
        "squadre": [
            {"id": t.id, "nome": t.display_name, "lega": t.league.name if t.league else None,
             "email_della_squadra": t.email, "crediti": str(t.credits), "spesi": str(t.spent_credits),
             "attiva": t.is_active, "creata_il": _dt(t.created_at)}
            for t in teams
        ],
        "offerte_asta": [
            {"squadra": b.participant.display_name, "asta": b.auction.title, "lotto": b.cycle,
             "importo": str(b.amount), "accettata": b.accepted and not b.cancelled,
             "data": _dt(b.server_received_at), "ip": b.ip_address, "dispositivo": b.user_agent}
            for b in Bid.objects.filter(participant_id__in=team_ids).select_related("auction", "participant")
        ],
        "buste_asta": [
            {"squadra": s.participant.display_name, "asta": s.auction.title, "giro": s.round,
             "giocatore": s.player.name if s.player else None, "importo": str(s.amount),
             "data": _dt(s.updated_at)}
            for s in SealedBid.objects.filter(participant_id__in=team_ids)
            .select_related("auction", "participant", "player")
        ],
        "buste_mercato": [
            {"squadra": m.participant.display_name, "mercato": m.session.title,
             "giocatore": m.player.name if m.player else None, "importo": str(m.amount),
             "priorita": m.priority, "stato": m.status, "data": _dt(m.created_at)}
            for m in MarketBid.objects.filter(participant_id__in=team_ids)
            .select_related("session", "participant", "player")
        ],
        "formazioni": [
            {"squadra": f.participant.display_name, "giornata": f.giornata.number, "modulo": f.module,
             "titolari": f.starter_ids, "panchina": f.bench_ids, "capitano": f.captain_id,
             "vice": f.vice_id, "aggiornata_il": _dt(f.updated_at)}
            for f in MatchdayFormation.objects.filter(participant_id__in=team_ids)
            .select_related("participant", "giornata")
        ],
        "scambi_proposti": [
            {"da": t.proposer.display_name, "a": t.receiver.display_name, "stato": t.status,
             "messaggio": t.message, "crediti_offerti": str(t.proposer_credits),
             "crediti_chiesti": str(t.receiver_credits), "data": _dt(t.created_at)}
            for t in Trade.objects.filter(proposer_id__in=team_ids).select_related("proposer", "receiver")
        ],
        "registro_azioni": [
            {"azione": a.get_action_display(), "da": a.actor_name if a.actor_id == user.id else "admin",
             "su": a.target_name if a.target_user_id == user.id else "", "data": _dt(a.created_at),
             "dettaglio": a.detail}
            for a in AuditLog.objects.filter(Q(target_user=user) | Q(actor=user))
        ],
    }
    return data


def _managed_leagues(user):
    from ..models import League

    return League.objects.filter(Q(owner=user) | Q(admins=user)).distinct().order_by("name")


# --- Eliminazione dell'account (art. 17 GDPR) --------------------------------

def deletion_blockers(user):
    """Le leghe che impediscono di eliminare l'account: quelle di cui è il
    presidente e che hanno squadre attive di altre persone (o senza account).
    Vanno passate a un co-admin o eliminate prima."""
    from ..models import League, Participant

    blocking = []
    for league in League.objects.filter(owner=user).order_by("name"):
        others = Participant.objects.filter(league=league, is_active=True).exclude(user=user)
        if others.exists():
            blocking.append(league)
    return blocking


def delete_account(user):
    """Elimina l'account e lascia la storia della lega senza dati personali.

    - le squadre restano alla lega, senza account (``Participant.user``);
    - l'email della squadra si svuota se era quella dell'account;
    - offerte, buste e formazioni restano (servono alla lega) senza IP né
      dispositivo;
    - nel registro delle azioni il nome dell'account diventa anonimo.
    Restituisce il riepilogo, o solleva ``ValueError`` se ci sono leghe che lo
    impediscono (``deletion_blockers``).
    """
    from ..models import AuditLog, Bid, Participant

    if deletion_blockers(user):
        raise ValueError("leghe da passare a un altro presidente")
    email = (user.email or "").lower()
    username = user.username
    with transaction.atomic():
        teams = list(Participant.objects.filter(user=user))
        team_ids = [t.id for t in teams]
        for t in teams:
            fields = ["user"]
            t.user = None
            if email and t.email.lower() == email:
                t.email = ""
                fields.append("email")
            t.save(update_fields=fields)
        bids = Bid.objects.filter(participant_id__in=team_ids).exclude(ip_address__isnull=True, user_agent="")
        cleared = bids.update(ip_address=None, user_agent="")
        anon = "account eliminato"
        AuditLog.objects.filter(actor=user).update(actor_name=anon)
        AuditLog.objects.filter(target_user=user).update(target_name=anon)
        audit(None, "account_deleted", detail=f"{len(teams)} squadre scollegate", target_name=anon)
        user.delete()
    logger.info("Account eliminato su richiesta dell'interessato (id non più presente)")
    return {"username": username, "teams": len(teams), "bids_cleared": cleared}


# --- Disiscrizione dalle email della lega ------------------------------------

def unsubscribe_token(participant):
    # Firmato e senza scadenza: vale anche per le email vecchie, e non dipende
    # dal link d'accesso della squadra (che si può rigenerare).
    return signing.dumps({"p": participant.pk}, salt=UNSUBSCRIBE_SALT, compress=True)


def unsubscribe_url(base, participant):
    return base + reverse("email_unsubscribe", args=[unsubscribe_token(participant)])


def check_unsubscribe_token(token):
    """La squadra del link di disiscrizione, o None se il link non vale."""
    from ..models import Participant

    try:
        data = signing.loads(token, salt=UNSUBSCRIBE_SALT)
    except signing.BadSignature:
        return None
    p = Participant.objects.filter(pk=data.get("p")).select_related("league").first()
    return p


def unsubscribe(participant):
    """Niente più email della lega per questa squadra: l'indirizzo si azzera e
    il presidente lo vede nella pagina Squadre."""
    participant.email = ""
    participant.email_opt_out_at = timezone.now()
    participant.save(update_fields=["email", "email_opt_out_at"])
    audit(None, "unsubscribe", target_user=participant.user, league=participant.league,
          detail=f"Squadra {participant.display_name}", target_name=participant.display_name)


# --- Conservazione ------------------------------------------------------------

def cleanup(now=None, dry_run=False):
    """La pulizia di ogni giorno (comando ``privacy_cleanup``, scheduler).

    - IP e user-agent delle offerte più vecchie di FM_RETENTION_BID_IP_DAYS;
    - sessioni scadute;
    - account registrati da soli, mai verificati, senza squadre né leghe, più
      vecchi di FM_RETENTION_UNVERIFIED_DAYS (solo quelli nati con la verifica:
      gli account di prima non hanno la riga e non si toccano).
    I link di verifica e di reset password scadono da soli (sono firmati con
    una durata), quindi non c'è niente da cancellare per loro.
    """
    from django.contrib.sessions.models import Session

    from ..models import AccountPrivacy, Bid, League, Participant

    now = now or timezone.now()
    report = {"bids": 0, "sessions": 0, "accounts": 0}

    bid_cut = now - timedelta(days=getattr(settings, "FM_RETENTION_BID_IP_DAYS", 90))
    bids = Bid.objects.filter(server_received_at__lt=bid_cut).filter(
        Q(ip_address__isnull=False) | ~Q(user_agent__in=("", "busta", "regia")))
    report["bids"] = bids.count() if dry_run else bids.update(ip_address=None, user_agent="")

    sessions = Session.objects.filter(expire_date__lt=now)
    report["sessions"] = sessions.count() if dry_run else sessions.delete()[0]

    acc_cut = now - timedelta(days=getattr(settings, "FM_RETENTION_UNVERIFIED_DAYS", 30))
    stale = AccountPrivacy.objects.filter(self_registered=True, email_verified_at__isnull=True,
                                         created_at__lt=acc_cut,
                                         user__is_superuser=False, user__is_staff=False)
    user_ids = list(stale.values_list("user_id", flat=True))
    busy = set(Participant.objects.filter(user_id__in=user_ids).values_list("user_id", flat=True))
    busy |= set(League.objects.filter(owner_id__in=user_ids).values_list("owner_id", flat=True))
    busy |= set(League.admins.through.objects.filter(user_id__in=user_ids).values_list("user_id", flat=True))
    doomed = [uid for uid in user_ids if uid not in busy]
    if dry_run:
        report["accounts"] = len(doomed)
    elif doomed:
        report["accounts"] = get_user_model().objects.filter(pk__in=doomed).delete()[1].get(
            get_user_model()._meta.label, 0)
    return report
