"""Configuration: public defaults + private override (never committed)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

from askesis.core.timeutil import parse_hhmm

ROOT = Path(os.environ.get("ASKESIS_ROOT", Path(__file__).resolve().parents[2]))
DEFAULT_FILE = ROOT / "config" / "askesis.default.toml"
PRIVATE_FILE = ROOT / "private" / "askesis.toml"


def reports_dir() -> Path:
    """Where generated reports live (overridable for tests and synthetic instances: never mix real and test texts)."""
    return Path(os.environ.get("ASKESIS_REPORTS_DIR", ROOT / "reports"))


@dataclass
class Config:
    timezone: str
    nutrition_cutoff: time
    weigh_time: time
    db_path: Path
    backup_dir: Path
    staging_dir: Path
    source_id: str
    context_aliases: dict[str, str] = field(default_factory=dict)
    # Dashboard access. The server always listens on 127.0.0.1 only; remote access is provided by a network layer
    # in front of it (e.g. a mesh VPN that proxies HTTPS to localhost). Changing provider = changing these values.
    web_allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1", "localhost"])
    web_secure_cookies: bool = False  # true when the dashboard is reached through HTTPS
    web_bind: list[str] = field(default_factory=lambda: ["127.0.0.1"])  # addresses to listen on (never 0.0.0.0)
    web_allowed_clients: list[str] = field(default_factory=list)  # extra client IPs besides this Mac (loopback)
    web_remember_days: int = 30  # "remember this device": fixed lifetime, never extended
    web_origin: str = ""  # https://domain when the dashboard has HTTPS on a real domain: enables passkeys
    health_exclude_sources: list[str] = field(default_factory=list)  # Health sources that are not the athlete's
    health_sync_weight: bool = False  # daily sync of body weight from Health (off: the weight stays manual)
    # Sealed backups (store/seal.py): public age keys only; the private key never lives on the server.
    backup_age_recipients: list[str] = field(default_factory=list)
    backup_sealed_dir: Path | None = None
    backup_offsite: str = ""  # rclone destination for sealed copies, e.g. "drive:askesis-backup" ("" = off)
    backup_pull_from: str = ""  # rsync source of sealed copies on the server (development machine side)
    backup_pull_dir: Path | None = None
    backup_identity: str = ""  # private key for restore tests: a file path or "keychain:<service>" (macOS)
    # Hosted database (Turso): when set, every interface uses it instead of db_path, and the app state that lived
    # in files next to the database (password hash, devices, passkeys, tokens, access log, coach texts) lives in
    # the database too. The token is never in a file: environment (hosting) or the macOS Keychain (development).
    db_url: str = ""
    db_token: str = "env:ASKESIS_DB_TOKEN"  # "env:<NAME>" or "keychain:<service>"

    @property
    def db_target(self) -> Path | str:
        return self.db_url or self.db_path

    @property
    def hosted(self) -> bool:
        return bool(self.db_url)


def _read(path: Path) -> dict:
    return tomllib.loads(path.read_text()) if path.exists() else {}


def load(private: Path | None = None) -> Config:
    data = _read(DEFAULT_FILE)
    override_path = private or Path(os.environ.get("ASKESIS_CONFIG", PRIVATE_FILE))
    # Hosting has no private folder: the private override arrives as text in an environment variable instead.
    inline = os.environ.get("ASKESIS_CONFIG_TOML", "")
    override = tomllib.loads(inline) if inline and private is None else _read(override_path)
    aliases = dict(data.get("context_aliases", {})) | dict(override.pop("context_aliases", {}))
    data.update(override)

    def path(key: str) -> Path:
        p = Path(os.environ.get(f"ASKESIS_{key.upper()}", data[key]))
        return p if p.is_absolute() else ROOT / p

    return Config(
        timezone=data["timezone"],
        nutrition_cutoff=parse_hhmm(data["nutrition_cutoff"]),
        weigh_time=parse_hhmm(data["weigh_time"]),
        db_path=path("db_path"),
        backup_dir=path("backup_dir"),
        staging_dir=path("staging_dir"),
        source_id=data["source_id"],
        context_aliases={k.lower(): v for k, v in aliases.items()},
        web_allowed_hosts=list(data.get("web_allowed_hosts", ["127.0.0.1", "localhost"])),
        web_secure_cookies=bool(data.get("web_secure_cookies", False)),
        web_bind=list(data.get("web_bind", ["127.0.0.1"])),
        web_allowed_clients=list(data.get("web_allowed_clients", [])),
        web_remember_days=int(data.get("web_remember_days", 30)),
        web_origin=str(os.environ.get("ASKESIS_WEB_ORIGIN", data.get("web_origin", ""))),
        health_exclude_sources=list(data.get("health_exclude_sources", [])),
        health_sync_weight=bool(data.get("health_sync_weight", False)),
        backup_age_recipients=list(data.get("backup_age_recipients", [])),
        backup_sealed_dir=path("backup_sealed_dir"),
        backup_offsite=str(data.get("backup_offsite", "")),
        backup_pull_from=str(data.get("backup_pull_from", "")),
        backup_pull_dir=path("backup_pull_dir"),
        backup_identity=str(data.get("backup_identity", "")),
        db_url=str(os.environ.get("ASKESIS_DB_URL", data.get("db_url", ""))),
        db_token=str(os.environ.get("ASKESIS_DB_TOKEN_SOURCE", data.get("db_token", "env:ASKESIS_DB_TOKEN"))),
    )
