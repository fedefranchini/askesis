"""launchd user agents (macOS): generated as XML, installed only on explicit request.

Shared by the backup agents and the dashboard agent. Nothing here runs at import time.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


def plist(label: str, args: list[str], path_env: str, log: Path, calendar: dict[str, int] | None = None,
          run_at_load: bool = False, keep_alive: bool = False) -> str:
    argv = "".join(f"\n        <string>{a}</string>" for a in args)
    extra = ""
    if calendar:
        cal = "".join(f"\n        <key>{k}</key><integer>{v}</integer>" for k, v in calendar.items())
        extra += f"\n    <key>StartCalendarInterval</key>\n    <dict>{cal}\n    </dict>"
    if run_at_load:
        extra += "\n    <key>RunAtLoad</key><true/>"
    if keep_alive:  # restart after a crash, never in a tight loop
        extra += ("\n    <key>KeepAlive</key>\n    <dict>\n        <key>SuccessfulExit</key><false/>\n    </dict>"
                  "\n    <key>ThrottleInterval</key><integer>30</integer>")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>{argv}
    </array>{extra}
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>{path_env}:/usr/bin:/bin</string>
    </dict>
    <key>StandardOutPath</key><string>{log}</string>
    <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def install(agents: dict[str, str]) -> list[str]:
    domain = f"gui/{os.getuid()}"
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    done = []
    for label, xml in agents.items():
        path = AGENTS_DIR / f"{label}.plist"
        path.write_text(xml)
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], check=False, capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
        done.append(label)
    return done


def uninstall(labels: list[str]) -> None:
    domain = f"gui/{os.getuid()}"
    for label in labels:
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], check=False, capture_output=True)
        (AGENTS_DIR / f"{label}.plist").unlink(missing_ok=True)
