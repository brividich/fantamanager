"""Player and roster audit models."""
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import models


class RosterLog(models.Model):
    """Audit trail of every roster change (release / assign), admin or participant."""
    class Action(models.TextChoices):
        RELEASE       = "release",       "Svincolo"
        ASSIGN        = "assign",        "Assegnazione"
        ADMIN_RELEASE = "admin_release", "Svincolo (admin)"
        ADMIN_ASSIGN  = "admin_assign",  "Assegnazione (admin)"
        EDIT          = "edit",          "Modifica"
        TRADE         = "trade",         "Scambio"

    created_at    = models.DateTimeField(auto_now_add=True, db_index=True)
    participant   = models.ForeignKey(
        "Participant", null=True, blank=True, on_delete=models.SET_NULL, related_name="roster_logs"
    )
    participant_name = models.CharField(max_length=80, blank=True)  # snapshot
    player_name   = models.CharField(max_length=120)
    player_role   = models.CharField(max_length=1, blank=True)
    action        = models.CharField(max_length=20, choices=Action.choices)
    credits_delta = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    by_admin      = models.BooleanField(default=False)
    note          = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.participant_name} {self.action} {self.player_name}"


class Player(models.Model):
    class Role(models.TextChoices):
        P = "P", "Portiere"
        D = "D", "Difensore"
        C = "C", "Centrocampista"
        A = "A", "Attaccante"

    # The listone is per-league: each League owns its own player pool. Legacy
    # single-league installs (and all the pre-existing tests) leave this null,
    # which means "the global/legacy pool" — every pool query treats league=None
    # as that bucket, so nothing changes for them.
    league       = models.ForeignKey(
        "League", null=True, blank=True, on_delete=models.SET_NULL, related_name="players"
    )

    name         = models.CharField(max_length=120, db_index=True)
    role         = models.CharField(max_length=1, choices=Role.choices, default=Role.A)
    team         = models.CharField(max_length=80, blank=True)   # Serie A club code
    initial_price = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("1"))
    # Optional headshot URL shown on the big screen / bidder page. Empty falls
    # back to a role-coloured initials avatar — so it is purely additive.
    photo_url    = models.URLField(blank=True)
    # Official source player id (Fantacalcio "Id" column), kept so photo URLs
    # can be (re)generated from a template without re-importing the listone.
    ext_id       = models.CharField(max_length=30, blank=True, db_index=True)
    # Il calciatore reale dell'anagrafica comune (API-Football): lo stesso per
    # tutte le leghe. Vuoto finché non è stato riconosciuto.
    footballer   = models.ForeignKey(
        "Footballer", null=True, blank=True, on_delete=models.SET_NULL, related_name="league_players"
    )

    # Ruoli Mantra come li scrive il listone ufficiale nella colonna RM:
    # "Dc", "M;C", "B;Dd;E". Vuoto per i listoni importati prima del Mantra e
    # per le leghe Classic, che continuano a vivere sul solo ``role``.
    mantra_roles = models.CharField(max_length=40, blank=True)
    # Il listone porta due prezzi e due valori di mercato, uno per modalità: in
    # Mantra un terzino che gioca anche esterno alto vale diversamente. Nulli
    # quando il file non li aveva; ``price_for``/``fvm_for`` ricadono su quelli
    # Classic, così una lega Mantra con un listone vecchio funziona lo stesso.
    price_m      = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    fvm_m        = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)

    # Optional decision-support stats shown on the bidder page / big screen when
    # a player is on the block. All additive & nullable: an install that never
    # imports the "Statistiche" file simply shows the base card. ``fvm`` (fanta
    # market value) rides in on the Quotazioni listone; the rest come from the
    # separate season-stats file via ``import_stats``.
    fvm          = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    presences    = models.PositiveIntegerField(null=True, blank=True)   # Pv – partite a voto
    avg_vote     = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)  # Mv
    fanta_avg    = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)  # Fm
    goals        = models.IntegerField(null=True, blank=True)           # Gf
    assists      = models.IntegerField(null=True, blank=True)           # Ass

    # Roster ownership (imported from Fantapazz). Null = free agent / on the block.
    owner = models.ForeignKey(
        "Participant", null=True, blank=True, on_delete=models.SET_NULL, related_name="roster"
    )
    cost  = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0"))

    # --- Contratto di permanenza (regolamento 4) ---------------------------
    # Stagioni di contratto rimaste, compresa quella in corso: 1 = ultimo anno,
    # 0 = scaduto (da rinnovare), None = appena acquistato, dado da tirare.
    contract_years = models.PositiveSmallIntegerField(null=True, blank=True)
    # Dichiarazione di rinnovo a contratto scaduto: None = da decidere,
    # True = da rinnovare (tiro del dado rinnovo), False = lasciato andare.
    renewal_declared = models.BooleanField(null=True, blank=True)
    # Rescisso col dado rinnovo: la squadra che l'aveva non può ricomprarlo
    # alla prossima asta, e l'incasso di quell'asta va a lei (4.1).
    rescinded_from = models.ForeignKey(
        "Participant", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    # --- Usciti dalla Serie A (regolamento 5.05, 5.06, 5.09) --------------
    # Segnalato quando sparisce dal listone ufficiale mentre è in una rosa.
    left_serie_a_at = models.DateTimeField(null=True, blank=True)
    # Destinazione rilevata/indicata e posizione nel ranking (UEFA club o FIFA).
    left_club = models.CharField(max_length=120, blank=True)
    left_rank_kind = models.CharField(max_length=5, blank=True)  # "uefa" / "fifa" / "free"
    left_rank_pos = models.PositiveIntegerField(null=True, blank=True)
    # Lista ceduti temporanei (5.09): resta della squadra fino a fine contratto,
    # fuori dagli slot della rosa; alla fine vale una cessione all'estero.
    abroad_list = models.BooleanField(default=False)
    abroad_compensation = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)

    # Prestito (5.07): la squadra che ha il cartellino e quante sessioni d'asta
    # mancano al rientro (si scala a ogni apertura di fase estiva/invernale).
    loan_from = models.ForeignKey(
        "Participant", null=True, blank=True, on_delete=models.SET_NULL, related_name="loaned_out"
    )
    loan_sessions_left = models.PositiveSmallIntegerField(null=True, blank=True)
    # Quando è arrivato in rosa (acquisto) e quando ha rinnovato: servono ai
    # divieti "non si vende/svincola nella stessa sessione di mercato".
    acquired_at = models.DateTimeField(null=True, blank=True)
    renewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["role", "name"]

    @property
    def role_list(self):
        """I ruoli Mantra del giocatore, già spacchettati. Vuota se non ne ha."""
        from .. import mantra
        return mantra.parse_roles(self.mantra_roles)

    @property
    def roles_display(self):
        """Come si scrivono i ruoli Mantra a schermo: ``"Dd/Ds/E"``.

        Barra invece del punto e virgola del file: è la notazione che usa il
        regolamento per gli slot, e su una scheda si legge molto meglio.
        """
        return "/".join(self.role_list)

    @property
    def audio_url(self):
        """URL dell'annuncio vocale generato (XTTS), se presente per questo
        giocatore. I file vivono in ``media/player_audio/{ext_id}.wav``."""
        if not self.ext_id:
            return ""
        path = Path(settings.MEDIA_ROOT) / "player_audio" / f"{self.ext_id}.wav"
        if not path.exists():
            return ""
        return f"{settings.MEDIA_URL}player_audio/{self.ext_id}.wav"

    def price_for(self, league=None):
        """Prezzo di partenza nella modalità della lega, con ricaduta su Classic."""
        lg = league if league is not None else self.league
        if lg is not None and lg.is_mantra and self.price_m is not None:
            return self.price_m
        return self.initial_price

    def fvm_for(self, league=None):
        lg = league if league is not None else self.league
        if lg is not None and lg.is_mantra and self.fvm_m is not None:
            return self.fvm_m
        return self.fvm

    def __str__(self):
        return f"{self.name} ({self.roles_display or self.role} – {self.team})"
