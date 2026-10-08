"""Secrets the app has to read back (the SMTP password), encrypted at rest.

Fernet (AES + HMAC) with the key from ``FM_FIELD_ENCRYPTION_KEY`` or, when
that is unset, derived from ``SECRET_KEY`` — which the Docker entrypoint
already keeps stable in the data volume. A database dump or backup then no
longer carries the mailbox password in clear. Values written before this
existed have no prefix and are read as they are; the next save encrypts them.
"""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

logger = logging.getLogger("auctions.crypto")
PREFIX = "fernet:"


def _fernet():
    key = getattr(settings, "FM_FIELD_ENCRYPTION_KEY", "") or ""
    if not key:
        digest = hashlib.sha256(("fantamanager-field-key:" + settings.SECRET_KEY).encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(value):
    if not value or value.startswith(PREFIX):
        return value
    return PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt(value):
    if not value or not value.startswith(PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode()).decode()
    except (InvalidToken, ValueError):
        # The key changed (new SECRET_KEY): the secret is lost, not leaked.
        logger.warning("Valore cifrato illeggibile: la chiave è cambiata, va reinserito.")
        return ""


class EncryptedTextField(models.TextField):
    """A text column stored encrypted, read back in clear."""

    def from_db_value(self, value, expression, connection):
        return decrypt(value)

    def get_prep_value(self, value):
        return encrypt(super().get_prep_value(value))
