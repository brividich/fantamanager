"""Models package for auctions.

Re-exports all data models, choices, and utilities for 100% backward compatibility
with `from auctions.models import ...` and `from .models import ...`.
"""
from .algo import AlgoReference, AlgoSample, AlgoSettingsVersion
from .auction import (
    Auction,
    AuctionCycleResult,
    AuctionQueueItem,
)
from .bidding import (
    Bid,
    SealedBid,
)
from .championship import (
    Competition,
    Fixture,
    Formation,
    Giornata,
    GiornataScore,
    MatchdayFormation,
    PlayerPerformance,
    Season,
)
from .core import generate_public_token
from .footballer import Footballer
from .league import (
    League,
    LeagueConfig,
)
from .trade import Trade, TradeWindow
from .contract import ContractEvent
from .season import CapEntry, CapPhase, DecreeAward, LeagueRanking, UefaClubRank
from .mail import MailSettings
from .market import (
    MarketBid,
    MarketSession,
)
from .participant import (
    CoAdminInvite,
    ManagedAccount,
    Participant,
    Watch,
)
from .privacy import AccountPrivacy, AuditLog, LegalAcceptance
from .player import (
    Player,
    RosterLog,
)
from .session import AuctionSession

__all__ = [
    "AlgoReference",
    "AlgoSample",
    "AlgoSettingsVersion",
    "generate_public_token",
    "League",
    "LeagueConfig",
    "RosterLog",
    "Player",
    "Footballer",
    "AuctionSession",
    "Auction",
    "AuctionQueueItem",
    "AuctionCycleResult",
    "Participant",
    "ManagedAccount",
    "CoAdminInvite",
    "AccountPrivacy",
    "AuditLog",
    "LegalAcceptance",
    "Watch",
    "Bid",
    "SealedBid",
    "MailSettings",
    "MarketSession",
    "MarketBid",
    "Trade",
    "TradeWindow",
    "ContractEvent",
    "LeagueRanking",
    "CapPhase",
    "CapEntry",
    "DecreeAward",
    "UefaClubRank",
    "Formation",
    "Competition",
    "Season",
    "Giornata",
    "PlayerPerformance",
    "GiornataScore",
    "MatchdayFormation",
    "Fixture",
]
