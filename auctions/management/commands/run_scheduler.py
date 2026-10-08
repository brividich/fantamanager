"""The scheduler service: ``python manage.py run_scheduler``.

Runs next to the web server (its own container in docker-compose) and does
on time what used to wait for a visitor: auction lots expiring, market
sessions opening and closing, lineups locking at the giornata deadline.
See ``services.scheduler``. ``--once`` does a single pass (cron, tests)."""
import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from ...services import scheduler

logger = logging.getLogger("auctions.scheduler")


class Command(BaseCommand):
    help = "Chiude i lotti scaduti, apre/chiude le sessioni di mercato e blocca le formazioni alla scadenza."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=float, default=2.0,
                            help="Secondi fra un giro e l'altro (default 2).")
        parser.add_argument("--once", action="store_true", help="Un solo giro, poi esce.")

    def handle(self, *args, interval, once, **opts):
        stop = {"now": False}

        def _stop(*_):
            stop["now"] = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        if not once:
            self.stdout.write(f"Scheduler avviato (ogni {interval}s).")
        while True:
            close_old_connections()
            try:
                summary = scheduler.run_once(broadcast=scheduler.channel_broadcast)
            except Exception:
                logger.exception("Scheduler: giro non riuscito")
                summary = None
            if summary and (summary["auctions"] or any(summary["market"]) or summary["giornate"]):
                logger.info("Scheduler: %s", summary)
            if once or stop["now"]:
                break
            time.sleep(interval)
        if once and summary is not None:
            self.stdout.write(str(summary))
