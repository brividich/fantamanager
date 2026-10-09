"""La pulizia della privacy: ``python manage.py privacy_cleanup``.

Azzera IP e user-agent delle offerte vecchie, cancella le sessioni scadute e
gli account registrati da soli, mai confermati e senza squadre né leghe (vedi
``services.privacy.cleanup`` e docs/PRIVACY.md). Lo scheduler la fa da solo
una volta al giorno; ``--dry-run`` conta senza toccare niente.
"""
from django.core.management.base import BaseCommand

from ...services import privacy


class Command(BaseCommand):
    help = "Applica le regole di conservazione dei dati personali (IP delle offerte, sessioni, account mai confermati)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Conta soltanto, non cambia niente.")

    def handle(self, *args, dry_run=False, **opts):
        report = privacy.cleanup(dry_run=dry_run)
        verb = "da pulire" if dry_run else "puliti"
        self.stdout.write(
            f"Offerte senza più IP/dispositivo ({verb}): {report['bids']} · "
            f"sessioni scadute: {report['sessions']} · account mai confermati: {report['accounts']}")
