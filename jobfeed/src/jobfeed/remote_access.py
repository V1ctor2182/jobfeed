"""The access code that lets a browser reach the dashboard through a tunnel.

The dashboard has no accounts. Requests made on the server itself stay open, and
requests that arrive through a tunnel need this code, entered once per browser.
Only a hash of the code is stored; the browser keeps a signed session cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

# 31 letters and digits that cannot be confused with each other when read
# aloud or copied from a chat: no 0/O, 1/I/L.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_GROUPS = 4
_GROUP_LENGTH = 4
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


@dataclass(frozen=True)
class RemoteAccess:
    """The stored hash of the current code and the key that signs sessions."""

    salt: bytes
    code_hash: bytes
    session_key: bytes

    def accepts(self, code: str) -> bool:
        """Return whether ``code`` is the current access code.

        Args:
            code: Code as the user typed it; case, spaces and dashes are ignored.

        Returns:
            True for the current code.
        """
        return hmac.compare_digest(
            _hash(normalize_code(code), self.salt), self.code_hash
        )

    def session_token(self) -> str:
        """Return the cookie value that proves a browser entered the code.

        Returns:
            A token that changes whenever a new code is created.
        """
        return hmac.new(
            self.session_key, b"jobfeed-remote-session", "sha256"
        ).hexdigest()


class RemoteAccessStore:
    """Read and replace ``data/remote-access.json``.

    Args:
        path: File that holds the code hash; absent means remote access is off.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cached: tuple[int, RemoteAccess] | None = None

    def load(self) -> RemoteAccess | None:
        """Return the current access settings, or None when remote access is off.

        Returns:
            The stored settings, re-read only when the file changes.
        """
        try:
            stamp = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._cached = None
            return None
        if self._cached is None or self._cached[0] != stamp:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._cached = (
                stamp,
                RemoteAccess(
                    salt=bytes.fromhex(raw["salt"]),
                    code_hash=bytes.fromhex(raw["code_hash"]),
                    session_key=bytes.fromhex(raw["session_key"]),
                ),
            )
        return self._cached[1]

    def create(self) -> str:
        """Turn remote access on with a new code, signing out every browser.

        Returns:
            The new code. It is not stored and cannot be shown again.
        """
        code = "-".join(
            "".join(secrets.choice(_ALPHABET) for _ in range(_GROUP_LENGTH))
            for _ in range(_GROUPS)
        )
        salt = secrets.token_bytes(16)
        payload = {
            "salt": salt.hex(),
            "code_hash": _hash(normalize_code(code), salt).hex(),
            "session_key": secrets.token_bytes(32).hex(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        temporary.replace(self.path)
        return code

    def delete(self) -> bool:
        """Turn remote access off; tunnel requests are refused afterwards.

        Returns:
            Whether remote access was on.
        """
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True


def normalize_code(code: str) -> str:
    """Return the code without case, spaces or dashes.

    Args:
        code: Code as typed.

    Returns:
        Upper-case letters and digits only.
    """
    return "".join(character for character in code.upper() if character.isalnum())


def _hash(code: str, salt: bytes) -> bytes:
    return hashlib.scrypt(code.encode(), salt=salt, **_SCRYPT)


__all__ = ["RemoteAccess", "RemoteAccessStore", "normalize_code"]
