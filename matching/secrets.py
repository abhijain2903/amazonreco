"""Encryption for the one secret admins may enter in the app (the Claude API key).

The encryption key never lives in the database: it comes from HUB_SECRETS_KEY on the server, or is derived from
DJANGO_SECRET_KEY. A copy of the database alone cannot reveal the API key. If the server key changes, the stored
key can no longer be read and the admin is asked to enter it again.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    seed = getattr(settings, "SECRETS_KEY", "") or settings.SECRET_KEY
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(f"mehub-secrets:{seed}".encode()).digest()))


def encrypt(text):
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token):
    """The secret, or None when it cannot be read (no value, or the server key changed)."""
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None
