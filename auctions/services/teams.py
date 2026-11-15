"""Eliminare una squadra della lega.

Serve per la squadra creata per sbaglio (una in più nel wizard, un doppione):
sparisce con le cose che non hanno ancora contato, i suoi giocatori tornano
svincolati. Una squadra che ha già giocato (punteggi, partite calcolate,
offerte d'asta, buste, scambi) resta: cancellarla toglierebbe partite e
risultati anche agli avversari e allo storico della lega.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from ..models import (Bid, Fixture, GiornataScore, MarketBid, Participant, Player, RosterLog,
                      SealedBid, Trade)
from . import sala


def team_removal(team):
    """Che cosa vuol dire eliminare ``team``: ``blocks`` (perché non si può,
    vuoto se si può), ``roster`` (giocatori che tornano svincolati) e
    ``fixtures`` (partite del calendario ancora da giocare che spariscono)."""
    blocks = []
    played = Fixture.objects.filter(Q(home=team) | Q(away=team), computed=True).count()
    scores = GiornataScore.objects.filter(participant=team).count()
    if played or scores:
        blocks.append(f"ha già giocato ({max(played, scores)} giornate con il punteggio)")
    bids = Bid.objects.filter(participant=team).count() + SealedBid.objects.filter(participant=team).count()
    if bids:
        blocks.append(f"ha fatto {bids} offerte in asta")
    market = MarketBid.objects.filter(participant=team).count()
    if market:
        blocks.append(f"ha {market} buste nei mercati")
    trades = Trade.objects.filter(Q(proposer=team) | Q(receiver=team)).count()
    if trades:
        blocks.append(f"ha {trades} scambi")
    return {
        "blocks": blocks,
        "roster": Player.objects.filter(owner=team).count(),
        "fixtures": Fixture.objects.filter(Q(home=team) | Q(away=team), computed=False).count(),
    }


class TeamNotRemovable(Exception):
    """Perché la squadra non si elimina (il messaggio è per l'admin)."""


@transaction.atomic
def delete_team(team):
    """Elimina ``team`` se ``team_removal`` lo permette; i giocatori tornano
    svincolati (registro movimenti «Svincolo (admin)»). Ritorna
    ``team_removal`` com'era prima, per dire all'admin che cosa è successo."""
    team = Participant.objects.select_for_update().get(pk=team.pk)
    sala.ensure_unlocked(team.league_id)
    info = team_removal(team)
    if info["blocks"]:
        raise TeamNotRemovable(
            f"«{team.display_name}» non si può eliminare: " + "; ".join(info["blocks"]) + ". "
            "Quello che ha fatto resta nello storico della lega. Se non gioca più, togli l'accesso dalla colonna "
            "Account e svincola i suoi giocatori dalla scheda Rose.")
    for player in Player.objects.select_for_update().filter(owner=team):
        RosterLog.objects.create(
            participant=team, participant_name=team.display_name,
            player_name=player.name, player_role=player.role,
            action=RosterLog.Action.ADMIN_RELEASE, credits_delta=Decimal("0"), by_admin=True,
            note="Squadra eliminata: torna svincolato",
        )
        player.owner = None
        player.cost = Decimal("0")
        player.save(update_fields=["owner", "cost"])
    team.delete()
    return info


# --- Squadre doppie: si uniscono, non si cancellano ------------------------------------

def team_key(name):
    """Il nome di una squadra ridotto per confrontarlo: senza maiuscole,
    accenti, punteggiatura e spazi («SLAVIA VIA FONDA» = «Slavia Viafonda»,
    «MACCABI DE FEO» = «Maccabi DeFeo»)."""
    from ..providers.importers import _norm
    return _norm(name).replace(" ", "")


def likely_duplicates(teams):
    """Le coppie di squadre della stessa lega che sembrano la stessa
    (nome uguale a meno di spazi e punteggiatura, o quasi uguale): di solito
    una creata dall'import delle rose accanto a quella che c'era già."""
    from difflib import SequenceMatcher

    teams = list(teams)
    keys = {t.pk: team_key(t.display_name) for t in teams}
    pairs = []
    for i, a in enumerate(teams):
        for b in teams[i + 1:]:
            ka, kb = keys[a.pk], keys[b.pk]
            if not ka or not kb:
                continue
            if ka == kb or (min(len(ka), len(kb)) >= 6 and SequenceMatcher(None, ka, kb).ratio() >= 0.9):
                pairs.append((a, b))
    return pairs


class TeamsNotMergeable(Exception):
    """Perché le due squadre non si uniscono (il messaggio è per l'admin)."""


def merge_problems(dup, keep):
    """Perché ``dup`` non può confluire in ``keep`` (lista vuota se può)."""
    problems = []
    if dup.pk == keep.pk:
        problems.append("hai scelto la stessa squadra")
    if dup.league_id != keep.league_id:
        problems.append("le due squadre sono di leghe diverse")
    played = Fixture.objects.filter(Q(home=dup) | Q(away=dup), computed=True).count()
    if played or GiornataScore.objects.filter(participant=dup).exists():
        problems.append(f"«{dup.display_name}» ha già giocato giornate con il punteggio: le sue partite "
                        "resterebbero senza squadra")
    if Trade.objects.filter(Q(proposer=dup, receiver=keep) | Q(proposer=keep, receiver=dup)).exists():
        problems.append("le due squadre hanno fatto scambi fra loro")
    from ..models import Auction
    running = Auction.objects.filter(league_id=keep.league_id,
                                     status__in=[Auction.Status.LIVE, Auction.Status.PAUSED])
    if running.exists():
        problems.append("c'è un'asta in corso nella lega (offerte e spareggi a buste aspettano le squadre "
                        "come sono): uniscile quando l'asta è chiusa")
    from ..models import MarketSession
    from .market import pending_releases
    for session in MarketSession.objects.filter(league_id=keep.league_id).exclude(
            status=MarketSession.Status.DRAFT):
        if session.status in (MarketSession.Status.OPEN, MarketSession.Status.CLOSED) \
                and MarketBid.objects.filter(session=session, participant__in=[dup, keep]).exists():
            problems.append(f"il mercato «{session.title}» è ancora aperto o da spogliare con buste di queste "
                            "squadre: uniscile dopo lo spoglio")
        elif pending_releases(session, dup.id):
            problems.append(f"«{dup.display_name}» deve ancora scegliere chi esce per le buste vinte in "
                            f"«{session.title}»: completa prima i tagli, poi unisci")
    rounds = set(SealedBid.objects.filter(participant=keep).values_list("auction_id", "cycle", "round"))
    if rounds and any(r in rounds for r in SealedBid.objects.filter(participant=dup)
                      .values_list("auction_id", "cycle", "round")):
        problems.append("hanno fatto tutte e due una busta segreta nello stesso giro della stessa asta: "
                        "sono due squadre che hanno giocato davvero")
    accounts = {u.pk: u for u in (keep.user, keep.co_user, dup.user, dup.co_user) if u is not None}
    if len(accounts) > 2:
        problems.append("insieme hanno più di due account, e una squadra ne ha al massimo due: "
                        "scollegane uno dalla colonna Account")
    return problems


@transaction.atomic
def merge_teams(dup, keep, *, budget="keep"):
    """``dup`` confluisce in ``keep`` e sparisce: giocatori (con costo e
    contratto), account, movimenti, offerte, buste, formazioni e voci del
    tetto passano a ``keep``. ``budget`` dice quanto resta a disposizione:
    ``keep`` (quello di chi resta), ``dup`` (quello dell'altra) o ``sum``.
    Ritorna un riassunto per l'admin."""
    from ..models import (AuctionCycleResult, CapEntry, ContractEvent, Formation, LeagueRanking,
                          MarketReleaseIntent, MatchdayFormation, Watch)

    keep = Participant.objects.select_for_update().get(pk=keep.pk)
    dup = Participant.objects.select_for_update().get(pk=dup.pk)
    sala.ensure_unlocked(keep.league_id)
    problems = merge_problems(dup, keep)
    if problems:
        raise TeamsNotMergeable(f"«{dup.display_name}» e «{keep.display_name}» non si uniscono: "
                                + "; ".join(problems) + ".")
    left = {"keep": keep.remaining_credits, "dup": dup.remaining_credits,
            "sum": keep.remaining_credits + dup.remaining_credits}.get(budget, keep.remaining_credits)

    moved = Player.objects.filter(owner=dup).update(owner=keep)
    Player.objects.filter(loan_from=dup).update(loan_from=keep)
    Player.objects.filter(rescinded_from=dup).update(rescinded_from=keep)
    RosterLog.objects.filter(participant=dup).update(participant=keep)
    ContractEvent.objects.filter(participant=dup).update(participant=keep)
    # Tetto: la quota per classifica (base estiva, fondi invernali) di una fase
    # in cui chi resta ha già la sua non si somma; il resto (bonus, extra cap,
    # rettifiche) passa.
    for kind in (CapEntry.Kind.BASE, CapEntry.Kind.WINTER):
        phases = CapEntry.objects.filter(participant=keep, kind=kind).values_list("phase_id", flat=True)
        CapEntry.objects.filter(participant=dup, kind=kind, phase_id__in=list(phases)).delete()
    CapEntry.objects.filter(participant=dup).update(participant=keep)
    # Classifiche salvate e ordini dei waiver: la doppia diventa chi resta; se
    # ci sono tutte e due, chi resta tiene il suo posto e la doppia esce.
    def follow(order):
        if not isinstance(order, list) or dup.id not in order:
            return None
        return ([pid for pid in order if pid != dup.id] if keep.id in order
                else [keep.id if pid == dup.id else pid for pid in order])

    for ranking in LeagueRanking.objects.filter(league_id=keep.league_id):
        order = follow(list(ranking.order or []))
        if order is not None:
            ranking.order = order
            ranking.save(update_fields=["order"])
    from ..models import MarketSession
    for session in MarketSession.objects.filter(league_id=keep.league_id):
        fields = []
        order = follow((session.config or {}).get("waiver_order"))
        if order is not None:
            session.config = {**session.config, "waiver_order": order}
            fields.append("config")
        before = follow((session.results_summary or {}).get("waiver_order_before"))
        if before is not None:
            session.results_summary = {**session.results_summary, "waiver_order_before": before}
            fields.append("results_summary")
        if fields:
            session.save(update_fields=fields)
    AuctionCycleResult.objects.filter(winner=dup).update(winner=keep)
    Bid.objects.filter(participant=dup).update(participant=keep)
    SealedBid.objects.filter(participant=dup).update(participant=keep)
    MarketBid.objects.filter(participant=dup).update(participant=keep)
    Trade.objects.filter(proposer=dup).update(proposer=keep)
    Trade.objects.filter(receiver=dup).update(receiver=keep)
    # Dove c'è un vincolo «una per squadra» vince quella di chi resta.
    watched = Watch.objects.filter(participant=keep).values_list("player_id", flat=True)
    Watch.objects.filter(participant=dup, player_id__in=list(watched)).delete()
    Watch.objects.filter(participant=dup).update(participant=keep)
    intents = MarketReleaseIntent.objects.filter(participant=keep).values_list("session_id", "player_id")
    for session_id, player_id in list(intents):
        MarketReleaseIntent.objects.filter(participant=dup, session_id=session_id, player_id=player_id).delete()
    MarketReleaseIntent.objects.filter(participant=dup).update(participant=keep)
    days = MatchdayFormation.objects.filter(participant=keep).values_list("giornata_id", flat=True)
    MatchdayFormation.objects.filter(participant=dup, giornata_id__in=list(days)).delete()
    MatchdayFormation.objects.filter(participant=dup).update(participant=keep)
    if Formation.objects.filter(participant=keep).exists():
        Formation.objects.filter(participant=dup).delete()
    else:
        Formation.objects.filter(participant=dup).update(participant=keep)
    fixtures = Fixture.objects.filter(Q(home=dup) | Q(away=dup), computed=False).count()

    # Account: chi resta tiene i suoi, quelli dell'altra riempiono i posti liberi.
    incoming = [u for u in (dup.user, dup.co_user) if u is not None and not keep.has_account(u)]
    dup.user = dup.co_user = None
    dup.co_invite_token, dup.co_invite_user = "", None
    dup.save(update_fields=["user", "co_user", "co_invite_token", "co_invite_user"])
    for user in incoming:
        if keep.user_id is None:
            keep.user = user
        elif keep.co_user_id is None:
            keep.co_user = user
    if keep.co_user_id and (keep.co_invite_token or keep.co_invite_user_id):
        keep.co_invite_token, keep.co_invite_user = "", None
    keep.spent_credits = keep.spent_credits + dup.spent_credits
    keep.credits = keep.spent_credits + left
    if not keep.email and dup.email:
        keep.email = dup.email
    keep.save()
    RosterLog.objects.create(
        participant=keep, participant_name=keep.display_name, player_name=f"Unita «{dup.display_name}»",
        action=RosterLog.Action.EDIT, credits_delta=Decimal("0"), by_admin=True,
        note=f"Squadra doppia «{dup.display_name}» unita: {moved} giocatori, budget a disposizione {left:.0f} FM",
    )
    name = dup.display_name
    dup.delete()
    return {"name": name, "players": moved, "accounts": [u.username for u in incoming], "left": left,
            "fixtures": fixtures}
