"""Impostazioni del voto algoritmico (``auctions/voto_algoritmico.py``).

Valgono per tutta la piattaforma e le modifica solo il superuser dalla pagina
del Supervisor. Ogni salvataggio è una nuova versione: la più recente è quella
in uso, le altre sono lo storico da cui si può ripristinare.
"""
from django.conf import settings
from django.db import models


class AlgoSettingsVersion(models.Model):
    # Solo le voci diverse dai default di ``ALGO_DEFAULTS`` (stessa forma: le
    # tabelle per ruolo come dict annidati).
    rules = models.JSONField(default=dict, blank=True)
    note = models.CharField(max_length=200, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"Voto algoritmico v{self.pk}"


class AlgoSample(models.Model):
    """Righe di partite (formato ``apifootball.fixture_player_rows``) su cui
    provare i parametri prima di salvarli."""

    class Source(models.TextChoices):
        APIFOOTBALL = "apifootball", "API-Football"

    name = models.CharField(max_length=120)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.APIFOOTBALL)
    rows = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.name


class AlgoReference(models.Model):
    """Voti di riferimento di una fonte esterna per un campione (una giornata):
    il file che il superuser si è procurato e ha caricato a mano.

    Solo materiale di taratura del voto algoritmico: lo vede solo il superuser
    sulla pagina del Supervisor e non entra mai nei voti delle giornate, nelle
    pagine di lega, nell'app o negli export. Nessun modello di lega punta qui.
    """
    sample = models.ForeignKey(AlgoSample, on_delete=models.CASCADE, related_name="references")
    # Etichetta libera della fonte, scritta dal superuser.
    label = models.CharField(max_length=80)
    filename = models.CharField(max_length=200, blank=True, default="")
    # Righe del file normalizzate: {name, team, role, vote} (vote stringa o None = s.v.).
    rows = models.JSONField(default=list, blank=True)
    # {"votes": {indice_riga_campione: voto|None}, "unmatched_file": [...],
    #  "unmatched_sample": [...], "ambiguous": [...]}
    matched = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sample_id", "label", "id"]

    def __str__(self):
        return f"{self.label} · {self.sample}"
