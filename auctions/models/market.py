"""Market session and sealed market bid models."""
from decimal import Decimal

from django.db import models
from django.utils import timezone

from .auction import Auction
from .league import League
from .participant import Participant
from .player import Player


class MarketSession(models.Model):
    """A transfer window / sealed-envelope market session (mercato di riparazione)."""

    class Status(models.TextChoices):
        DRAFT    = "draft",    "Bozza (non visibile ai partecipanti)"
        OPEN     = "open",     "Aperto (consegna buste in corso)"
        CLOSED   = "closed",   "Chiuso (in attesa di spoglio)"
        RESOLVED = "resolved", "Concluso (spoglio eseguito)"

    league = models.ForeignKey(
        League, on_delete=models.CASCADE, related_name="market_sessions"
    )
    title = models.CharField(max_length=200, default="Mercato di Riparazione")
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT
    )
    opens_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True)

    # Regole svincoli condizionati
    allow_conditional_release = models.BooleanField(default=True)
    release_refund_mode = models.CharField(
        max_length=10,
        choices=Auction.RefundMode.choices,
        default=Auction.RefundMode.PURCHASE,
    )

    # Tetti massimi di acquisti per reparto (0 = nessun limite)
    max_acquisitions_p = models.PositiveIntegerField(default=0)
    max_acquisitions_d = models.PositiveIntegerField(default=0)
    max_acquisitions_c = models.PositiveIntegerField(default=0)
    max_acquisitions_a = models.PositiveIntegerField(default=0)

    # Report dettagliato dello spoglio (vincitori, tagli, pareggi)
    results_summary = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"

    @property
    def is_open(self):
        if self.status != self.Status.OPEN:
            return False
        if self.closes_at is not None and timezone.now() >= self.closes_at:
            return False
        return True

    def max_for_role(self, role):
        field = f"max_acquisitions_{role.lower()}" if role else ""
        return getattr(self, field, 0)


class MarketBid(models.Model):
    """A sealed market bid on a free agent, with optional conditional release."""

    class Status(models.TextChoices):
        PENDING   = "pending",   "In attesa di spoglio"
        WON       = "won",       "Aggiudicato"
        LOST      = "lost",      "Non aggiudicato"
        TIED      = "tied",      "Pari merito (spareggio)"
        CANCELLED = "cancelled", "Annullato / Ritirato"

    session = models.ForeignKey(
        MarketSession, on_delete=models.CASCADE, related_name="bids"
    )
    participant = models.ForeignKey(
        Participant, on_delete=models.CASCADE, related_name="market_bids"
    )
    player = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name="market_bids"
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("1"))
    priority = models.PositiveIntegerField(default=1)

    # Calciatore da svincolare della propria rosa in caso di aggiudicazione
    release_player = models.ForeignKey(
        Player, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="released_in_market_bids",
    )

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING
    )
    note = models.CharField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "-amount", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "participant", "player"],
                name="uniq_market_bid_per_player",
            )
        ]

    def __str__(self):
        return f"{self.participant} -> {self.player.name} ({self.amount} FM, p{self.priority})"
