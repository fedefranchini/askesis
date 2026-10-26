"""Vercel entry point: the hosted dashboard. All configuration comes from environment variables
(see src/askesis/web/vercel.py); nothing private is in the repository."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from askesis.web.vercel import app  # noqa: E402,F401
