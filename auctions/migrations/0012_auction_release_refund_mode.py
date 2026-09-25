from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("auctions", "0011_auction_public_token_participant_public_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="auction",
            name="release_refund_mode",
            field=models.CharField(
                choices=[
                    ("purchase", "Costo di acquisto"),
                    ("current", "Costo attuale (quotazione)"),
                    ("none", "Nessun rimborso"),
                ],
                default="purchase",
                max_length=10,
            ),
        ),
    ]
