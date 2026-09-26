"""Stagioni di lega: classifiche, fasi del tetto salariale, Decreto Salvacalcio."""
from decimal import Decimal

from django.db import models

from .league import League
from .participant import Participant


class LeagueRanking(models.Model):
    """Una classifica di lega fotografata in un momento della stagione."""

    class Kind(models.TextChoices):
        MIDSEASON = "mid",   "Metà stagione"
        FINAL     = "final", "Fine stagione"

    class Source(models.TextChoices):
        MANUAL = "manual", "Inserita dall'admin"
        APP    = "app",    "Classifica interna FantaManager"
        REMOTE = "remote", "Letta dal sito di lega"

    league = models.ForeignKey(League, on_delete=models.CASCADE, related_name="rankings")
    season = models.PositiveIntegerField()
    kind = models.CharField(max_length=5, choices=Kind.choices)
    source = models.CharField(max_length=6, choices=Source.choices, default=Source.MANUAL)
    # Id dei partecipanti in ordine di classifica (primo = indice 0).
    order = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-season", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["league", "season", "kind"], name="uniq_ranking_per_season_kind"),
        ]

    def position_of(self, participant_id):
        try:
            return self.order.index(participant_id) + 1
        except ValueError:
            return None


class CapPhase(models.Model):
    """Una fase di mercato (estiva / invernale) ai fini del tetto salariale."""

    class Kind(models.TextChoices):
        SUMMER = "summer", "Fase estiva"
        WINTER = "winter", "Fase invernale"

    league = models.ForeignKey(League, on_delete=models.CASCADE, related_name="cap_phases")
    season = models.PositiveIntegerField()
    kind = models.CharField(max_length=6, choices=Kind.choices)
    started_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(fields=["league", "season", "kind"], name="uniq_cap_phase"),
        ]


class CapEntry(models.Model):
    """Una voce del tetto salariale di una squadra in una stagione."""

    class Kind(models.TextChoices):
        BASE        = "base",     "Tetto base (classifica anno precedente)"
        WINTER      = "winter",   "Fondi invernali (classifica attuale)"
        LOST        = "lost",     "Bonus giocatori persi"
        RENEWALS    = "renewals", "Bonus rinnovi mancati"
        EXTRA       = "extra",    "Extra salary cap (budget convertito)"
        MANUAL      = "manual",   "Rettifica admin"

    phase = models.ForeignKey(CapPhase, on_delete=models.CASCADE, related_name="entries")
    participant = models.ForeignKey(Participant, on_delete=models.CASCADE, related_name="cap_entries")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class DecreeAward(models.Model):
    """Decreto Salvacalcio assegnato (una volta per stagione e tipo)."""

    league = models.ForeignKey(League, on_delete=models.CASCADE, related_name="decree_awards")
    season = models.PositiveIntegerField()
    kind = models.CharField(max_length=5, choices=LeagueRanking.Kind.choices)
    # [{"participant_id", "name", "position", "credits", "euro"}]
    details = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["league", "season", "kind"], name="uniq_decree_per_season_kind"),
        ]
