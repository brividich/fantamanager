"""Backfill league scoping for the existing single-league install.

Until now Player had no league and the dashboard treated Participant/Player as a
global pool. With per-league pools we attach the existing data to the one real
League. On a fresh/empty DB (e.g. the test database) there is no League, so this
is a no-op and legacy rows simply keep league=None.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    League = apps.get_model("auctions", "League")
    Player = apps.get_model("auctions", "Player")
    Participant = apps.get_model("auctions", "Participant")
    Auction = apps.get_model("auctions", "Auction")

    league = League.objects.order_by("id").first()
    if league is None:
        return  # nothing to attach (fresh install / tests)

    Player.objects.filter(league__isnull=True).update(league=league)
    Participant.objects.filter(league__isnull=True).update(league=league)
    Auction.objects.filter(league__isnull=True).update(league=league)


def noop(apps, schema_editor):
    # Irreversible in practice (we cannot tell which rows were null before), but
    # leaving league set on reverse is harmless.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("auctions", "0014_player_league"),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
