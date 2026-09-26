"""League and league-level configuration models."""
from decimal import Decimal

from django.conf import settings
from django.db import models


class League(models.Model):
    """A fantasy league. Additive multi-league foundation.

    Existing single-league installs keep working: Auction/Participant FKs to
    League are nullable, so legacy rows simply have league=None and behave
    exactly as before. New leagues created via the wizard own their own
    participants, budget and roster slots.
    """
    name        = models.CharField(max_length=120, default="Lega")
    owner       = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="leagues",
    )
    source_site = models.CharField(max_length=40, blank=True)   # e.g. "fantapazz"
    external_id = models.CharField(max_length=60, blank=True)   # league id on source site

    budget   = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("500"))
    # Roster shape: either a cap per role ("limite slot per ruolo") or none at
    # all ("nessun limite slot"), where a team may buy any number of players in
    # any role and the only ceiling is its budget. The per-role numbers are kept
    # either way, so switching the mode back restores the previous caps.
    slot_limits = models.BooleanField(default=True)
    slots_p  = models.PositiveIntegerField(default=3)
    slots_d  = models.PositiveIntegerField(default=8)
    slots_c  = models.PositiveIntegerField(default=8)
    slots_a  = models.PositiveIntegerField(default=6)

    # Classic o Mantra. Non è una preferenza estetica: cambia quali ruoli ha un
    # giocatore (P/D/C/A contro Dc, M;C, W;A…), quale quotazione fa fede (il
    # listone ne porta due, diverse fra loro) e come si valida una formazione.
    # Default Classic, così ogni lega già esistente resta esattamente com'era.
    class GameMode(models.TextChoices):
        CLASSIC = "CLASSIC", "Classic"
        MANTRA  = "MANTRA",  "Mantra"

    game_mode = models.CharField(
        max_length=8, choices=GameMode.choices, default=GameMode.CLASSIC)

    # In Mantra la rosa non si conta per reparto: i ruoli sono dodici e quasi
    # tutti multipli, quindi "otto difensori" non vuol dire più niente. Si
    # contano i portieri e tutti gli altri insieme.
    slots_gk  = models.PositiveIntegerField(default=3)
    slots_out = models.PositiveIntegerField(default=22)

    # Scambi tra squadre: ammessi? e, se sì, serve la ratifica dell'admin?
    trades_enabled = models.BooleanField(default=True)
    trades_need_approval = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    @property
    def is_mantra(self):
        return self.game_mode == self.GameMode.MANTRA

    def slot_roles(self, role):
        """I ruoli classici che condividono un tetto con ``role``.

        In Classic ogni ruolo ha il suo ("D" pesa solo sugli otto difensori);
        in Mantra i tetti sono due — portieri e movimento — quindi comprare un
        difensore consuma lo stesso slot di comprare un attaccante. Chi conta
        le rose deve contare su questo insieme, non sul singolo ruolo.
        """
        if self.is_mantra:
            return ("P",) if role == "P" else ("D", "C", "A")
        return (role,)

    def slots_for(self, role):
        if not self.slot_limits:
            return 0
        if self.is_mantra:
            return self.slots_gk if role == "P" else self.slots_out
        return {"P": self.slots_p, "D": self.slots_d, "C": self.slots_c, "A": self.slots_a}.get(role, 0)

    @property
    def total_slots(self):
        if not self.slot_limits:
            return 0
        if self.is_mantra:
            return self.slots_gk + self.slots_out
        return self.slots_p + self.slots_d + self.slots_c + self.slots_a

    @property
    def slots_label(self):
        if not self.slot_limits:
            return "Nessun limite"
        if self.is_mantra:
            return f"{self.slots_gk} Por + {self.slots_out} mov."
        return f"{self.slots_p}/{self.slots_d}/{self.slots_c}/{self.slots_a}"

    def __str__(self):
        shape = "nessun limite slot" if not self.slot_limits else self.slots_label
        mode = " Mantra" if self.is_mantra else ""
        return f"{self.name}{mode} ({shape}, {self.budget})"


class LeagueConfig(models.Model):
    """Single league-wide configuration: roster slots per role + budget.

    Serve anche da memoria del form: crea una lega Mantra e la prossima parte
    gia' su Mantra, invece di farti rifare la stessa scelta ogni volta.
    """
    name     = models.CharField(max_length=120, default="Lega")
    budget   = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("500"))
    slot_limits = models.BooleanField(default=True)
    slots_p  = models.PositiveIntegerField(default=3)
    slots_d  = models.PositiveIntegerField(default=8)
    slots_c  = models.PositiveIntegerField(default=8)
    slots_a  = models.PositiveIntegerField(default=6)
    game_mode = models.CharField(max_length=8, default="CLASSIC")
    slots_gk  = models.PositiveIntegerField(default=3)
    slots_out = models.PositiveIntegerField(default=22)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def slots_for(self, role):
        return {"P": self.slots_p, "D": self.slots_d, "C": self.slots_c, "A": self.slots_a}.get(role, 0)

    @property
    def total_slots(self):
        return self.slots_p + self.slots_d + self.slots_c + self.slots_a

    def __str__(self):
        return f"{self.name} ({self.slots_p}P/{self.slots_d}D/{self.slots_c}C/{self.slots_a}A, {self.budget})"
