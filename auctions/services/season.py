"""Passaggi di stagione automatizzati.

L'admin preme tre pulsanti in tutta la stagione e il sistema fa il resto:

* **Nuova stagione** (fine campionato): fotografa la classifica finale, assegna
  il Decreto Salvacalcio di fine stagione, scala di un anno i contratti e apre i
  rinnovi. Se non ci sono contratti scaduti apre subito la fase estiva.
* **Chiudi rinnovi**: quando le squadre hanno dichiarato e tirato i dadi,
  apre la fase estiva del tetto salariale (base per classifica + bonus
  giocatori persi e rinnovi mancati).
* **Metà stagione**: fotografa la classifica attuale, assegna il Decreto di
  metà stagione e apre la fase invernale (fondi invernali + nuovi bonus).

La classifica arriva, in ordine: da quella passata dall'admin, dalla fonte
remota configurata (sito di lega), dalla classifica interna dell'app.
"""
from django.db import transaction

from ..models import CapPhase, League, LeagueRanking
from . import contracts, salary
from .sala import ensure_unlocked as _sala_guard


def _resolve_ranking(league, season, kind, order=None):
    if order:
        return salary.save_ranking(league, season, kind, order, LeagueRanking.Source.MANUAL)
    existing = LeagueRanking.objects.filter(league=league, season=season, kind=kind).first()
    if existing is not None:
        return existing
    from ..providers.standings import fetch_remote_ranking

    remote = fetch_remote_ranking(league)
    if remote:
        return salary.save_ranking(league, season, kind, remote, LeagueRanking.Source.REMOTE)
    app = salary.app_ranking(league)
    if app:
        return salary.save_ranking(league, season, kind, app, LeagueRanking.Source.APP)
    return None


def _step(report, text):
    report["steps"].append(text)


DECREE_NAMES = {LeagueRanking.Kind.MIDSEASON: "invernale (metà stagione)",
                LeagueRanking.Kind.FINAL: "estivo (fine stagione)"}


def _award(report, league, kind, ranking):
    try:
        award = salary.award_decree(league, kind, ranking)
    except ValueError as exc:
        report["warnings"].append(str(exc))
        return
    _step(report, f"Decreto Salvacalcio {DECREE_NAMES[kind]}: "
          + ", ".join(f"{d['name']} +{d['credited']}" for d in award.details) + ".")


@transaction.atomic
def award_decree_now(league_id, kind, order=None):
    """L'admin assegna adesso il Decreto invernale o estivo della stagione in
    corso, senza il passaggio di stagione (la classifica come per i passaggi:
    quella scritta, il sito di lega, la classifica dell'app). Una volta sola:
    il passaggio dopo lo trova già assegnato e lo salta."""
    league = League.objects.select_for_update().get(pk=league_id)
    _sala_guard(league)
    ranking = _resolve_ranking(league, league.season_number, kind, order)
    if ranking is None:
        what = "di metà stagione" if kind == LeagueRanking.Kind.MIDSEASON else "finale"
        return {"ok": False, "steps": [], "warnings": [],
                "message": f"Serve la classifica {what}: scrivi la posizione di ogni squadra nella colonna "
                           f"«{'Metà' if kind == LeagueRanking.Kind.MIDSEASON else 'Fine'}» e riprova."}
    report = {"ok": True, "steps": [], "warnings": []}
    _award(report, league, kind, ranking)
    if report["warnings"] and not report["steps"]:
        return {"ok": False, "message": report["warnings"][0], "steps": [], "warnings": []}
    return report


@transaction.atomic
def start_new_season(league_id, final_order=None):
    league = League.objects.select_for_update().get(pk=league_id)
    _sala_guard(league)
    report = {"ok": True, "steps": [], "warnings": []}
    finishing = league.season_number

    ranking = _resolve_ranking(league, finishing, LeagueRanking.Kind.FINAL, final_order)
    if ranking is None:
        report["warnings"].append("Classifica finale mancante: inseriscila per Decreto e tetto salariale.")
    else:
        _step(report, f"Classifica finale della stagione {finishing} salvata ({ranking.get_source_display()}).")
        if league.decree_final_enabled:
            _award(report, league, LeagueRanking.Kind.FINAL, ranking)

    if league.contracts_enabled:
        res = contracts.new_season(league.id)
        league.refresh_from_db()
        _step(report, f"Stagione {res['season']}: contratti scalati di un anno, {res['expired']} scaduti.")
        _roll_championship(league, report)
        for name, amount in res.get("listed_lost", []):
            _step(report, f"{name} lascia la lista ceduti a fine contratto: +{amount} FM alla squadra.")
        if res["expired"]:
            _step(report, "Rinnovi aperti: le squadre dichiarano e tirano il dado rinnovo dalla Rosa. "
                          "Poi premi «Chiudi rinnovi» per aprire la fase estiva.")
            return report
        contracts.close_renewals(league.id)
    else:
        league.season_number += 1
        league.save(update_fields=["season_number", "updated_at"])
        _step(report, f"Stagione {league.season_number} iniziata.")
        _roll_championship(league, report)

    _open_summer(league, ranking, report)
    return report


def _roll_championship(league, report):
    """Giornate, risultati e classifiche dell'anno finito restano nella loro
    stagione (lo storico); la nuova riparte da zero con le stesse competizioni."""
    from .competitions import roll_season

    new = roll_season(league)
    if new is not None:
        _step(report, f"{new.name}: giornate nuove, competizioni ricreate con i loro calendari; "
                      "risultati e classifiche dell'anno scorso restano nello storico.")


def _tick_loans(league, report):
    from .loans import tick
    for name in tick(league):
        _step(report, f"Fine prestito: {name} rientra alla squadra che ha il cartellino.")


def _open_summer(league, final_ranking, report):
    _tick_loans(league, report)
    if not league.salary_cap_enabled:
        return
    try:
        salary.open_phase(league, CapPhase.Kind.SUMMER, final_ranking)
        _step(report, "Fase estiva aperta: tetti salariali calcolati.")
    except ValueError as exc:
        report["warnings"].append(str(exc))


@transaction.atomic
def close_renewals(league_id):
    league = League.objects.select_for_update().get(pk=league_id)
    _sala_guard(league)
    report = {"ok": True, "steps": [], "warnings": []}
    contracts.close_renewals(league.id)
    _step(report, "Rinnovi chiusi.")
    previous = LeagueRanking.objects.filter(
        league=league, season=league.season_number - 1, kind=LeagueRanking.Kind.FINAL).first()
    _open_summer(league, previous, report)
    return report


@transaction.atomic
def midseason(league_id, mid_order=None):
    league = League.objects.select_for_update().get(pk=league_id)
    _sala_guard(league)
    report = {"ok": True, "steps": [], "warnings": []}
    ranking = _resolve_ranking(league, league.season_number, LeagueRanking.Kind.MIDSEASON, mid_order)
    if ranking is None:
        return {"ok": False, "message": "Serve la classifica di metà stagione.", "steps": [], "warnings": []}
    _step(report, f"Classifica di metà stagione salvata ({ranking.get_source_display()}).")
    _tick_loans(league, report)
    if league.decree_mid_enabled:
        _award(report, league, LeagueRanking.Kind.MIDSEASON, ranking)
    if league.salary_cap_enabled:
        try:
            salary.open_phase(league, CapPhase.Kind.WINTER, ranking)
            _step(report, "Fase invernale aperta: fondi invernali e bonus aggiunti ai tetti.")
        except ValueError as exc:
            report["warnings"].append(str(exc))
    return report
