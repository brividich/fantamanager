"""Core model utilities for auctions."""
import secrets


def generate_public_token():
    """Unguessable URL-safe token for public surfaces (join links, TV screen)."""
    return secrets.token_urlsafe(16)
