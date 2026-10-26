"""Secrets kept in the database are encrypted with Fernet (D12): AES with an HMAC, so a
tampered value fails to decrypt rather than decrypting to garbage. The key is
`ENCRYPTION_KEY`, which lives only in the server's environment, never in the database,
so a stolen database dump doesn't reveal the TOTP secret (or, from P5-7, exchange keys).
"""

from cryptography.fernet import Fernet
from pydantic import SecretStr


class Secrets:
    def __init__(self, key: SecretStr) -> None:
        self._fernet = Fernet(key.get_secret_value())

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, sealed: str) -> str:
        """Raises `cryptography.fernet.InvalidToken` if it was tampered with or made
        with another key."""
        return self._fernet.decrypt(sealed.encode()).decode()
