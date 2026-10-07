from django.apps import AppConfig


class AuctionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "auctions"

    def ready(self):
        from django.db.models.signals import post_save

        from .models import League
        from .services.footballers import on_league_created

        post_save.connect(on_league_created, sender=League, dispatch_uid="footballers_default_listone")
