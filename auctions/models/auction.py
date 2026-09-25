"""Live auction models: Auction, AuctionQueueItem, AuctionCycleResult."""
from decimal import Decimal

from django.db import models
from django.utils import timezone

from .core import generate_public_token


class Auction(models.Model):
    class Status(models.TextChoices):
        DRAFT  = "DRAFT",  "Draft"
        READY  = "READY",  "Ready"
        LIVE   = "LIVE",   "Live"
        PAUSED = "PAUSED", "Paused"
        CLOSED = "CLOSED", "Closed"

    class Mode(models.TextChoices):
        NEW_FROM_ZERO    = "NEW_FROM_ZERO",    "Asta da zero"
        REPAIR_AUCTION   = "REPAIR_AUCTION",   "Asta di riparazione"
        CONTINUOUS_LEAGUE = "CONTINUOUS_LEAGUE", "Fantacalcio continuativo"
        RESUME_SAVED     = "RESUME_SAVED",     "Ripresa sessione salvata"

    class RefundMode(models.TextChoices):
        # Credits returned to a manager when a player is released (svincolo).
        PURCHASE = "purchase", "Costo di acquisto"          # Player.cost paid at auction
        CURRENT  = "current",  "Costo attuale (quotazione)" # Player.initial_price from the listone
        NONE     = "none",     "Nessun rimborso"

    class FlowMode(models.TextChoices):
        # How the auction steps from one player to the next.
        CALL       = "call",       "A chiamata"               # admin nominates each player manually
        CONTINUOUS = "continuous", "Asta continua"            # auto-advance with a preset timer that arms at once
        MANUAL     = "manual",     "Manuale (avanti/indietro)" # admin steps through the queue by hand

    class CallOrder(models.TextChoices):
        # The order in which queued free agents are presented.
        PDCA   = "pdca",   "Per ruolo P → D → C → A"
        ACDP   = "acdp",   "Per ruolo A → C → D → P"
        ALPHA  = "alpha",  "Alfabetico"
        RANDOM = "random", "Casuale"

    class WithinRole(models.TextChoices):
        # Tie-break used inside each role band (PDCA / ACDP only).
        QUOTA  = "quota",  "Quotazione (alto → basso)"
        ALPHA  = "alpha",  "Alfabetico"
        RANDOM = "random", "Casuale"
        # Regolamento "lettera estratta" (§3.1 B): per ogni ruolo si sorteggia
        # una lettera dell'alfabeto e da lì si legge il listone in ordine
        # alfabetico, tornando alla A dopo la Z. Le lettere sorteggiate finiscono
        # in ``Auction.drawn_letters`` così la sala può vederle.
        LETTER = "letter", "Alfabetico da lettera estratta"

    class ScreenSize(models.TextChoices):
        # How big a maxischermo element is drawn. A projector at the back of a
        # hall wants it large; a laptop screen does not.
        SMALL  = "s", "Piccolo"
        MEDIUM = "m", "Medio"
        LARGE  = "l", "Grande"

    class OpeningPriceMode(models.TextChoices):
        # Where the bidding for each player starts when it goes on the block.
        QUOTAZIONE = "quotazione", "Quotazione (valore attuale)"  # Player.initial_price
        BASE_ONE   = "base_one",   "Base 1"                       # fixed 1 credit

    class UnsoldPolicy(models.TextChoices):
        # What happens to a player lot that expires with no bids.
        ROLE_END    = "role_end",    "Fine reparto (alla fine del proprio ruolo)"
        AUCTION_END = "auction_end", "Giro di recupero (a fine asta, dopo tutti i ruoli)"
        DISCARD     = "discard",     "Invenduto definitivo (nessun secondo passaggio)"

    league      = models.ForeignKey(
        "League", null=True, blank=True, on_delete=models.SET_NULL, related_name="auctions"
    )
    # Set when this auction was created by resuming a saved session (§2/§7).
    resumed_from_session = models.ForeignKey(
        "AuctionSession", null=True, blank=True, on_delete=models.SET_NULL, related_name="resumed_auctions"
    )

    title       = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    # Creation mode + provenance (set by the wizard; legacy rows default to NEW_FROM_ZERO).
    mode             = models.CharField(max_length=20, choices=Mode.choices, default=Mode.NEW_FROM_ZERO)
    source_site      = models.CharField(max_length=40, blank=True)   # "fantapazz" / "excel" / ...
    source_league_id = models.CharField(max_length=60, blank=True)
    imported_at      = models.DateTimeField(null=True, blank=True)
    notes            = models.TextField(blank=True)

    starting_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    current_price  = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    min_increment  = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("1"))

    player = models.ForeignKey(
        "Player", null=True, blank=True, on_delete=models.SET_NULL, related_name="auctions"
    )

    quick_increments = models.CharField(max_length=100, default="10,50,100,500")
    duration_seconds = models.PositiveIntegerField(default=60)
    antisnipe_seconds = models.PositiveIntegerField(default=10)

    # The pause between one lot and the next: how long the result stays on the
    # screen ("venduto a X per 47") before the next player goes up. Long enough
    # to announce the sale and let everyone write it down; too long and the
    # evening drags. Only self-advancing flows run it — CALL and hand-stepped
    # MANUAL already wait for the admin. 0 = go straight on.
    #
    # Fractional on purpose: what separates "no pause" from "the briefest beat"
    # is well under a second, and the sleep this drives takes a float anyway.
    cycle_break_seconds = models.FloatField(default=4)

    # Roster/budget enforcement toggle (admin override). When True and the
    # bidder belongs to a League, bids are validated against per-role slots and
    # a 1-credit-per-empty-slot budget reserve. Legacy participants without a
    # League are never affected, regardless of this flag.
    enforce_limits = models.BooleanField(default=True)

    # Anti double-click: refuse a rilancio from whoever is already leading, so
    # a twitchy finger cannot bid against itself and inflate the price. Off
    # gives the old free-for-all back (some leagues raise their own bid on
    # purpose, to scare the room).
    block_leader_rebid = models.BooleanField(default=True)

    # How many credits a manager gets back when releasing (svincolo) a player.
    # Configurable both at creation (wizard) and later (auction settings).
    release_refund_mode = models.CharField(
        max_length=10, choices=RefundMode.choices, default=RefundMode.PURCHASE
    )

    # How the auction advances (call / continuous / manual), the order in which
    # queued free agents are presented, and the tie-break used inside each role.
    flow_mode = models.CharField(
        max_length=12, choices=FlowMode.choices, default=FlowMode.CALL
    )
    call_order = models.CharField(
        max_length=10, choices=CallOrder.choices, default=CallOrder.PDCA
    )
    within_role_order = models.CharField(
        max_length=10, choices=WithinRole.choices, default=WithinRole.QUOTA
    )
    unsold_policy = models.CharField(
        max_length=20, choices=UnsoldPolicy.choices, default=UnsoldPolicy.ROLE_END
    )

    # MANUAL flow only: normally a lot waits for the admin and the clock starts
    # on the first bid, so a player nobody wants sits there forever. With this
    # on, every lot goes up with the presentation timer already running and an
    # un-bid player expires and rolls on by itself — the avanti/indietro
    # controls stay available to override the run at any time.
    manual_auto_advance = models.BooleanField(default=False)

    # Size of the countdown dial and of the player name on the big screen
    # (display-only preferences, changed live from the console).
    screen_timer_size = models.CharField(
        max_length=1, choices=ScreenSize.choices, default=ScreenSize.MEDIUM
    )
    screen_name_size = models.CharField(
        max_length=1, choices=ScreenSize.choices, default=ScreenSize.MEDIUM
    )

    # Where bidding starts for each player put on the block: the player's
    # listone quotazione (valore attuale) or a fixed base of 1 credit.
    opening_price_mode = models.CharField(
        max_length=12, choices=OpeningPriceMode.choices, default=OpeningPriceMode.QUOTAZIONE
    )

    # --- Asta alle buste (regolamento §3.1 E) -------------------------------
    # Opzione: finché il prezzo resta sotto la soglia del ruolo si rilancia
    # "alle grida" come sempre; quando un rilancio la raggiunge il lotto passa
    # a scrutinio segreto — ognuno scrive una cifra non inferiore all'ultima
    # dichiarata +1, si aprono tutte insieme e vince la più alta. A parità si
    # ripete, a oltranza, finché resta un solo miglior offerente.
    sealed_bids = models.BooleanField(default=False)

    # Soglie per ruolo, in crediti. 0 = quel ruolo non va mai alle buste.
    # I default sono quelli del regolamento; sono modificabili come tutto il resto.
    sealed_threshold_p = models.PositiveIntegerField(default=50)
    sealed_threshold_d = models.PositiveIntegerField(default=50)
    sealed_threshold_c = models.PositiveIntegerField(default=100)
    sealed_threshold_a = models.PositiveIntegerField(default=150)

    # Quanto dura ogni scrutinio: il tempo per scrivere la propria busta.
    sealed_seconds = models.PositiveIntegerField(default=45)

    # Stato in corso (non è configurazione): 0 = nessuno scrutinio aperto,
    # 1 = primo giro di buste, 2+ = spareggi dopo un pari merito.
    sealed_round = models.PositiveIntegerField(default=0)
    # Cifra minima accettata in questo giro ("ultimo dichiarato +1").
    sealed_floor = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    sealed_ends_at = models.DateTimeField(null=True, blank=True)
    # Negli spareggi solo chi ha pareggiato può ripresentarsi: lista di id
    # Participant. Vuota al primo giro = aperto a tutti.
    sealed_contenders = models.JSONField(default=list, blank=True)
    # Le buste dell'ultimo giro risolto, aperte: [{"team": ..., "amount": ...}].
    # Serve al maxischermo per lo scrutinio pubblico; si azzera al lotto dopo.
    sealed_reveal = models.JSONField(default=list, blank=True)

    # Lettera estratta per ruolo quando l'ordine dentro al ruolo è LETTER:
    # {"P": "M", "D": "F", ...}. Riempita da build_queue.
    drawn_letters = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)

    current_cycle = models.PositiveIntegerField(default=1)

    # Unguessable token for sharing a read-only TV screen link publicly.
    public_token = models.CharField(max_length=64, blank=True, db_index=True)

    starts_at        = models.DateTimeField(null=True, blank=True)
    ends_at          = models.DateTimeField(null=True, blank=True)
    remaining_seconds = models.FloatField(null=True, blank=True)

    best_bid = models.ForeignKey(
        "Bid", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        # Assign a public token once, at creation. Guard with update_fields so
        # the hot bid path (save(update_fields=[...])) never re-touches it.
        if not self.public_token and kwargs.get("update_fields") is None:
            self.public_token = generate_public_token()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} ({self.status})"

    def allowed_increments(self):
        out = []
        for chunk in (self.quick_increments or "").split(","):
            chunk = chunk.strip()
            if chunk:
                try:
                    out.append(Decimal(chunk))
                except (ValueError, ArithmeticError):
                    continue
        return out

    def is_expired(self, now=None):
        now = now or timezone.now()
        return self.ends_at is not None and now >= self.ends_at

    def is_open_for_bids(self, now=None):
        now = now or timezone.now()
        return self.status == self.Status.LIVE and not self.is_expired(now)

    def remaining(self, now=None):
        now = now or timezone.now()
        if self.status == self.Status.PAUSED and self.remaining_seconds is not None:
            return max(0.0, float(self.remaining_seconds))
        if self.ends_at is None:
            return float(self.duration_seconds)
        return max(0.0, (self.ends_at - now).total_seconds())

    @property
    def by_role(self):
        """Whether the running order is grouped into per-role bands."""
        return self.call_order in (self.CallOrder.PDCA, self.CallOrder.ACDP)

    @property
    def is_ordered_flow(self):
        """True for the auto/stepped flows that consume a prebuilt queue."""
        return self.flow_mode in (self.FlowMode.CONTINUOUS, self.FlowMode.MANUAL)

    @property
    def auto_advances(self):
        """True when the running order rolls on without the admin touching it.

        Always for CONTINUOUS; for MANUAL only when ``manual_auto_advance`` is
        on. It governs both halves of the same behaviour: a lot goes up with the
        presentation timer already armed, and its expiry moves to the next
        queued player instead of parking on an empty block.
        """
        return (
            self.flow_mode == self.FlowMode.CONTINUOUS
            or (self.flow_mode == self.FlowMode.MANUAL and self.manual_auto_advance)
        )

    def sealed_threshold_for(self, role):
        """Soglia di passaggio alle buste per un ruolo, 0 se non si applica."""
        if not self.sealed_bids:
            return 0
        return {
            "P": self.sealed_threshold_p,
            "D": self.sealed_threshold_d,
            "C": self.sealed_threshold_c,
            "A": self.sealed_threshold_a,
        }.get(role, 0)

    @property
    def sealed_open(self):
        """True mentre è aperto uno scrutinio segreto su questo lotto."""
        return self.sealed_round > 0


class AuctionQueueItem(models.Model):
    """One player slot in an auction's running order (the "coda d'asta").

    The queue is built from the free-agent pool when the auction is set up, in
    the order dictated by ``Auction.call_order`` (P→D→C→A, A→C→D→P, alphabetical
    or random) with ``within_role_order`` breaking ties inside each role band.
    When ``call_order`` is role-grouped the run is banded P→D→C→A. ``order`` is the sort key
    (lower = sooner); leaving large gaps between role bands lets a released
    player be appended to the *end of its own role* without renumbering. Items
    are consumed (``done=True``) as each player reaches the block.
    """
    auction = models.ForeignKey(
        "Auction", on_delete=models.CASCADE, related_name="queue_items"
    )
    player  = models.ForeignKey("Player", on_delete=models.CASCADE, related_name="+")
    role    = models.CharField(max_length=1, blank=True)  # cached for grouping
    order   = models.IntegerField(default=0)
    done    = models.BooleanField(default=False)          # already auctioned
    # How many times this lot was offered and went unsold. Continuous play
    # re-queues an unsold player at the end of their role band; without a cap
    # the last unsold players of a band would re-offer each other forever and
    # the run would never reach the next role. Past the cap the item is parked
    # (left ``done``) and the admin can still call the player by hand.
    unsold_passes = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        unique_together = [("auction", "player")]

    def __str__(self):
        return f"#{self.order} {self.player.name} ({'done' if self.done else 'pending'})"


class AuctionCycleResult(models.Model):
    """Outcome of one auction cycle (one called player).

    Acts as the idempotency guard for assignment: there is at most one row per
    (auction, cycle), so the winning amount can never be charged twice even if
    both the auto-reset path and an admin close fire for the same cycle.
    """
    auction      = models.ForeignKey(Auction, on_delete=models.CASCADE, related_name="cycle_results")
    cycle        = models.PositiveIntegerField(default=1)

    winner       = models.ForeignKey(
        "Participant", null=True, blank=True, on_delete=models.SET_NULL, related_name="cycle_wins"
    )
    winner_name  = models.CharField(max_length=80, blank=True)   # snapshot
    amount       = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))

    # The player may be a pre-existing Player, or just a name called aloud.
    player       = models.ForeignKey(
        "Player", null=True, blank=True, on_delete=models.SET_NULL, related_name="cycle_results"
    )
    player_name  = models.CharField(max_length=120, blank=True)
    player_role  = models.CharField(max_length=1, blank=True)

    assigned     = models.BooleanField(default=False)
    assigned_at  = models.DateTimeField(null=True, blank=True)
    assigned_by  = models.CharField(max_length=80, blank=True)   # admin username snapshot / "auto"
    note         = models.CharField(max_length=200, blank=True)

    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["auction", "cycle"]
        constraints = [
            models.UniqueConstraint(fields=["auction", "cycle"], name="uniq_auction_cycle_result"),
        ]

    def __str__(self):
        who = self.winner_name or "—"
        return f"Asta {self.auction_id} ciclo {self.cycle}: {who} {self.amount}"
