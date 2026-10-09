"""Participant (team) and watchlist models."""
import secrets
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.db.models import Q

from .core import generate_public_token

# Team codes are typed by hand, so no 0/O or 1/I/L to confuse.
ACCESS_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ACCESS_CODE_LENGTH = 8
MIN_CUSTOM_CODE_LENGTH = 6
AMBIGUOUS_CODE_MESSAGE = ("Questo codice è usato da più squadre: entra con il link della tua squadra "
                          "o chiedi all'organizzatore un codice nuovo.")


def access_code_taken(code, exclude_pk=None):
    """True when another team, in any league, already answers to ``code``: the
    login looks codes up across leagues, so two equal ones would be a coin toss."""
    qs = Participant.objects.filter(access_code__iexact=code)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


def generate_access_code():
    """A fresh random team code, unique across every league."""
    while True:
        code = "".join(secrets.choice(ACCESS_CODE_ALPHABET) for _ in range(ACCESS_CODE_LENGTH))
        if not access_code_taken(code):
            return code


def custom_code_error(code, exclude_pk=None):
    """Why a code chosen by an admin can't be used ("" when it can)."""
    if len(code) < MIN_CUSTOM_CODE_LENGTH:
        return f"Il codice squadra deve avere almeno {MIN_CUSTOM_CODE_LENGTH} caratteri."
    if access_code_taken(code, exclude_pk):
        return "Questo codice squadra è già usato da un'altra squadra: scegline un altro."
    return ""


def find_team_by_code(code):
    """``(team, ambiguous)`` for a typed code or a join token. Codes chosen
    before they were unique may repeat across leagues: then nobody gets in on
    the code alone (``ambiguous``), never a team picked at random."""
    code = (code or "").strip()
    if not code:
        return None, False
    matches = list(Participant.objects.filter(
        Q(access_code__iexact=code) | Q(public_token=code), is_active=True,
    )[:2])
    if len(matches) > 1:
        return None, True
    return (matches[0] if matches else None), False


class Participant(models.Model):
    league        = models.ForeignKey(
        "League", null=True, blank=True, on_delete=models.SET_NULL, related_name="participants"
    )
    user          = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="teams"
    )
    # External team identity (when imported from a provider such as Fantapazz).
    external_team_id = models.CharField(max_length=60, blank=True)
    display_name  = models.CharField(max_length=80)
    access_code   = models.CharField(max_length=20, blank=True, db_index=True)
    # Where the league writes to the team (invites, market notices). An
    # account linked to the team has its own email: this one wins when set.
    email         = models.EmailField(blank=True)
    # La squadra ha chiesto di non ricevere più email dalla lega (link nelle
    # email): l'indirizzo è stato tolto e quello dell'account collegato non si
    # usa più finché il presidente non ne scrive uno nuovo.
    email_opt_out_at = models.DateTimeField(null=True, blank=True)
    # Strong, unguessable join credential (the human-friendly access_code stays
    # for manual entry; this token backs shareable join links).
    public_token  = models.CharField(max_length=64, blank=True, db_index=True)
    credits       = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("500"))
    spent_credits = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    logo          = models.ImageField(upload_to="logos/", blank=True, null=True)
    is_active     = models.BooleanField(default=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    # --- Invito ------------------------------------------------------------
    # A che punto è l'invito della squadra (pagina Squadre): mandato quando e
    # come, aperto (prima visita a /invito/<token>/), accettato (account
    # collegato). Solo per il presidente: non cambia niente del gioco.
    class InviteChannel(models.TextChoices):
        EMAIL = "email", "Email"
        WHATSAPP = "whatsapp", "WhatsApp"
        SHARE = "share", "Condividi"
        COPY = "copy", "Link copiato"
        QR = "qr", "QR"

    invite_sent_at      = models.DateTimeField(null=True, blank=True)
    invite_last_channel = models.CharField(max_length=10, choices=InviteChannel.choices, blank=True)
    invite_opened_at    = models.DateTimeField(null=True, blank=True)
    invite_accepted_at  = models.DateTimeField(null=True, blank=True)

    # --- Scheda squadra ----------------------------------------------------
    # L'intestazione della scheda che la lega si passa prima dell'asta:
    # testo libero, niente di tutto questo tocca crediti, rose o regole.
    # Sigla per gli elenchi di lega ("S. VIAFONDA"): vuota = nome intero.
    short_name     = models.CharField(max_length=30, blank=True)
    founded        = models.PositiveSmallIntegerField(null=True, blank=True)
    president_name = models.CharField(max_length=80, blank=True)
    coach_name     = models.CharField(max_length=80, blank=True)
    stadium        = models.CharField(max_length=80, blank=True)
    stadium_capacity = models.PositiveIntegerField(null=True, blank=True)
    # Palmarès come coppie [voce, numero] nell'ordine della scheda: le coppe
    # hanno il nome della lega ("COPPE LUGNANESI"), quindi le voci sono dati.
    honours        = models.JSONField(default=list, blank=True)
    kit_home       = models.ImageField(upload_to="kits/", blank=True, null=True)
    kit_away       = models.ImageField(upload_to="kits/", blank=True, null=True)

    def save(self, *args, **kwargs):
        if not self.public_token and kwargs.get("update_fields") is None:
            self.public_token = generate_public_token()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.display_name

    @property
    def contact_email(self):
        """The address the league's emails go to ("" when there is none)."""
        if self.email:
            return self.email
        if self.email_opt_out_at is not None:
            return ""
        user = self.user if self.user_id else None
        if user is None or not user.email:
            return ""
        # Un'email registrata e non ancora confermata non riceve avvisi.
        try:
            privacy = user.privacy
        except ObjectDoesNotExist:
            privacy = None
        if privacy is not None and privacy.self_registered and privacy.email_verified_at is None:
            return ""
        return user.email

    @property
    def remaining_credits(self):
        return max(Decimal("0"), self.credits - self.spent_credits)


class ManagedAccount(models.Model):
    """A portal login the league admin created for a coach from the Squadre page.

    The record is the admin's licence to keep managing the account later
    (password, username, email, on/off): an account the coach registered on
    their own carries no such record, so a league admin cannot link somebody
    else's login to a team of theirs and then reset its password. Superusers
    manage every account and do not need it.
    """
    user       = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="managed_account"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} (creato da {self.created_by or '—'})"


class Watch(models.Model):
    """A player a manager is tracking pre-auction ("obiettivo") + a mental cap.

    Purely a private planning aid for one participant: it never affects bids,
    prices or ownership. ``max_price`` is the manager's self-noted ceiling (the
    bidder page warns when the live price passes it); it is optional.
    """
    participant = models.ForeignKey(
        "Participant", on_delete=models.CASCADE, related_name="watches"
    )
    player      = models.ForeignKey("Player", on_delete=models.CASCADE, related_name="+")
    max_price   = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    note        = models.CharField(max_length=120, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["player__role", "player__name"]
        unique_together = [("participant", "player")]

    def __str__(self):
        return f"{self.participant.display_name} 🎯 {self.player.name}"


class CoAdminInvite(models.Model):
    """Un invito a fare il co-admin di una lega (``/invito-admin/<token>/``).

    Chi lo apre entra con il suo account (o se ne crea uno) e finisce in
    ``league.admins``. Il token vale una volta: dopo l'accettazione, o se il
    presidente lo ritira, non apre più niente.
    """
    league      = models.ForeignKey("League", on_delete=models.CASCADE, related_name="coadmin_invites")
    email       = models.EmailField(blank=True)
    token       = models.CharField(max_length=64, unique=True)
    created_by  = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    created_at  = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    revoked     = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_open(self):
        return self.accepted_at is None and not self.revoked

    def __str__(self):
        return f"co-admin {self.league} → {self.email or '(link)'}"
