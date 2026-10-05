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


def latest(directory: Path) -> Path | None:
    files = sorted(p for p in directory.iterdir() if PATTERN.match(p.name)) if directory.exists() else []
    return files[-1] if files else None


def verify(live: sqlite3.Connection, snapshot: Path, workdir: Path) -> dict:
    """Restore test: open a copy of the snapshot and check that it is a usable, consistent past state of the live DB.

    1. integrity_check on the restored copy;
    2. append-only containment: every RAW record, plan version and intervention event of the snapshot exists
       unchanged in the live database;
    3. metric digest: metrics recomputed from the restored copy equal those recomputed from the live database at the
       same knowledge cutoff (the snapshot's last recorded_at)."""
    import shutil

    from askesis.analytics import engine
    from askesis.store.db import connect

    workdir.mkdir(parents=True, exist_ok=True)
    copy = workdir / f"restore-test-{snapshot.name}"
    shutil.copyfile(snapshot, copy)
    checks: dict = {"snapshot": snapshot.name}
    try:
        restored = connect(copy)
        checks["integrity"] = restored.execute("PRAGMA integrity_check").fetchone()[0]
        missing = {}
        for table, key in (("raw_record", "id, payload_hash"), ("plan_version", "id, content_hash"),
                           ("intervention_event", "id, event")):
            snap = set(map(tuple, restored.execute(f"SELECT {key} FROM {table}").fetchall()))  # noqa: S608
            here = set(map(tuple, live.execute(f"SELECT {key} FROM {table}").fetchall()))  # noqa: S608
            checks[f"{table}_count"] = len(snap)
            missing[table] = len(snap - here)
        checks["missing_or_changed_in_live"] = missing
        cutoff_s = restored.execute("SELECT MAX(recorded_at) FROM raw_record").fetchone()[0]
        if cutoff_s:
            cutoff = datetime.fromisoformat(cutoff_s)
            a, b = engine.load_inputs(restored, cutoff), engine.load_inputs(live, cutoff)
            if a.dates:
                start, end = min(a.dates), max(a.dates)
                checks["metrics_digest_equal"] = (engine.digest(engine.compute(a, start, end))
                                                  == engine.digest(engine.compute(b, start, end)))
        restored.close()
    finally:
        copy.unlink(missing_ok=True)
    checks["ok"] = (checks.get("integrity") == "ok" and not any(checks["missing_or_changed_in_live"].values())
                    and checks.get("metrics_digest_equal", True))
    return checks


def launch_agents(root: Path, uv: str, log_dir: Path) -> dict[str, str]:
    """launchd agents (plist XML by label): daily backup and weekly restore test. Nothing is written here."""
    def plist(label: str, args: list[str], calendar: dict[str, int]) -> str:
        argv = "".join(f"\n        <string>{a}</string>" for a in args)
        cal = "".join(f"\n        <key>{k}</key><integer>{v}</integer>" for k, v in calendar.items())
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>{argv}
    </array>
    <key>StartCalendarInterval</key>
    <dict>{cal}
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>{Path(uv).parent}:/usr/bin:/bin</string>
    </dict>
    <key>StandardOutPath</key><string>{log_dir / (label + ".log")}</string>
    <key>StandardErrorPath</key><string>{log_dir / (label + ".log")}</string>
</dict>
</plist>
"""
    ak = str(root / "bin" / "ak")
    return {
        "local.askesis.backup": plist("local.askesis.backup", [ak, "backup"], {"Hour": 2, "Minute": 30}),
        "local.askesis.restore-test": plist("local.askesis.restore-test", [ak, "backup", "verify", "--notify"],
                                            {"Weekday": 0, "Hour": 3, "Minute": 0}),
    }
