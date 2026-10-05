"""Dashboard authentication: one user, password stored only as an scrypt hash in a private file under data/.

The session secret is random and stored with owner-only permissions. Nothing here is ever committed (data/ is
ignored by git and blocked by the privacy hook).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

SCRYPT = {"n": 2**15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}
MIN_LENGTH = 12


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def set_password(path: Path, password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"la password deve avere almeno {MIN_LENGTH} caratteri")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    _write_private(path, json.dumps({"scheme": "scrypt", "params": SCRYPT, "salt": salt.hex(), "hash": digest.hex()}))


def check_password(path: Path, password: str) -> bool:
    if not path.exists():
        return False
    data = json.loads(path.read_text())
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(data["salt"]), **data["params"])
    return hmac.compare_digest(digest.hex(), data["hash"])


def session_secret(path: Path) -> str:
    if not path.exists():
        _write_private(path, secrets.token_hex(32))
    return path.read_text().strip()


class Throttle:
    """Exponential delay after failed logins (in memory, single local process)."""

    def __init__(self) -> None:
        self.failures = 0
        self.until = 0.0

    def blocked_for(self) -> float:
        return max(0.0, self.until - time.monotonic())

    def fail(self) -> None:
        self.failures += 1
        self.until = time.monotonic() + min(2 ** self.failures, 300)

    def success(self) -> None:
        self.failures, self.until = 0, 0.0
