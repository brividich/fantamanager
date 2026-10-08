from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("auctions", "0061_voto_algoritmico_impostazioni"),
    ]

    operations = [
        migrations.AddField(
            model_name="playerperformance",
            name="vote_detail",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
