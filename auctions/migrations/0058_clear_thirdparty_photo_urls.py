"""Le foto non si prendono più dal CDN di fantacalcio.it.

Svuota i ``photo_url`` che puntano lì e, dove il giocatore è collegato
all'anagrafica API-Football, mette la foto dell'anagrafica.
"""
from django.db import migrations

THIRD_PARTY_HOST = "content.fantacalcio.it"


def clear_photos(apps, schema_editor):
    Player = apps.get_model("auctions", "Player")
    for p in Player.objects.filter(photo_url__contains=THIRD_PARTY_HOST).select_related("footballer"):
        p.photo_url = (p.footballer.photo_url if p.footballer_id else "") or ""
        p.save(update_fields=["photo_url"])


class Migration(migrations.Migration):

    dependencies = [
        ("auctions", "0057_footballer_registry"),
    ]

    operations = [
        migrations.RunPython(clear_photos, migrations.RunPython.noop),
    ]
