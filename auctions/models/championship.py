"""Championship and post-auction gameplay models: Season, Giornata, Performance, Lineup/Formation."""
from decimal import Decimal

from django.db import models


class Formation(models.Model):
    """A manager's last saved lineup: a module + chosen starters (by slot order).

    The template for the next giornata: each giornata keeps its own copy in
    ``MatchdayFormation``, frozen when the giornata starts. ``starter_ids`` is
    an ordered list of Player ids laid out P → D… → C… → A… to match
    ``module``'s slots (None for an empty slot). ``bench_ids`` is the bench
    order the manager chose (the substitution priority); owned players missing
    from both lists go to the end of the bench.
    """
    participant = models.OneToOneField(
        "Participant", on_delete=models.CASCADE, related_name="formation"
    )
    module      = models.CharField(max_length=10, default="4-3-3")
    starter_ids = models.JSONField(default=list, blank=True)
    bench_ids   = models.JSONField(default=list, blank=True)
    # Captain and vice: Player ids among the starters (the vice takes the
    # armband when the captain gets no vote). Null = none chosen.
    captain_id  = models.PositiveIntegerField(null=True, blank=True)
    vice_id     = models.PositiveIntegerField(null=True, blank=True)
    updated_at  = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.participant_id}: {self.module}"


class Season(models.Model):
    """A playable championship for a league: the frame the post-auction game
    hangs off (giornate, lineups, scores). One league can run several over time;
    ``is_current`` marks the active one. ``rules`` overrides scoring defaults."""
    league     = models.ForeignKey(
        "League", null=True, blank=True, on_delete=models.CASCADE, related_name="seasons"
    )
    name       = models.CharField(max_length=120, default="Stagione")
    matchdays  = models.PositiveIntegerField(default=38)
    rules      = models.JSONField(default=dict, blank=True)   # scoring overrides
    is_current = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.league_id})"


class Competition(models.Model):
    """A specific tournament or championship within a Season.
    Supports classic 1vs1 round robin, total points (formula 1),
    Battle Royale, knockout cup brackets, groups + playoffs,
    Apertura/Clausura splits, and Supercoppa.
    """
    class Type(models.TextChoices):
        ROUND_ROBIN     = "ROUND_ROBIN",     "Campionato (Scontri Diretti 1vs1)"
        TOTAL_POINTS    = "TOTAL_POINTS",    "Gran Premio (Somma Punti)"
        FORMULA_1       = "FORMULA_1",       "Formula 1 (Punti GP di Giornata)"
        SURVIVAL        = "SURVIVAL",        "Survival Cup (L'Uomo Morto)"
        SWISS_LEAGUE    = "SWISS_LEAGUE",    "Nuova Champions a Girone Svizzero"
        FANTA_DAVIS     = "FANTA_DAVIS",     "Fanta-Davis a Coppie"
        KNOCKOUT        = "KNOCKOUT",        "Coppa a Eliminazione (Tabellone)"
        GROUPS_KNOCKOUT = "GROUPS_KNOCKOUT", "Coppa a Gironi + Fase Finale"
        SEASON_SPLIT    = "SEASON_SPLIT",    "Torneo a Fasi (Apertura / Clausura)"
        SUPERCOPPA      = "SUPERCOPPA",      "Supercoppa di Lega (Sfida Secca)"
        BATTLE_ROYALE   = "BATTLE_ROYALE",   "Battle Royale (Tutti contro Tutti)"

    season     = models.ForeignKey(Season, on_delete=models.CASCADE, related_name="competitions")
    name       = models.CharField(max_length=120)
    kind       = models.CharField(max_length=20, choices=Type.choices, default=Type.ROUND_ROBIN)
    is_active  = models.BooleanField(default=True)
    settings   = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.name} ({self.get_kind_display()})"


class Giornata(models.Model):
    """One matchday. Lineups are editable while OPEN; LOCKED freezes them (voti
    get entered); SCORED means scores + fixtures have been computed."""
    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED", "In programma"
        OPEN      = "OPEN",      "Formazioni aperte"
        LOCKED    = "LOCKED",    "Bloccata"
        LIVE      = "LIVE",      "Live in corso"
        SCORED    = "SCORED",    "Calcolata"

    season     = models.ForeignKey(Season, on_delete=models.CASCADE, related_name="giornate")
    number     = models.PositiveIntegerField()
    serie_a_matchday = models.PositiveIntegerField(null=True, blank=True)
    status     = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED)
    # Deadline for the lineups: the scheduler locks them when it passes (the
    # first Serie A kick-off of the round, or a time the admin chose).
    starts_at  = models.DateTimeField(null=True, blank=True)
    locked_at  = models.DateTimeField(null=True, blank=True)
    scored_at  = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["season", "number"]
        constraints = [
            models.UniqueConstraint(fields=["season", "number"], name="uniq_season_giornata"),
        ]

    def __str__(self):
        return f"G{self.number} · {self.season_id}"


class PlayerPerformance(models.Model):
    """A player's raw stat line for one giornata. ``vote`` None = senza voto
    (didn't play / no grade) — a substitution trigger. Fantavoto is derived by
    the scoring engine, never stored here."""
    giornata        = models.ForeignKey(Giornata, on_delete=models.CASCADE, related_name="performances")
    player          = models.ForeignKey("Player", on_delete=models.CASCADE, related_name="performances")
    vote            = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    goals           = models.PositiveSmallIntegerField(default=0)
    assists         = models.PositiveSmallIntegerField(default=0)
    own_goals       = models.PositiveSmallIntegerField(default=0)
    pen_scored      = models.PositiveSmallIntegerField(default=0)   # informativo; già nei goals
    pen_missed      = models.PositiveSmallIntegerField(default=0)
    pen_saved       = models.PositiveSmallIntegerField(default=0)
    goals_conceded  = models.PositiveSmallIntegerField(default=0)   # portiere
    yellow          = models.BooleanField(default=False)
    red             = models.BooleanField(default=False)
    is_live         = models.BooleanField(default=False)
    live_source     = models.CharField(max_length=40, blank=True, default="")
    live_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["giornata", "player"], name="uniq_giornata_player_perf"),
        ]

    def as_perf(self):
        """Dict shape the scoring engine consumes."""
        return {
            "vote": self.vote, "goals": self.goals, "assists": self.assists,
            "own_goals": self.own_goals, "pen_missed": self.pen_missed,
            "pen_saved": self.pen_saved, "goals_conceded": self.goals_conceded,
            "yellow": self.yellow, "red": self.red,
        }

    def __str__(self):
        return f"{self.player_id} G? {self.vote}"


class GiornataScore(models.Model):
    """A manager's computed result for a giornata: fantapunti, converted goals,
    and a JSON breakdown (per-slot lines + subs) for the Live/Lega views."""
    giornata     = models.ForeignKey(Giornata, on_delete=models.CASCADE, related_name="scores")
    participant  = models.ForeignKey("Participant", on_delete=models.CASCADE, related_name="giornata_scores")
    total        = models.DecimalField(max_digits=6, decimal_places=1, default=Decimal("0"))
    goals        = models.PositiveSmallIntegerField(default=0)
    modificatore = models.DecimalField(max_digits=4, decimal_places=1, default=Decimal("0"))
    breakdown    = models.JSONField(default=dict, blank=True)
    computed_at  = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["giornata", "participant"], name="uniq_giornata_participant_score"),
        ]

    def __str__(self):
        return f"{self.participant_id} G{self.giornata_id}: {self.total} ({self.goals})"


class Fixture(models.Model):
    """A head-to-head match in a giornata (home vs away team). Goals come from
    each side's GiornataScore.goals; points are 3/1/0. A bye (odd team count)
    is a fixture with ``away`` null."""
    giornata    = models.ForeignKey(Giornata, on_delete=models.CASCADE, related_name="fixtures")
    competition = models.ForeignKey(
        Competition, null=True, blank=True, on_delete=models.CASCADE, related_name="fixtures"
    )
    stage       = models.CharField(max_length=60, blank=True)
    home        = models.ForeignKey("Participant", on_delete=models.CASCADE, related_name="home_fixtures")
    away        = models.ForeignKey("Participant", null=True, blank=True,
                                    on_delete=models.CASCADE, related_name="away_fixtures")
    home_goals  = models.PositiveSmallIntegerField(default=0)
    away_goals  = models.PositiveSmallIntegerField(default=0)
    home_points = models.PositiveSmallIntegerField(default=0)
    away_points = models.PositiveSmallIntegerField(default=0)
    # Fantapunti each side played the match with: the team's giornata total
    # plus the competition's home bonus for the home side.
    home_total  = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    away_total  = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    computed    = models.BooleanField(default=False)

    class Meta:
        ordering = ["giornata", "id"]

    def __str__(self):
        return f"G{self.giornata_id}: {self.home_id} vs {self.away_id}"


class MatchdayFormation(models.Model):
    """Lineup for a specific participant in a specific matchday (Giornata).
    Stores module, chosen starters, and ordered bench players.
    Allows matchday-by-matchday history and administrative adjustments.
    """
    giornata    = models.ForeignKey(Giornata, on_delete=models.CASCADE, related_name="matchday_formations")
    participant = models.ForeignKey("Participant", on_delete=models.CASCADE, related_name="matchday_formations")
    module      = models.CharField(max_length=10, default="4-3-3")
    starter_ids = models.JSONField(default=list, blank=True)
    bench_ids   = models.JSONField(default=list, blank=True)
    captain_id  = models.PositiveIntegerField(null=True, blank=True)
    vice_id     = models.PositiveIntegerField(null=True, blank=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["giornata", "participant"]
        constraints = [
            models.UniqueConstraint(fields=["giornata", "participant"], name="uniq_giornata_participant_formation"),
        ]

    def __str__(self):
        return f"{self.participant_id} · G{self.giornata_id}: {self.module}"
