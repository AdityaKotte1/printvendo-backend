"""Encryption for third-party secrets held on behalf of someone else.

An owner's Razorpay key secret is stored encrypted, never in plaintext, so a
database dump does not hand over live payment credentials. Fernet gives
authenticated encryption, so a tampered ciphertext fails instead of decrypting
to garbage.

Nothing here is for passwords. Passwords are hashed, not encrypted — see
app/core/security.py.
"""

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Could not decrypt secret: wrong key or tampered data") from exc


def mask_secret(value: str) -> str:
    """What an API is allowed to return about a stored secret."""
    if not value:
        return ""
    if len(value) <= 4:
        return "••••"
    return f"••••{value[-4:]}"
