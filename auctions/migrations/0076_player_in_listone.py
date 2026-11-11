from django.db import migrations, models


def mark_out_of_listone(apps, schema_editor):
    """Gli svincolati che già oggi non sono più nel listone: chi è stato perso
    con la cessione fuori dalla Serie A (evento «left») ed è ancora senza
    squadra, chi era segnalato come uscito, e nelle leghe sul listone generale
    chi nell'anagrafica non è più in Serie A."""
    Player = apps.get_model("auctions", "Player")
    ContractEvent = apps.get_model("auctions", "ContractEvent")
    free = Player.objects.filter(owner__isnull=True)
    left_ids = ContractEvent.objects.filter(kind="left", player__isnull=False).values("player_id")
    free.filter(id__in=left_ids).update(in_listone=False)
    free.filter(left_serie_a_at__isnull=False).update(in_listone=False)
    Player.objects.filter(league__isnull=False, league__own_listone=False,
                          footballer__isnull=False, footballer__in_serie_a=False).update(in_listone=False)


class Migration(migrations.Migration):

    dependencies = [
        ("auctions", "0075_league_demo"),
    ]

    operations = [
        migrations.AddField(
            model_name="player",
            name="in_listone",
            field=models.BooleanField(db_index=True, default=True),
        ),
        migrations.RunPython(mark_out_of_listone, migrations.RunPython.noop),
    ]
