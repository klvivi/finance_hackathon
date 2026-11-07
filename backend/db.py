"""SQLite ledger: the single source of truth. Journal tables are append-only (enforced by triggers)."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, kind TEXT, sha256 TEXT,
  status TEXT DEFAULT 'uploaded', workspace_path TEXT, ingested_at TEXT, note TEXT
);

CREATE TABLE IF NOT EXISTS clients (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT, aliases TEXT DEFAULT '[]',
  terms_days INTEGER DEFAULT 30, revenue_account TEXT DEFAULT '4000'
);

-- One normalized shape for every source: bank CSV, card PDF, receipt image.
CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  amount_minor INTEGER NOT NULL,       -- signed, from the funding account's view (+ in, - out)
  currency TEXT NOT NULL DEFAULT 'USD',
  counterparty TEXT NOT NULL,
  description TEXT NOT NULL,
  source TEXT NOT NULL,                -- bank_csv | card_pdf | receipt | history
  source_ref TEXT,                     -- file:row
  funding_account TEXT NOT NULL,       -- 1000 checking, 2000 card, 3000 paid personally
  fingerprint TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',  -- new | categorized | review | posted | linked | duplicate
  category TEXT, confidence REAL, cat_method TEXT, cat_reason TEXT,
  receipt_file TEXT, linked_txn_id INTEGER, invoice_id INTEGER,
  receipt_status TEXT NOT NULL DEFAULT 'n/a',   -- ok | missing | waived | n/a (income, transfers)
  document TEXT,                                -- supporting document for income (invoice no., statement)
  period TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_txn_period ON transactions(period);
CREATE INDEX IF NOT EXISTS ix_txn_fp ON transactions(fingerprint);

CREATE TABLE IF NOT EXISTS journal_entries (
  id INTEGER PRIMARY KEY, date TEXT NOT NULL, memo TEXT NOT NULL,
  txn_id INTEGER, invoice_id INTEGER, reverses_entry_id INTEGER,
  kind TEXT NOT NULL,                 -- txn | invoice | payment | reversal | reclass | opening | manual
  created_by TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal_lines (
  id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL REFERENCES journal_entries(id),
  account TEXT NOT NULL, debit_minor INTEGER NOT NULL DEFAULT 0, credit_minor INTEGER NOT NULL DEFAULT 0,
  CHECK (debit_minor >= 0 AND credit_minor >= 0 AND (debit_minor = 0 OR credit_minor = 0))
);
CREATE INDEX IF NOT EXISTS ix_lines_entry ON journal_lines(entry_id);
CREATE INDEX IF NOT EXISTS ix_lines_acct ON journal_lines(account);

CREATE TRIGGER IF NOT EXISTS je_no_update BEFORE UPDATE ON journal_entries
BEGIN SELECT RAISE(ABORT, 'journal entries are immutable; post a reversing entry'); END;
CREATE TRIGGER IF NOT EXISTS je_no_delete BEFORE DELETE ON journal_entries
BEGIN SELECT RAISE(ABORT, 'journal entries are immutable; post a reversing entry'); END;
CREATE TRIGGER IF NOT EXISTS jl_no_update BEFORE UPDATE ON journal_lines
BEGIN SELECT RAISE(ABORT, 'journal lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS jl_no_delete BEFORE DELETE ON journal_lines
BEGIN SELECT RAISE(ABORT, 'journal lines are immutable'); END;

CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY, date TEXT, client_id TEXT, project TEXT, description TEXT,
  billing TEXT, hours REAL, rate_minor INTEGER, fixed_fee_minor INTEGER, amount_minor INTEGER,
  invoice_id INTEGER, source_ref TEXT, period TEXT
);

CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY, number TEXT UNIQUE, client_id TEXT, issue_date TEXT, due_date TEXT,
  total_minor INTEGER NOT NULL, status TEXT NOT NULL,  -- draft | sent | partially_paid | paid
  entry_id INTEGER, pdf_path TEXT, workspace_path TEXT, notes TEXT, created_by TEXT, created_at TEXT,
  sent_at TEXT
);
CREATE TABLE IF NOT EXISTS invoice_lines (
  id INTEGER PRIMARY KEY, invoice_id INTEGER, description TEXT, qty REAL,
  unit_price_minor INTEGER, amount_minor INTEGER, delivery_id INTEGER
);
CREATE TABLE IF NOT EXISTS invoice_links (
  invoice_id INTEGER, txn_id INTEGER, kind TEXT, PRIMARY KEY (invoice_id, txn_id, kind)
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY, txn_id INTEGER, invoice_id INTEGER, amount_minor INTEGER,
  fee_minor INTEGER DEFAULT 0, entry_id INTEGER, created_at TEXT
);

CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY, pattern TEXT NOT NULL, account TEXT NOT NULL,
  origin TEXT NOT NULL,              -- builtin | learned (from agent memory or a review decision)
  note TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY, kind TEXT, invoice_id INTEGER, to_addr TEXT, subject TEXT, body TEXT,
  channel TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS close_summaries (
  period TEXT PRIMARY KEY, markdown TEXT, pdf_path TEXT, workspace_path TEXT, created_at TEXT
);

-- Balances printed on the bank statement (pulled from the transactions CSV), used for reconciliation.
CREATE TABLE IF NOT EXISTS statements (
  file TEXT, account TEXT, period TEXT, opening_minor INTEGER, closing_minor INTEGER,
  first_date TEXT, last_date TEXT, PRIMARY KEY (file, account)
);

CREATE TABLE IF NOT EXISTS contractors (
  name TEXT PRIMARY KEY, match TEXT NOT NULL DEFAULT '[]', form TEXT NOT NULL DEFAULT '',
  needs_1099 INTEGER NOT NULL DEFAULT 1, ytd_before_minor INTEGER NOT NULL DEFAULT 0
);

-- Human decisions on open items that are not a transaction field (reconciliation sign-off, missing documents).
CREATE TABLE IF NOT EXISTS open_item_resolutions (
  key TEXT PRIMARY KEY, resolution TEXT, note TEXT, resolved_by TEXT, resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS deliveries_log (
  period TEXT, version INTEGER, status TEXT, pdf_path TEXT, open_items INTEGER, created_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, action TEXT, detail TEXT
);

CREATE TABLE IF NOT EXISTS tool_calls (
  call_id TEXT PRIMARY KEY, name TEXT, input TEXT, result TEXT, is_error INTEGER,
  session_id TEXT, created_at TEXT
);
"""

_lock = threading.RLock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect()
        _conn.executescript(SCHEMA)
    return _conn


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


@contextmanager
def tx():
    """Serialized write transaction."""
    with _lock:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def q(sql: str, *args) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, args).fetchall()


def q1(sql: str, *args) -> sqlite3.Row | None:
    with _lock:
        return conn().execute(sql, args).fetchone()


def audit(c: sqlite3.Connection, actor: str, action: str, detail: dict | str) -> None:
    c.execute(
        "INSERT INTO audit_log (ts, actor, action, detail) VALUES (?,?,?,?)",
        (now(), actor, action, detail if isinstance(detail, str) else json.dumps(detail, default=str)),
    )


def rows(rs) -> list[dict]:
    return [dict(r) for r in rs]
