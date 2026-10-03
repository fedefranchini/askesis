"""Online SQLite backups with rotation: keep the newest backup of each of the last 7 days and of the
last 4 ISO weeks. Only files created by this module (askesis-YYYYMMDD-HHMMSS.db) are ever removed."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

PATTERN = re.compile(r"^askesis-(\d{8})-(\d{6})\.db$")


def backup(conn: sqlite3.Connection, directory: Path, now: datetime) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"askesis-{now:%Y%m%d-%H%M%S}.db"
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def rotate(directory: Path, keep_daily: int = 7, keep_weekly: int = 4) -> list[Path]:
    files = []
    for p in directory.iterdir():
        if m := PATTERN.match(p.name):
            files.append((datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S"), p))
    files.sort(reverse=True)
    keep: set[Path] = set()
    days, weeks = [], []
    for ts, p in files:
        day, week = ts.date(), ts.isocalendar()[:2]
        if day not in days and len(days) < keep_daily:
            days.append(day)
            keep.add(p)
        if week not in weeks and len(weeks) < keep_weekly:
            weeks.append(week)
            keep.add(p)
    removed = [p for _, p in files if p not in keep]
    for p in removed:
        p.unlink()
    return removed
