"""Audit trail of contract dice and changes (regolamento 4)."""
from django.db import models

from .league import League
from .participant import Participant
from .player import Player


class ContractEvent(models.Model):
    class Kind(models.TextChoices):
        CONTRACT    = "contract",    "Dado contratti"
        RENEWED     = "renewed",     "Rinnovo (dado verde)"
        RESCINDED   = "rescinded",   "Rescissione (dado rosso)"
        NOT_RENEWED = "not_renewed", "Non rinnovato"
        SET         = "set",         "Durata impostata dall'admin"
        SEASON      = "season",      "Nuova stagione"

    league = models.ForeignKey(League, on_delete=models.CASCADE, related_name="contract_events")
    player = models.ForeignKey(Player, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    player_name = models.CharField(max_length=120, blank=True)
    participant = models.ForeignKey(Participant, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    participant_name = models.CharField(max_length=80, blank=True)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    # Faccia uscita (1-4 per il dado contratti, 1 = verde / 0 = rosso per il
    # dado rinnovo) e anni risultanti dopo le soglie.
    roll = models.SmallIntegerField(null=True, blank=True)
    years = models.PositiveSmallIntegerField(null=True, blank=True)
    manual = models.BooleanField(default=False)
    by_admin = models.BooleanField(default=False)
    season = models.PositiveIntegerField(default=1)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.player_name} ({self.years or '-'})"
