"""Roster-import providers (Fantapazz today; Fantacalcio.it / Leghe / Excel later).

The provider layer isolates *where roster data comes from* (a website, an
uploaded file) from *how the app stores it* (see ``importers``) and from the
HTTP/session glue (see ``views``). Add a new site by subclassing
``RosterProvider`` and registering it in ``get_provider``.
"""
from .base import ProviderError, RosterProvider, get_provider

__all__ = ["ProviderError", "RosterProvider", "get_provider"]
