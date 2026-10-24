"""Secrets: app key handling, encryption at rest, random tokens, password hashing."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

# argon2id with library defaults (RFC 9106 "low memory" profile: t=3, m=64 MiB, p=4).
_hasher = PasswordHasher()
# Used to burn the same time for unknown users as for real ones (no user enumeration by timing).
_DUMMY_HASH = _hasher.hash("timing-equaliser")

MIN_PASSWORD_LENGTH = 12


def load_or_create_app_key(data_dir: str, configured: str) -> str:
    """Return the configured key or a persisted random one (created on first start)."""
    if configured:
        return configured
    path = Path(data_dir) / "secret.key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 from the start - never world-readable, not even briefly.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    log.info("Generated new APP_SECRET_KEY in %s", path)
    return key


class Encryptor:
    """Fernet (AES-128-CBC + HMAC) with a key derived from the app secret."""

    def __init__(self, app_key: str):
        digest = hashlib.sha256(b"pve-dashboard/fernet/v1" + app_key.encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Cannot decrypt - APP_SECRET_KEY changed?") from exc


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-ish time: also hashes when the user does not exist (``password_hash=None``)."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def password_problem(password: str, username: str = "") -> str | None:
    """Minimal policy: length, not the username. Returns an error text or None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen lang sein"
    if username and username.lower() in password.lower():
        return "Passwort darf den Benutzernamen nicht enthalten"
    return None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
