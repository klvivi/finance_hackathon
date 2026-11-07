"""Double-entry posting. Every entry must balance; entries are never edited, only reversed."""
from __future__ import annotations

import sqlite3

from . import coa
from .db import audit, now


class LedgerError(ValueError):
    pass


def post(
    c: sqlite3.Connection,
    *,
    date: str,
    memo: str,
    lines: list[tuple[str, int, int]],  # (account, debit_minor, credit_minor)
    kind: str,
    actor: str,
    txn_id: int | None = None,
    invoice_id: int | None = None,
    reverses_entry_id: int | None = None,
) -> int:
    if not lines or len(lines) < 2:
        raise LedgerError("an entry needs at least two lines")
    dr = cr = 0
    for acct, d, k in lines:
        if not coa.is_valid(acct):
            raise LedgerError(f"unknown account {acct!r}; use the chart of accounts")
        if not isinstance(d, int) or not isinstance(k, int) or d < 0 or k < 0 or (d and k):
            raise LedgerError("each line is a non-negative integer debit OR credit in minor units")
        dr += d
        cr += k
    if dr != cr:
        raise LedgerError(f"entry does not balance: debits {dr} != credits {cr}")
    if dr == 0:
        raise LedgerError("entry has zero value")
    cur = c.execute(
        "INSERT INTO journal_entries (date, memo, txn_id, invoice_id, reverses_entry_id, kind, created_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (date, memo, txn_id, invoice_id, reverses_entry_id, kind, actor, now()),
    )
    eid = cur.lastrowid
    c.executemany(
        "INSERT INTO journal_lines (entry_id, account, debit_minor, credit_minor) VALUES (?,?,?,?)",
        [(eid, a, d, k) for a, d, k in lines if d or k],
    )
    audit(c, actor, "post_entry", {"entry_id": eid, "kind": kind, "memo": memo, "amount_minor": dr})
    return eid


def lines_for_txn(funding: str, category: str, amount_minor: int) -> list[tuple[str, int, int]]:
    """A bank/card/receipt transaction becomes two lines between its funding account and its category."""
    a = abs(amount_minor)
    if amount_minor < 0:   # money out of the funding account
        return [(category, a, 0), (funding, 0, a)]
    return [(funding, a, 0), (category, 0, a)]


def reverse(c: sqlite3.Connection, entry_id: int, *, date: str, reason: str, actor: str) -> int:
    e = c.execute("SELECT * FROM journal_entries WHERE id=?", (entry_id,)).fetchone()
    if e is None:
        raise LedgerError(f"entry {entry_id} not found")
    if c.execute("SELECT 1 FROM journal_entries WHERE reverses_entry_id=?", (entry_id,)).fetchone():
        raise LedgerError(f"entry {entry_id} is already reversed")
    ls = c.execute("SELECT account, debit_minor, credit_minor FROM journal_lines WHERE entry_id=?", (entry_id,)).fetchall()
    return post(
        c, date=date, memo=f"Reversal of #{entry_id}: {reason}",
        lines=[(r["account"], r["credit_minor"], r["debit_minor"]) for r in ls],
        kind="reversal", actor=actor, txn_id=e["txn_id"], invoice_id=e["invoice_id"], reverses_entry_id=entry_id,
    )


def active_txn_entry(c: sqlite3.Connection, txn_id: int) -> int | None:
    """Latest unreversed entry posted for a transaction."""
    r = c.execute(
        "SELECT e.id FROM journal_entries e WHERE e.txn_id=? AND e.kind IN ('txn','reclass','payment')"
        " AND NOT EXISTS (SELECT 1 FROM journal_entries r WHERE r.reverses_entry_id=e.id)"
        " ORDER BY e.id DESC LIMIT 1",
        (txn_id,),
    ).fetchone()
    return r["id"] if r else None


def balances(c: sqlite3.Connection, *, start: str | None = None, end: str | None = None) -> dict[str, int]:
    """Net debit-positive balance per account over [start, end] (dates inclusive, ISO)."""
    sql = ("SELECT l.account, SUM(l.debit_minor) - SUM(l.credit_minor) AS net FROM journal_lines l"
           " JOIN journal_entries e ON e.id = l.entry_id WHERE 1=1")
    args: list = []
    if start:
        sql += " AND e.date >= ?"
        args.append(start)
    if end:
        sql += " AND e.date <= ?"
        args.append(end)
    sql += " GROUP BY l.account"
    return {r["account"]: int(r["net"]) for r in c.execute(sql, args).fetchall()}


def trial_balance_ok(c: sqlite3.Connection) -> bool:
    r = c.execute("SELECT SUM(debit_minor) - SUM(credit_minor) AS d FROM journal_lines").fetchone()
    return (r["d"] or 0) == 0
