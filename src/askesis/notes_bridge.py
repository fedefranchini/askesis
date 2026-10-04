"""Thin bridge to Apple Notes via AppleScript (macOS only). All parsing lives in ingestion/gymnote.py.

First use triggers the macOS prompt "… wants to control Notes" (Privacy & Security → Automation).
"""

from __future__ import annotations

import subprocess

FOLDER = "Askesis"


class NotesError(RuntimeError):
    pass


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run(script: str) -> str:
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:  # pragma: no cover - non-macOS
        raise NotesError("osascript non disponibile: Apple Notes funziona solo su macOS") from exc
    if out.returncode != 0:
        msg = out.stderr.strip()
        if "-1743" in msg or "not allowed" in msg.lower():
            msg += " — concedi il permesso in Impostazioni → Privacy e sicurezza → Automazione → Note"
        raise NotesError(msg)
    return out.stdout.strip()


def upsert(title: str, body_html: str, account: str = "iCloud") -> str:
    """Create or replace the note with this title in the Askesis folder. Returns the note id."""
    script = f"""
    tell application "Notes"
        tell account {_q(account)}
            if not (exists folder {_q(FOLDER)}) then make new folder with properties {{name:{_q(FOLDER)}}}
            set f to folder {_q(FOLDER)}
            set matches to (notes of f whose name is {_q(title)})
            if (count of matches) > 0 then
                set n to item 1 of matches
                set body of n to {_q(body_html)}
            else
                set n to make new note at f with properties {{body:{_q(body_html)}}}
            end if
            return id of n
        end tell
    end tell"""
    return _run(script)


def read(title: str, account: str = "iCloud") -> str | None:
    script = f"""
    tell application "Notes"
        tell account {_q(account)}
            if not (exists folder {_q(FOLDER)}) then return ""
            set matches to (notes of folder {_q(FOLDER)} whose name is {_q(title)})
            if (count of matches) = 0 then return ""
            return body of item 1 of matches
        end tell
    end tell"""
    body = _run(script)
    return body or None
