"""Session snapshot, restore, and maintenance operations."""
import secrets as _sec
from decimal import Decimal, InvalidOperation

from django.db import transaction

from ..models import (
    Auction, AuctionCycleResult, AuctionSession, League, Participant, Player,
)


def league_overview(leagues=None):
    """One row per league for the config page: size, activity, health.

    ``playable`` is the same condition ``start_auction`` enforces, so the page
    can show at a glance which leagues are dead weight (no listone, no auction)
    and which are the real ones. ``leagues`` narrows it to the ones a user
    runs; None means every league.
    """
    qs = League.objects.all() if leagues is None else leagues
    rows = []
    for lg in qs.order_by("id"):
        pool = Player.objects.filter(league=lg)
        auctions = Auction.objects.filter(league=lg)
        teams = Participant.objects.filter(league=lg)
        rows.append({
            "league": lg,
            "teams": teams.count(),
            # Teams nobody can log into: no account and no PIN.
            "locked_out": teams.filter(user__isnull=True, access_code="").count(),
            "pool": pool.count(),
            "free": pool.filter(owner__isnull=True).count(),
            "auctions": auctions.count(),
            "live": auctions.filter(status=Auction.Status.LIVE).count(),
            "sessions": AuctionSession.objects.filter(league=lg).count(),
            "playable": pool.exists(),
        })
    return rows


@transaction.atomic
def delete_league(league_id):
    """Remove a league and everything that hangs off it.

    Every FK to League is ``SET_NULL``, so deleting the row on its own would
    leave its players, teams and auctions behind as orphans in the legacy pool
    — invisible in the console but still counted. This clears the tree
    explicitly (saved sessions included: leaving them would let "Riprendi
    sessione" resurrect the league that was just deleted) and reports what went.
    """
    league = League.objects.filter(pk=league_id).first()
    if league is None:
        return None
    report = {
        "name": league.name,
        "players": Player.objects.filter(league=league).count(),
        "teams": Participant.objects.filter(league=league).count(),
        "auctions": Auction.objects.filter(league=league).count(),
        "sessions": AuctionSession.objects.filter(league=league).count(),
    }
    Player.objects.filter(league=league).delete()
    Participant.objects.filter(league=league).delete()
    Auction.objects.filter(league=league).delete()
    AuctionSession.objects.filter(league=league).delete()
    league.delete()
    return report


@transaction.atomic
def delete_auction(auction_id):
    """Remove one auction (its bids, queue and results cascade with it).

    The league, its teams and its listone stay: only the event is dropped.
    """
    auction = Auction.objects.filter(pk=auction_id).first()
    if auction is None:
        return None
    report = {"title": auction.title, "bids": auction.bids.count()}
    auction.delete()
    return report


def _snapshot_pool(league):
    """Serialise a league's whole listone — free agents included.

    Without this a resumed session came back with only the owned players, so
    the new league had no pool and the auction could never start ("il listone
    è obbligatorio"). Ownership travels as the team name, which is what the
    resume rebuilds participants by.
    """
    qs = Player.objects.filter(league=league) if league is not None else Player.objects.all()
    return [
        {
            "name": pl.name, "role": pl.role, "team": pl.team,
            "initial_price": str(pl.initial_price), "cost": str(pl.cost),
            "owner": pl.owner.display_name if pl.owner_id else None,
            "photo_url": pl.photo_url, "ext_id": pl.ext_id,
            "fvm": None if pl.fvm is None else str(pl.fvm),
            "presences": pl.presences,
            "avg_vote": None if pl.avg_vote is None else str(pl.avg_vote),
            "fanta_avg": None if pl.fanta_avg is None else str(pl.fanta_avg),
            "goals": pl.goals, "assists": pl.assists,
        }
        for pl in qs.select_related("owner").order_by("role", "name")
    ]


def _restore_pool(pool, league, participants_by_name):
    """Rebuild a league's listone from a snapshot. Returns how many were made."""
    def _dec_or_none(v):
        try:
            return None if v in (None, "") else Decimal(str(v))
        except (InvalidOperation, ValueError, TypeError):
            return None

    made = []
    for pl in pool:
        owner_name = pl.get("owner")
        made.append(Player(
            league=league,
            name=(pl.get("name") or "")[:120],
            role=(pl.get("role") or "A")[:1],
            team=(pl.get("team") or "")[:80],
            initial_price=_dec_or_none(pl.get("initial_price")) or Decimal("1"),
            cost=_dec_or_none(pl.get("cost")) or Decimal("0"),
            owner=participants_by_name.get(owner_name) if owner_name else None,
            photo_url=pl.get("photo_url") or "",
            ext_id=(pl.get("ext_id") or "")[:30],
            fvm=_dec_or_none(pl.get("fvm")),
            presences=pl.get("presences"),
            avg_vote=_dec_or_none(pl.get("avg_vote")),
            fanta_avg=_dec_or_none(pl.get("fanta_avg")),
            goals=pl.get("goals"), assists=pl.get("assists"),
        ))
    Player.objects.bulk_create(made, batch_size=500)
    return len(made)


def _snapshot_participant(p):
    """Serialise one participant plus their current roster into plain JSON."""
    roster = [
        {"name": pl.name, "role": pl.role, "team": pl.team, "cost": str(pl.cost)}
        for pl in Player.objects.filter(owner=p).order_by("role", "name")
    ]
    return {
        "display_name": p.display_name,
        "external_team_id": p.external_team_id,
        "credits": str(p.credits),
        "spent_credits": str(p.spent_credits),
        "is_active": p.is_active,
        "roster": roster,
    }


@transaction.atomic
def save_session(auction_id, *, name="", created_by="", notes=""):
    """Capture the standings of an auction's league into a durable AuctionSession.

    Participants are taken from the auction's league when set, otherwise from
    every participant (legacy single-league install). The snapshot is a plain
    JSON payload so it survives schema changes and can be inspected/exported.
    """
    auction = Auction.objects.get(pk=auction_id)
    league = auction.league

    if league is not None:
        participants = Participant.objects.filter(league=league)
    else:
        participants = Participant.objects.all()

    payload = {
        "league": None if league is None else {
            "name": league.name, "budget": str(league.budget),
            "slot_limits": league.slot_limits,
            "slots_p": league.slots_p, "slots_d": league.slots_d,
            "slots_c": league.slots_c, "slots_a": league.slots_a,
            "source_site": league.source_site, "external_id": league.external_id,
        },
        "auction": {
            "title": auction.title, "min_increment": str(auction.min_increment),
            "quick_increments": auction.quick_increments,
            "duration_seconds": auction.duration_seconds,
            "antisnipe_seconds": auction.antisnipe_seconds,
            "enforce_limits": auction.enforce_limits,
            "release_refund_mode": auction.release_refund_mode,
            "opening_price_mode": auction.opening_price_mode,
            "starting_price": str(auction.starting_price),
            # Le buste sono una regola di lega, non una preferenza della
            # serata: riprendere una sessione salvata non deve cambiare
            # sotto i piedi il modo in cui si aggiudicano i big.
            "sealed_bids": auction.sealed_bids,
            "sealed_threshold_p": auction.sealed_threshold_p,
            "sealed_threshold_d": auction.sealed_threshold_d,
            "sealed_threshold_c": auction.sealed_threshold_c,
            "sealed_threshold_a": auction.sealed_threshold_a,
            "sealed_seconds": auction.sealed_seconds,
        },
        "participants": [_snapshot_participant(p) for p in participants],
        "pool": _snapshot_pool(league),
        "cycle_results": [
            {"cycle": r.cycle, "winner_name": r.winner_name, "amount": str(r.amount),
             "player_name": r.player_name, "player_role": r.player_role, "assigned": r.assigned}
            for r in AuctionCycleResult.objects.filter(auction=auction)
        ],
    }

    return AuctionSession.objects.create(
        name=(name or auction.title or "Sessione")[:120],
        league=league,
        source_auction=auction,
        mode=auction.mode,
        current_cycle=auction.current_cycle,
        created_by=(created_by or "")[:80],
        notes=(notes or "")[:200],
        data=payload,
    )


@transaction.atomic
def snapshot_participants(participants, *, name="", created_by="", notes="",
                         league=None, source_auction=None):
    """Snapshot an explicit set of participants (+ rosters) into a resumable
    AuctionSession.

    Used by the wizard's "fresh start" so the teams it is about to wipe are not
    lost — the result can be reopened later via ``resume_session``. ``league``
    and ``source_auction`` only enrich the saved settings; when omitted,
    ``resume_session`` falls back to sane defaults.
    """
    participants = list(participants)
    league = league or next((p.league for p in participants if p.league_id), None)

    payload = {
        "league": None if league is None else {
            "name": league.name, "budget": str(league.budget),
            "slot_limits": league.slot_limits,
            "slots_p": league.slots_p, "slots_d": league.slots_d,
            "slots_c": league.slots_c, "slots_a": league.slots_a,
            "source_site": league.source_site, "external_id": league.external_id,
        },
        "auction": None if source_auction is None else {
            "title": source_auction.title, "min_increment": str(source_auction.min_increment),
            "quick_increments": source_auction.quick_increments,
            "duration_seconds": source_auction.duration_seconds,
            "antisnipe_seconds": source_auction.antisnipe_seconds,
            "enforce_limits": source_auction.enforce_limits,
            "release_refund_mode": source_auction.release_refund_mode,
            "opening_price_mode": source_auction.opening_price_mode,
            "starting_price": str(source_auction.starting_price),
        },
        "participants": [_snapshot_participant(p) for p in participants],
        "pool": _snapshot_pool(league),
        "cycle_results": [],
    }

    return AuctionSession.objects.create(
        name=(name or (league.name if league else "Lega precedente"))[:120],
        league=league,
        source_auction=source_auction,
        mode=source_auction.mode if source_auction else "",
        current_cycle=source_auction.current_cycle if source_auction else 1,
        created_by=(created_by or "")[:80],
        notes=(notes or "")[:200],
        data=payload,
    )


@transaction.atomic
def resume_session(session_id, *, created_by=""):
    """Rebuild a playable auction from a saved session (non-destructive).

    Creates a fresh League, fresh Participants (with their saved budget/spent),
    re-creates owned Players linked to those participants, and a new Auction in
    ``RESUME_SAVED`` mode pointing back at the session. Returns the new Auction.
    """
    session = AuctionSession.objects.get(pk=session_id)
    data = session.data or {}
    lg = data.get("league") or {}
    ac = data.get("auction") or {}

    def _dec(v, default="0"):
        try:
            return Decimal(str(v))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal(default)

    league = League.objects.create(
        name=(lg.get("name") or session.name or "Lega ripresa")[:120],
        source_site=lg.get("source_site", ""),
        external_id=lg.get("external_id", ""),
        budget=_dec(lg.get("budget"), "500"),
        slot_limits=bool(lg.get("slot_limits", True)),
        slots_p=int(lg.get("slots_p", 3)), slots_d=int(lg.get("slots_d", 8)),
        slots_c=int(lg.get("slots_c", 8)), slots_a=int(lg.get("slots_a", 6)),
    )

    by_name = {}
    for ps in data.get("participants", []):
        participant = Participant.objects.create(
            league=league,
            display_name=(ps.get("display_name") or "Squadra")[:80],
            external_team_id=ps.get("external_team_id", ""),
            access_code=_sec.token_hex(4),
            credits=_dec(ps.get("credits"), "500"),
            spent_credits=_dec(ps.get("spent_credits"), "0"),
            is_active=ps.get("is_active", True),
        )
        by_name[participant.display_name] = participant

    # The listone comes back whole (free agents included) — an auction cannot
    # start without a pool. Snapshots taken before the pool was stored only
    # carry the rosters, so fall back to those and, when the original league is
    # still around, copy its free agents across too.
    pool = data.get("pool")
    if pool:
        _restore_pool(pool, league, by_name)
    else:
        for ps in data.get("participants", []):
            participant = by_name.get((ps.get("display_name") or "Squadra")[:80])
            for pl in ps.get("roster", []):
                Player.objects.create(
                    league=league,
                    name=(pl.get("name") or "")[:120],
                    role=(pl.get("role") or "A")[:1],
                    team=pl.get("team", ""),
                    cost=_dec(pl.get("cost"), "0"),
                    owner=participant,
                )
        if session.league_id:
            legacy = [
                {"name": p.name, "role": p.role, "team": p.team,
                 "initial_price": str(p.initial_price), "cost": "0", "owner": None,
                 "photo_url": p.photo_url, "ext_id": p.ext_id,
                 "fvm": None if p.fvm is None else str(p.fvm),
                 "presences": p.presences,
                 "avg_vote": None if p.avg_vote is None else str(p.avg_vote),
                 "fanta_avg": None if p.fanta_avg is None else str(p.fanta_avg),
                 "goals": p.goals, "assists": p.assists}
                for p in Player.objects.filter(league_id=session.league_id, owner__isnull=True)
            ]
            if legacy:
                _restore_pool(legacy, league, {})

    auction = Auction.objects.create(
        league=league,
        resumed_from_session=session,
        title=(ac.get("title") or session.name or "Asta ripresa")[:200],
        mode=Auction.Mode.RESUME_SAVED,
        starting_price=_dec(ac.get("starting_price"), "1"),
        current_price=_dec(ac.get("starting_price"), "1"),
        min_increment=_dec(ac.get("min_increment"), "1"),
        quick_increments=ac.get("quick_increments") or "1,2,5,10",
        duration_seconds=int(ac.get("duration_seconds", 60)),
        antisnipe_seconds=int(ac.get("antisnipe_seconds", 10)),
        enforce_limits=bool(ac.get("enforce_limits", True)),
        release_refund_mode=ac.get("release_refund_mode") or Auction.RefundMode.PURCHASE,
        opening_price_mode=ac.get("opening_price_mode") or Auction.OpeningPriceMode.QUOTAZIONE,
        sealed_bids=bool(ac.get("sealed_bids", False)),
        sealed_threshold_p=int(ac.get("sealed_threshold_p", 50)),
        sealed_threshold_d=int(ac.get("sealed_threshold_d", 50)),
        sealed_threshold_c=int(ac.get("sealed_threshold_c", 100)),
        sealed_threshold_a=int(ac.get("sealed_threshold_a", 150)),
        sealed_seconds=int(ac.get("sealed_seconds", 45)),
        current_cycle=session.current_cycle,
        status=Auction.Status.READY,
    )
    return auction
