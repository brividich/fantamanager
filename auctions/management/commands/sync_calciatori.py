"""Aggiorna l'anagrafica comune dei calciatori da API-Football.

Una richiesta per club di Serie A al ritmo del piano (il gratuito ne concede
10 al minuto): da pianificare, per esempio, una volta al giorno.
"""
from django.core.management.base import BaseCommand, CommandError

from ...services import footballers


class Command(BaseCommand):
    help = "Aggiorna l'anagrafica dei calciatori dalle rose dei club di Serie A su API-Football"

    def handle(self, *args, **options):
        report = footballers.sync_registry(
            progress=lambda done, total: self.stdout.write(f"club {done + 1} di {total}…"))
        summary = footballers.sync_summary(report)
        if report["error"] and not report["clubs"]:
            raise CommandError(summary)
        self.stdout.write(self.style.SUCCESS(summary))
