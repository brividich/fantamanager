from django.db import migrations


def drop_out_of_listone(apps, schema_editor):
    """Le code delle aste fatte prima di 0077 possono avere in attesa chi la
    0077 ha messo fuori listone: escono (non il lotto già sul banco), come fa
    ``services.queue.drop_from_queues`` quando qualcuno esce dal listone."""
    Auction = apps.get_model("auctions", "Auction")
    AuctionQueueItem = apps.get_model("auctions", "AuctionQueueItem")
    on_block = Auction.objects.exclude(player__isnull=True).values("player_id")
    (AuctionQueueItem.objects
     .filter(done=False, player__owner__isnull=True, player__in_listone=False)
     .exclude(player_id__in=on_block)
     .delete())


class Migration(migrations.Migration):

    dependencies = [
        ("auctions", "0077_player_in_listone"),
    ]

    operations = [
        migrations.RunPython(drop_out_of_listone, migrations.RunPython.noop),
    ]
