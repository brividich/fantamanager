"""Trades (scambi) between two teams of the same league."""
from decimal import Decimal

from django.db import models

from .league import League
from .participant import Participant
from .player import Player


class Trade(models.Model):
    """A proposed swap of players and/or credits between two teams.

    Flow: the proposer sends it (PENDING); the receiver accepts or rejects.
    When the league requires ratification an accepted trade waits for the
    admin (ACCEPTED) who approves or vetoes it; otherwise it is executed on
    acceptance. Everything is re-validated at execution time.
    """

    class Status(models.TextChoices):
        PENDING   = "pending",   "In attesa di risposta"
        ACCEPTED  = "accepted",  "Accettato, in attesa di ratifica"
        COMPLETED = "completed", "Completato"
        REJECTED  = "rejected",  "Rifiutato"
        CANCELLED = "cancelled", "Annullato"
        VETOED    = "vetoed",    "Bocciato dall'admin"
        FAILED    = "failed",    "Non eseguibile"

    OPEN_STATUSES = (Status.PENDING, Status.ACCEPTED)

    league = models.ForeignKey(League, on_delete=models.CASCADE, related_name="trades")
    proposer = models.ForeignKey(
        Participant, on_delete=models.CASCADE, related_name="trades_proposed"
    )
    receiver = models.ForeignKey(
        Participant, on_delete=models.CASCADE, related_name="trades_received"
    )
    # Players each side gives away.
    proposer_players = models.ManyToManyField(Player, blank=True, related_name="+")
    receiver_players = models.ManyToManyField(Player, blank=True, related_name="+")
    # Credits each side gives away (a trade may carry credits one way).
    proposer_credits = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    receiver_credits = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))

    message = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    status_note = models.CharField(max_length=300, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Scambio #{self.pk} {self.proposer} ⇄ {self.receiver} ({self.get_status_display()})"

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES
