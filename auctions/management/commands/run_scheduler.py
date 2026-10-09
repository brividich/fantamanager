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

from ...services import privacy, scheduler

logger = logging.getLogger("auctions.scheduler")

PRIVACY_CLEANUP_EVERY = 24 * 3600


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
        last_cleanup = 0.0
        while True:
            if not once and time.monotonic() - last_cleanup >= PRIVACY_CLEANUP_EVERY:
                # Conservazione dei dati personali (docs/PRIVACY.md): una volta
                # al giorno, e subito all'avvio.
                last_cleanup = time.monotonic()
                try:
                    report = privacy.cleanup()
                    if any(report.values()):
                        logger.info("Pulizia privacy: %s", report)
                except Exception:
                    logger.exception("Pulizia privacy non riuscita")
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
            # Between passes, like between two requests: drop a connection
            # past CONN_MAX_AGE or broken. Never inside a pass (or --once,
            # which may run inside the caller's transaction).
            close_old_connections()
        if once and summary is not None:
            self.stdout.write(str(summary))
