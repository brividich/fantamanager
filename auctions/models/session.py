"""Saved auction sessions and checkpoints."""
from django.db import models


class AuctionSession(models.Model):
    """A named, durable snapshot of a league's standings at a point in time.

    Saving a session captures every participant (budget, spent, roster) plus the
    auction settings and cycle results into ``data`` (JSON). Resuming rebuilds a
    fresh League + Participants + roster ownership from that snapshot and creates
    a new Auction in ``RESUME_SAVED`` mode linked back here via
    ``Auction.resumed_from_session`` — non-destructive: the original rows are
    never mutated, so a save acts as a restorable checkpoint.
    """
    name        = models.CharField(max_length=120, default="Sessione")
    league      = models.ForeignKey(
        "League", null=True, blank=True, on_delete=models.SET_NULL, related_name="sessions"
    )
    source_auction = models.ForeignKey(
        "Auction", null=True, blank=True, on_delete=models.SET_NULL, related_name="saved_sessions"
    )
    mode         = models.CharField(max_length=20, blank=True)   # auction mode at save time
    current_cycle = models.PositiveIntegerField(default=1)
    created_by   = models.CharField(max_length=80, blank=True)   # admin username snapshot
    notes        = models.CharField(max_length=200, blank=True)
    data         = models.JSONField(default=dict)                # full snapshot payload
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.name} ({self.created_at:%Y-%m-%d %H:%M})"

    @property
    def participant_count(self):
        return len(self.data.get("participants", []))
