"""Register models in the Django admin for raw data access / debugging."""
from django.contrib import admin

from .models import (
    Auction, AuctionCycleResult, AuctionQueueItem, AuctionSession,
    Bid, League, Participant, Player,
)


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "role", "team", "league", "initial_price", "photo_url", "owner", "cost")
    list_editable = ("photo_url",)
    list_filter = ("role", "league", ("owner", admin.EmptyFieldListFilter))
    search_fields = ("name", "team")
    fields = ("league", "name", "role", "team", "initial_price", "photo_url", "owner", "cost")


@admin.register(League)
class LeagueAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "source_site", "external_id", "budget",
                    "slots_p", "slots_d", "slots_c", "slots_a", "owner")
    search_fields = ("name", "external_id")


@admin.register(Auction)
class AuctionAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "league", "mode", "status", "current_price", "min_increment", "ends_at")
    list_filter = ("status", "mode", "league")
    search_fields = ("title",)


@admin.register(AuctionQueueItem)
class AuctionQueueItemAdmin(admin.ModelAdmin):
    list_display = ("id", "auction", "order", "player", "role", "done")
    list_filter = ("done", "auction", "role")
    search_fields = ("player__name",)


@admin.register(AuctionCycleResult)
class AuctionCycleResultAdmin(admin.ModelAdmin):
    list_display = ("id", "auction", "cycle", "winner_name", "player_name",
                    "amount", "assigned", "assigned_by", "assigned_at")
    list_filter = ("assigned", "auction")
    search_fields = ("winner_name", "player_name")


@admin.register(AuctionSession)
class AuctionSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "league", "source_auction", "mode",
                    "current_cycle", "created_by", "created_at")
    list_filter = ("mode", "league")
    search_fields = ("name", "created_by")
    readonly_fields = ("created_at",)


@admin.register(Participant)
class ParticipantAdmin(admin.ModelAdmin):
    list_display = ("id", "display_name", "access_code", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("display_name", "access_code")


@admin.register(Bid)
class BidAdmin(admin.ModelAdmin):
    list_display = (
        "id", "auction", "participant", "amount", "increment",
        "accepted", "cancelled", "rejection_reason", "server_received_at",
    )
    list_filter = ("accepted", "cancelled", "auction")
    search_fields = ("participant__display_name", "rejection_reason")
    readonly_fields = ("server_received_at",)
