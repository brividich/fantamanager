"""Anagrafica dei calciatori reali, unica per tutto FantaManager."""
from django.db import models


class Footballer(models.Model):
    """Un calciatore vero, come lo conosce API-Football.

    È lo stesso per tutte le leghe: si aggiorna una volta sola dalle rose dei
    club di Serie A (``services.footballers.sync_registry``) e ogni lega vi
    collega i giocatori del proprio listone (``Player.footballer``). Proprietà,
    costo e contratto restano sul ``Player`` della lega.
    """
    class Position(models.TextChoices):
        P = "P", "Portiere"
        D = "D", "Difensore"
        C = "C", "Centrocampista"
        A = "A", "Attaccante"

    api_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=120, db_index=True)
    position = models.CharField(max_length=1, choices=Position.choices, blank=True)
    age = models.PositiveSmallIntegerField(null=True, blank=True)
    number = models.PositiveSmallIntegerField(null=True, blank=True)
    photo_url = models.URLField(blank=True)

    # Il club dell'ultima rosa in cui è comparso.
    club_api_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    club_name = models.CharField(max_length=120, blank=True)
    club_logo = models.URLField(blank=True)
    # In una rosa di Serie A all'ultimo aggiornamento completo.
    in_serie_a = models.BooleanField(default=True, db_index=True)

    seen_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.club_name})" if self.club_name else self.name
