"""Bid and sealed bid models."""
from decimal import Decimal

from django.db import models
from django.utils import timezone

from .auction import Auction
from .participant import Participant
from .player import Player


class Bid(models.Model):
    auction     = models.ForeignKey(Auction, on_delete=models.CASCADE, related_name="bids")
    participant = models.ForeignKey(Participant, on_delete=models.CASCADE, related_name="bids")

    amount    = models.DecimalField(max_digits=12, decimal_places=2)
    increment = models.DecimalField(max_digits=12, decimal_places=2)

    server_received_at = models.DateTimeField(default=timezone.now, db_index=True)

    accepted         = models.BooleanField(default=False)
    rejection_reason = models.CharField(max_length=120, blank=True)

    cancelled        = models.BooleanField(default=False)
    cancelled_at     = models.DateTimeField(null=True, blank=True)
    cancelled_reason = models.CharField(max_length=120, blank=True)

    cycle            = models.PositiveIntegerField(default=1)
    remaining_at_bid = models.FloatField(null=True, blank=True)

    user_agent = models.CharField(max_length=300, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-server_received_at", "-id"]
        indexes  = [models.Index(fields=["auction", "accepted", "cancelled"])]

    def __str__(self):
        state = "OK" if (self.accepted and not self.cancelled) else "REJECTED"
        return f"{self.participant} {self.amount} [{state}]"


class SealedBid(models.Model):
    """Una busta: l'offerta segreta di una squadra in un giro di scrutinio.

    Resta segreta finché il giro non si chiude — nessuna vista la manda in
    giro prima di allora — e una squadra ne ha al massimo una per giro, che
    può riscrivere finché il tempo non scade (l'ultima parola è quella che
    conta, come cambiare il foglietto prima di consegnarlo).
    """
    auction     = models.ForeignKey(Auction, on_delete=models.CASCADE, related_name="sealed_bids_set")
    participant = models.ForeignKey(Participant, on_delete=models.CASCADE, related_name="sealed_bids")
    player      = models.ForeignKey(
        Player, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    cycle       = models.PositiveIntegerField(default=1)
    round       = models.PositiveIntegerField(default=1)
    amount      = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-amount", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["auction", "cycle", "round", "participant"],
                name="uniq_sealed_bid_per_round",
            ),
        ]

    def __str__(self):
        return f"busta {self.participant} {self.amount} (giro {self.round})"
