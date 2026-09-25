from django.db import migrations, models


def forwards(apps, schema_editor):
    """Map the old queue_mode/queue_by_role pair onto the new flow fields."""
    Auction = apps.get_model("auctions", "Auction")
    for a in Auction.objects.all():
        qm = a.queue_mode or "call"
        by_role = bool(a.queue_by_role)

        # Flow: CALL stays manual-nomination; the old auto modes become CONTINUOUS.
        a.flow_mode = "call" if qm == "call" else "continuous"

        # Call order: a role-grouped run maps to PDCA; otherwise carry the sort.
        if by_role:
            a.call_order = "pdca"
        elif qm == "alpha":
            a.call_order = "alpha"
        elif qm == "random":
            a.call_order = "random"
        else:  # call
            a.call_order = "pdca"

        a.within_role_order = "quota"
        a.save(update_fields=["flow_mode", "call_order", "within_role_order"])


def backwards(apps, schema_editor):
    """Best-effort reverse: collapse the flow fields back to queue_mode/by_role."""
    Auction = apps.get_model("auctions", "Auction")
    for a in Auction.objects.all():
        if a.flow_mode == "call":
            a.queue_mode = "call"
        elif a.call_order == "alpha":
            a.queue_mode = "alpha"
        else:
            a.queue_mode = "random"
        a.queue_by_role = a.call_order in ("pdca", "acdp")
        a.save(update_fields=["queue_mode", "queue_by_role"])


class Migration(migrations.Migration):

    dependencies = [
        ("auctions", "0015_backfill_player_league"),
    ]

    operations = [
        migrations.AddField(
            model_name="auction",
            name="flow_mode",
            field=models.CharField(
                choices=[
                    ("call", "A chiamata"),
                    ("continuous", "Asta continua"),
                    ("manual", "Manuale (avanti/indietro)"),
                ],
                default="call",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="auction",
            name="call_order",
            field=models.CharField(
                choices=[
                    ("pdca", "Per ruolo P → D → C → A"),
                    ("acdp", "Per ruolo A → C → D → P"),
                    ("alpha", "Alfabetico"),
                    ("random", "Casuale"),
                ],
                default="pdca",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="auction",
            name="within_role_order",
            field=models.CharField(
                choices=[
                    ("quota", "Quotazione (alto → basso)"),
                    ("alpha", "Alfabetico"),
                    ("random", "Casuale"),
                ],
                default="quota",
                max_length=10,
            ),
        ),
        migrations.RunPython(forwards, backwards),
        migrations.RemoveField(model_name="auction", name="queue_mode"),
        migrations.RemoveField(model_name="auction", name="queue_by_role"),
    ]
