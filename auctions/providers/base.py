"""Provider interface shared by every roster source.

A provider turns an external source (a fantasy site, an uploaded file) into a
normalised list of *teams*, each shaped like::

    {
        "name": "GELSI UNITED",
        "credits": 2168,            # remaining credits on the source, or None
        "external_id": "1449",      # team id on the source, optional
        "players": [
            {"role": "P", "name": "Falcone", "cost": 9, "club": "Lecce"},
            ...
        ],
    }

``importers.import_rose_data`` consumes exactly this shape, so any provider that
emits it gets DB import + budget reconstruction for free.
"""
from abc import ABC, abstractmethod


class ProviderError(Exception):
    """Raised for expected, user-facing provider failures (bad auth, HTTP error).

    The message is safe to show in the UI.
    """


class RosterProvider(ABC):
    #: short stable key used in URLs / Auction.source_site
    name = "base"
    #: human label for the UI
    label = "Provider"

    def authenticate(self, **kwargs):
        """Return an authenticated handle (e.g. a requests.Session) or raise
        ProviderError. File-based providers may ignore this."""
        raise NotImplementedError

    def list_leagues(self, handle):
        """Return ``[{"id": ..., "name": ...}, ...]`` for the logged-in user."""
        raise NotImplementedError

    @abstractmethod
    def fetch_rosters(self, handle, league_id, team_ids=None):
        """Return ``(teams, errors)`` — ``teams`` in the normalised shape above."""

    @staticmethod
    def parse_rosters(raw):
        """Parse a single raw payload (HTML/file bytes) into normalised data."""
        raise NotImplementedError

    def import_rosters(self, teams, replace=False):
        """Persist normalised ``teams`` to the DB. Defaults to the shared importer."""
        from . import importers
        return importers.import_rose_data(teams, replace=replace)

    def sync_budget(self, league_id=None):
        """Recompute participants' remaining budget from imported costs."""
        from . import importers
        return importers.sync_budget()


_REGISTRY = {}


def register(provider_cls):
    _REGISTRY[provider_cls.name] = provider_cls
    return provider_cls


def get_provider(name):
    """Return a provider instance by name, or raise ProviderError if unknown."""
    # Import built-ins lazily to avoid import cycles.
    from . import fantapazz  # noqa: F401  (registers FantapazzProvider)

    cls = _REGISTRY.get((name or "").strip().lower())
    if cls is None:
        raise ProviderError(f"Provider sconosciuto: {name!r}")
    return cls()
