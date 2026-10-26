"""Dashboard authentication: one user, password stored only as an scrypt hash in a private file under data/.

The session secret is random and stored with owner-only permissions. Nothing here is ever committed (data/ is
ignored by git and blocked by the privacy hook).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from askesis.store import state

SCRYPT = {"n": 2**15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}
MIN_LENGTH = 12


def _write_private(path: Path, text: str) -> None:
    state.write_private(path, text)


def set_password(path: Path, password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"la password deve avere almeno {MIN_LENGTH} caratteri")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    _write_private(path, json.dumps({"scheme": "scrypt", "params": SCRYPT, "salt": salt.hex(), "hash": digest.hex()}))


def check_password(path: Path, password: str) -> bool:
    if not state.exists(path):
        return False
    data = json.loads(state.read_text(path))
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(data["salt"]), **data["params"])
    return hmac.compare_digest(digest.hex(), data["hash"])


def session_secret(path: Path) -> str:
    if not state.exists(path):
        _write_private(path, secrets.token_hex(32))
    return state.read_text(path).strip()


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


# ------------------------------------------------------------------ remembered devices
# A remembered device holds a random token in an HttpOnly cookie; the server keeps only its SHA-256, a coarse label
# and a fixed expiry (never extended). The token only replaces typing the password: client allowlist, host check,
# CSRF and SameSite stay in force. Changing the password revokes every device and every open session.

def credential_epoch(auth_file: Path) -> str:
    """Changes whenever the password changes: sessions created before are no longer valid."""
    if not state.exists(auth_file):
        return ""
    return hashlib.sha256(state.read_bytes(auth_file)).hexdigest()[:16]


def _load_devices(path: Path) -> dict:
    return json.loads(state.read_text(path)) if state.exists(path) else {}


def remember(path: Path, label: str, days: int, now: float | None = None) -> tuple[str, dict]:
    """New remembered device: returns the cookie value (shown to the browser once) and the stored record."""
    now = time.time() if now is None else now
    did, token = secrets.token_hex(4), secrets.token_urlsafe(32)
    rec = {"label": label, "created": now, "expires": now + days * 86400, "last_used": now,
           "hash": hashlib.sha256(token.encode()).hexdigest()}
    devices = {k: v for k, v in _load_devices(path).items() if v["expires"] > now}  # drop expired ones
    devices[did] = rec
    _write_private(path, json.dumps(devices, indent=1))
    return f"{did}.{token}", rec | {"id": did}


def device_for(path: Path, cookie: str | None, now: float | None = None, touch: bool = False) -> dict | None:
    """The remembered device matching the cookie, if it exists and has not expired."""
    if not cookie or "." not in cookie:
        return None
    now = time.time() if now is None else now
    did, token = cookie.split(".", 1)
    devices = _load_devices(path)
    rec = devices.get(did)
    if rec is None or rec["expires"] <= now:
        return None
    if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), rec["hash"]):
        return None
    if touch:
        rec["last_used"] = now
        _write_private(path, json.dumps(devices, indent=1))
    return rec | {"id": did}


def device_alive(path: Path, did: str, now: float | None = None) -> bool:
    rec = _load_devices(path).get(did)
    return rec is not None and rec["expires"] > (time.time() if now is None else now)


def devices(path: Path, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    return sorted(({"id": k} | {x: y for x, y in v.items() if x != "hash"} for k, v in _load_devices(path).items()
                   if v["expires"] > now), key=lambda d: -d["created"])


def revoke(path: Path, did: str | None = None) -> int:
    """Revoke one device, or all of them when `did` is None. Returns how many were removed."""
    current = _load_devices(path)
    keep = {} if did is None else {k: v for k, v in current.items() if k != did}
    _write_private(path, json.dumps(keep, indent=1))
    return len(current) - len(keep)


def device_label(user_agent: str) -> str:
    """Coarse, non-identifying label from the User-Agent (no version numbers stored)."""
    ua = user_agent or ""
    kind = "iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Mac" if "Macintosh" in ua else \
        "Android" if "Android" in ua else "Windows" if "Windows" in ua else "dispositivo"
    browser = "Firefox" if "Firefox/" in ua else "Chrome" if ("Chrome/" in ua or "CriOS/" in ua) else \
        "Safari" if "Safari/" in ua else "browser"
    return f"{kind} · {browser}"


# ------------------------------------------------------------------ access log (monitoring)
def log_attempt(path: Path, method: str, ok: bool, ip: str, detail: str = "") -> None:
    """One JSON line per login attempt (password or passkey): who tried from where, never what was typed."""
    line = json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "method": method, "ok": ok, "ip": ip,
                       "detail": detail[:80]}, ensure_ascii=False)
    state.append_line(path, line)


def recent_attempts(path: Path, limit: int = 2000) -> list[dict]:
    out = []
    for line in state.tail_lines(path, limit):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
