"""Shared test fixtures and helpers for auction tests."""
from datetime import timedelta
from decimal import Decimal
from django.utils import timezone

from ..models import Auction


def make_live_auction(**kwargs):
    now = timezone.now()
    defaults = dict(
        title="Test",
        starting_price=Decimal("100"),
        current_price=Decimal("100"),
        min_increment=Decimal("10"),
        quick_increments="10,50,100,500",
        duration_seconds=60,
        status=Auction.Status.LIVE,
        starts_at=now,
        ends_at=now + timedelta(seconds=60),
    )
    defaults.update(kwargs)
    return Auction.objects.create(**defaults)
