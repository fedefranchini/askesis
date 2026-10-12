"""User-visible alerts. macOS: a notification-centre banner. Elsewhere (e.g. a systemd service): a line on stderr, which
ends up in the journal, plus an optional append-only alerts file named by ASKESIS_ALERTS_FILE. Never raises."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def send(text: str, title: str = "Askesis") -> None:
    try:
        if sys.platform == "darwin":
            safe = text.replace("\\", "").replace('"', "'")
            safe_title = title.replace("\\", "").replace('"', "'")
            subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "{safe_title}"'],
                           check=False)
            return
        print(f"{title}: {text}", file=sys.stderr, flush=True)
        target = os.environ.get("ASKESIS_ALERTS_FILE")
        if target:
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            flat = lambda s: " ".join(s.split())  # noqa: E731  one line per alert
            with path.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')}\t{flat(title)}\t{flat(text)}\n")
    except Exception:  # noqa: BLE001  an alert must never break the caller
        pass
