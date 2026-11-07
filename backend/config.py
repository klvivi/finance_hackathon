"""Paths and settings. The API key is read from the environment / .env only, server-side."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA = ROOT / "data"
DEMO_DIR = DATA / "demo"            # the synthetic files a user "uploads"
SEED_DIR = DATA / "seed"            # prior-month history loaded by reset
RUNTIME = DATA / "runtime"          # ledger db, uploads, generated PDFs (gitignored)
INBOX = RUNTIME / "inbox"           # uploaded files, as the backend sees them
OUT = RUNTIME / "out"               # generated report + invoice PDFs
DB_PATH = RUNTIME / "ledgerly.sqlite"
STATE_PATH = RUNTIME / "state.json"  # agent id, session cursors, run history (survives reset)
STATIC = ROOT / "static"

COMPANY = {
    "name": "Brightwave Studio LLC",
    "address": "41 Kent Ave, Suite 3, Brooklyn, NY 11249",
    "email": "billing@brightwave.example",
    "currency": "USD",
}

# Confidence below this sends a model categorization to the review list (posted to Suspense).
REVIEW_THRESHOLD = float(os.getenv("LEDGERLY_REVIEW_THRESHOLD", "0.75"))
# Similarity to an approved past transaction needed to reuse its category without the model.
SIMILARITY_THRESHOLD = 0.6

PUBLIC_BASE_URL = os.getenv("LEDGERLY_PUBLIC_URL", "").rstrip("/")  # cloudflared tunnel, optional
MODEL = os.getenv("LEDGERLY_MODEL", "")


def ensure_dirs() -> None:
    for d in (RUNTIME, INBOX, OUT, OUT / "reports", OUT / "invoices"):
        d.mkdir(parents=True, exist_ok=True)
