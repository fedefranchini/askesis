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


def _read(path: Path) -> dict:
    return tomllib.loads(path.read_text()) if path.exists() else {}


def load(private: Path | None = None) -> Config:
    data = _read(DEFAULT_FILE)
    override_path = private or Path(os.environ.get("ASKESIS_CONFIG", PRIVATE_FILE))
    override = _read(override_path)
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
    )
