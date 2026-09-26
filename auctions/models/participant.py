"""Participant (team) and watchlist models."""
from decimal import Decimal

from django.conf import settings
from django.db import models

from .core import generate_public_token


class Participant(models.Model):
    league        = models.ForeignKey(
        "League", null=True, blank=True, on_delete=models.SET_NULL, related_name="participants"
    )
    user          = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="teams"
    )
    # External team identity (when imported from a provider such as Fantapazz).
    external_team_id = models.CharField(max_length=60, blank=True)
    display_name  = models.CharField(max_length=80)
    access_code   = models.CharField(max_length=20, blank=True, db_index=True)
    # Strong, unguessable join credential (the human-friendly access_code stays
    # for manual entry; this token backs shareable join links).
    public_token  = models.CharField(max_length=64, blank=True, db_index=True)
    credits       = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("500"))
    spent_credits = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    logo          = models.ImageField(upload_to="logos/", blank=True, null=True)
    is_active     = models.BooleanField(default=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.public_token and kwargs.get("update_fields") is None:
            self.public_token = generate_public_token()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.display_name

    @property
    def remaining_credits(self):
        return max(Decimal("0"), self.credits - self.spent_credits)


class ManagedAccount(models.Model):
    """A portal login the league admin created for a coach from the Squadre page.

    The record is the admin's licence to keep managing the account later
    (password, username, email, on/off): an account the coach registered on
    their own carries no such record, so a league admin cannot link somebody
    else's login to a team of theirs and then reset its password. Superusers
    manage every account and do not need it.
    """
    user       = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="managed_account"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} (creato da {self.created_by or '—'})"


class Watch(models.Model):
    """A player a manager is tracking pre-auction ("obiettivo") + a mental cap.

    Purely a private planning aid for one participant: it never affects bids,
    prices or ownership. ``max_price`` is the manager's self-noted ceiling (the
    bidder page warns when the live price passes it); it is optional.
    """
    participant = models.ForeignKey(
        "Participant", on_delete=models.CASCADE, related_name="watches"
    )
    player      = models.ForeignKey("Player", on_delete=models.CASCADE, related_name="+")
    max_price   = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    note        = models.CharField(max_length=120, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["player__role", "player__name"]
        unique_together = [("participant", "player")]

    def __str__(self):
        return f"{self.participant.display_name} 🎯 {self.player.name}"
