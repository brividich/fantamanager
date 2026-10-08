"""Calibra il voto algoritmico di una lega sulle giornate già giocate.

Raccoglie i voti grezzi (``vote_detail["raw"]``, prima della calibrazione) della
stagione corrente della lega, stampa media e deviazione standard attuali e la
scala/spostamento che portano alla media e dispersione volute. Con ``--apply``
li scrive nei ritocchi della lega (``Season.rules["algo"]``). Si usa una volta,
o a inizio stagione: ricalibrare a ogni giornata renderebbe i voti non
confrontabili.
"""
from statistics import mean, pstdev

from django.core.management.base import BaseCommand, CommandError

from ...models import League, PlayerPerformance, Season
from ...services import voto_algo
from ...voto_algoritmico import calibrate

MIN_VOTES = 30


class Command(BaseCommand):
    help = "Calibra il voto algoritmico di una lega sui voti grezzi della stagione corrente"

    def add_arguments(self, parser):
        parser.add_argument("--league", type=int, required=True, help="id della lega")
        parser.add_argument("--mean", type=float, default=6.0, help="media voluta (default 6,0)")
        parser.add_argument("--sd", type=float, default=0.6, help="deviazione standard voluta (default 0,6)")
        parser.add_argument("--apply", action="store_true",
                            help="scrive scala e spostamento nelle regole della lega (senza: solo anteprima)")

    def handle(self, *args, **opts):
        league = League.objects.filter(pk=opts["league"]).first()
        if league is None:
            raise CommandError(f"Lega {opts['league']} inesistente.")
        season = Season.objects.filter(league=league, is_current=True).first()
        if season is None:
            raise CommandError(f"La lega «{league.name}» non ha una stagione corrente.")
        if opts["sd"] <= 0:
            raise CommandError("La deviazione standard voluta deve essere maggiore di zero.")

        raws = []
        for detail in (PlayerPerformance.objects.filter(giornata__season=season, vote_detail__isnull=False)
                       .values_list("vote_detail", flat=True)):
            if (detail or {}).get("source") == voto_algo.ALGO_LIVE_SOURCE and detail.get("raw") is not None:
                raws.append(float(detail["raw"]))
        if len(raws) < MIN_VOTES:
            raise CommandError(
                f"Servono almeno {MIN_VOTES} voti algoritmici per calibrare: la stagione corrente di "
                f"«{league.name}» ne ha {len(raws)}.")

        rules = voto_algo.algo_rules_for(season)
        proposal = calibrate(raws, target_mean=opts["mean"], target_sd=opts["sd"], base=rules["base"])
        self.stdout.write(f"Lega «{league.name}», {season.name}: {len(raws)} voti grezzi.")
        self.stdout.write(f"Attuale: media {mean(raws):.3f}, deviazione standard {pstdev(raws):.3f} "
                          f"(calibrazione in uso: scala {rules['calib_scale']}, spostamento {rules['calib_shift']}).")
        self.stdout.write(f"Proposta per media {opts['mean']} e deviazione {opts['sd']}: "
                          f"calib_scale {proposal['calib_scale']}, calib_shift {proposal['calib_shift']}.")
        if not opts["apply"]:
            self.stdout.write("Anteprima: nulla è stato scritto. Rilancia con --apply per salvarla.")
            return
        algo = {**((season.rules or {}).get("algo") or {}), **proposal}
        candidate = voto_algo.merge_overrides(
            {k: v for k, v in voto_algo.platform_rules().items() if not k.startswith("_")}, algo)
        _full, errors = voto_algo.clean_rules(candidate)
        if errors:
            raise CommandError("Calibrazione fuori dai limiti, non salvata: " + "; ".join(errors.values()))
        season.rules = dict(season.rules or {})
        season.rules["algo"] = algo
        season.save(update_fields=["rules"])
        self.stdout.write(self.style.SUCCESS(
            "Calibrazione salvata nelle regole della lega. Per applicarla alle giornate già giocate "
            "salva le regole di punteggio dalla pagina Giornate con «Ricalcola le giornate già giocate»."))
