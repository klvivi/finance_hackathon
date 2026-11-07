"""Invoices, payment matching, AR. All totals are computed here from delivery records."""
from __future__ import annotations

import json
from datetime import date, timedelta

from . import coa, journal
from .db import audit, now, q, q1, rows, tx
from .ingest import norm_tokens
from .journal import LedgerError


def invoice_balance(c, inv_id: int) -> int:
    inv = c.execute("SELECT total_minor FROM invoices WHERE id=?", (inv_id,)).fetchone()
    paid = c.execute("SELECT COALESCE(SUM(amount_minor + fee_minor),0) AS p FROM payments WHERE invoice_id=?",
                     (inv_id,)).fetchone()["p"]
    return inv["total_minor"] - paid


def open_invoices(c, as_of: str | None = None) -> list[dict]:
    out = []
    for inv in rows(c.execute("SELECT i.*, cl.name AS client_name, cl.email FROM invoices i JOIN clients cl ON cl.id=i.client_id"
                              " WHERE i.status IN ('draft','sent','partially_paid') ORDER BY i.due_date")):
        if as_of and inv["issue_date"] > as_of:
            continue
        bal = invoice_balance(c, inv["id"])
        if bal > 0:
            days = (date.fromisoformat(as_of) - date.fromisoformat(inv["due_date"])).days if as_of else 0
            out.append({"invoice_id": inv["id"], "number": inv["number"], "client_id": inv["client_id"],
                        "client": inv["client_name"], "email": inv["email"], "issue_date": inv["issue_date"],
                        "due_date": inv["due_date"], "total_minor": inv["total_minor"], "balance_minor": bal,
                        "status": inv["status"], "days_overdue": max(days, 0)})
    return out


def payment_candidates(c, period: str) -> list[dict]:
    """Unmatched deposits with invoice suggestions scored by client alias + amount (deterministic)."""
    clients = rows(c.execute("SELECT * FROM clients"))
    invs = open_invoices(c)
    out = []
    for t in rows(c.execute("SELECT * FROM transactions WHERE period=? AND amount_minor>0 AND funding_account='1000'"
                            " AND status IN ('new','categorized','review') AND invoice_id IS NULL", (period,))):
        d = t["description"].upper()
        client = next((cl for cl in clients if any(a in d for a in json.loads(cl["aliases"]))), None)
        sugg = []
        for inv in invs:
            score, why = 0.0, []
            if client and inv["client_id"] == client["id"]:
                score += 0.6
                why.append("client name in descriptor")
            if inv["number"] in d:
                score += 0.3
                why.append("invoice number in descriptor")
            if inv["balance_minor"] == t["amount_minor"]:
                score += 0.3
                why.append("exact open balance")
            elif t["amount_minor"] < inv["balance_minor"] and client and inv["client_id"] == client["id"]:
                why.append("partial payment")
            if score >= 0.6:
                sugg.append({"invoice_id": inv["invoice_id"], "number": inv["number"], "client": inv["client"],
                             "balance_minor": inv["balance_minor"], "score": round(min(score, 1.0), 2), "why": why})
        out.append({"txn_id": t["id"], "date": t["date"], "amount_minor": t["amount_minor"], "description": t["description"],
                    "client_guess": client["name"] if client else None,
                    "suggested_invoices": sorted(sugg, key=lambda s: -s["score"])})
    return out


def unbilled_deliveries(c, period: str | None = None) -> list[dict]:
    sql = ("SELECT d.*, cl.name AS client_name FROM deliveries d JOIN clients cl ON cl.id=d.client_id"
           " WHERE d.invoice_id IS NULL AND d.billing IN ('hourly','fixed')")
    args: list = []
    if period:
        sql += " AND d.period=?"
        args.append(period)
    out = rows(c.execute(sql + " ORDER BY d.client_id, d.date", args))
    # Mark deliveries whose client already paid without an invoice (e.g. card payment through Stripe).
    for d in out:
        d["uninvoiced_deposit_hint"] = None
    return out


def _next_number(c) -> str:
    r = c.execute("SELECT number FROM invoices ORDER BY CAST(SUBSTR(number,5) AS INTEGER) DESC LIMIT 1").fetchone()
    n = int(r["number"][4:]) + 1 if r else 1001
    return f"INV-{n}"


def create_invoice(*, client_id: str, delivery_ids: list[int], issue_date: str, linked_txn_ids: list[int] | None = None,
                   notes: str = "", actor: str = "agent") -> dict:
    with tx() as c:
        cl = c.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if cl is None:
            raise LedgerError(f"unknown client {client_id!r}")
        if not delivery_ids:
            raise LedgerError("an invoice needs at least one delivery_id")
        dels = rows(c.execute(f"SELECT * FROM deliveries WHERE id IN ({','.join('?' * len(delivery_ids))})", delivery_ids))
        if len(dels) != len(set(delivery_ids)):
            raise LedgerError("some delivery_ids do not exist")
        for d in dels:
            if d["client_id"] != client_id:
                raise LedgerError(f"delivery {d['id']} belongs to {d['client_id']}, not {client_id}")
            if d["invoice_id"]:
                raise LedgerError(f"delivery {d['id']} is already on invoice #{d['invoice_id']}")
            if d["billing"] not in ("hourly", "fixed"):
                raise LedgerError(f"delivery {d['id']} is {d['billing']} and not billable")
        total = sum(d["amount_minor"] for d in dels)
        number = _next_number(c)
        due = (date.fromisoformat(issue_date) + timedelta(days=cl["terms_days"] or 0)).isoformat()
        cur = c.execute("INSERT INTO invoices (number, client_id, issue_date, due_date, total_minor, status, notes, created_by, created_at)"
                        " VALUES (?,?,?,?,?, 'draft', ?,?,?)", (number, client_id, issue_date, due, total, notes, actor, now()))
        inv_id = cur.lastrowid
        for d in dels:
            if d["billing"] == "hourly":
                desc, qty, unit = f"{d['project']}: {d['description']}", d["hours"], d["rate_minor"]
            else:
                desc, qty, unit = f"{d['project']}: {d['description']}", 1, d["fixed_fee_minor"]
            c.execute("INSERT INTO invoice_lines (invoice_id, description, qty, unit_price_minor, amount_minor, delivery_id)"
                      " VALUES (?,?,?,?,?,?)", (inv_id, desc, qty, unit, d["amount_minor"], d["id"]))
            c.execute("UPDATE deliveries SET invoice_id=? WHERE id=?", (inv_id, d["id"]))
        for tid in linked_txn_ids or []:
            if c.execute("SELECT 1 FROM transactions WHERE id=?", (tid,)).fetchone():
                c.execute("INSERT OR IGNORE INTO invoice_links (invoice_id, txn_id, kind) VALUES (?,?, 'evidence')", (inv_id, tid))
        # Accrual: revenue is recognized when the work was delivered and billed.
        eid = journal.post(c, date=issue_date, memo=f"{number} {cl['name']}",
                           lines=[("1200", total, 0), (cl["revenue_account"], 0, total)],
                           kind="invoice", actor=actor, invoice_id=inv_id)
        c.execute("UPDATE invoices SET entry_id=? WHERE id=?", (eid, inv_id))
        audit(c, actor, "create_invoice", {"invoice": number, "client": client_id, "total_minor": total})
    return {"invoice_id": inv_id, "number": number, "client": cl["name"], "email": cl["email"], "issue_date": issue_date,
            "due_date": due, "total_minor": total, "status": "draft", "journal_entry_id": eid,
            "lines": [{"delivery_id": d["id"], "description": d["description"], "amount_minor": d["amount_minor"]} for d in dels]}


def match_payment(*, txn_id: int, invoice_id: int, allow_processor_fee: bool = False, actor: str = "agent") -> dict:
    with tx() as c:
        t = c.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
        inv = c.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if t is None or inv is None:
            raise LedgerError("unknown transaction or invoice")
        if t["amount_minor"] <= 0 or t["funding_account"] != "1000":
            raise LedgerError("only deposits into checking can pay an invoice")
        if t["invoice_id"] or t["status"] == "posted":
            raise LedgerError(f"transaction {txn_id} is already matched/posted")
        bal = invoice_balance(c, invoice_id)
        amt = t["amount_minor"]
        fee = 0
        if amt > bal:
            raise LedgerError(f"deposit {amt} exceeds open balance {bal} of {inv['number']}; this is not (only) this invoice")
        if amt < bal and allow_processor_fee:
            short = bal - amt
            if short <= max(int(bal * 0.04) + 30, 100):  # card-processor fee band (~2.9% + 30c)
                fee = short
            else:
                raise LedgerError(f"shortfall {short} is too large to be a processor fee; record as a partial payment instead")
        lines = [("1000", amt, 0)] + ([("6700", fee, 0)] if fee else []) + [("1200", 0, amt + fee)]
        eid = journal.post(c, date=t["date"], memo=f"Payment for {inv['number']}" + (" (net of processor fee)" if fee else ""),
                           lines=lines, kind="payment", actor=actor, txn_id=txn_id, invoice_id=invoice_id)
        c.execute("INSERT INTO payments (txn_id, invoice_id, amount_minor, fee_minor, entry_id, created_at) VALUES (?,?,?,?,?,?)",
                  (txn_id, invoice_id, amt, fee, eid, now()))
        c.execute("UPDATE transactions SET status='posted', category='1200', invoice_id=?, cat_method='payment_match',"
                  " confidence=1.0, cat_reason=? WHERE id=?",
                  (invoice_id, f"Payment matched to {inv['number']}", txn_id))
        c.execute("INSERT OR IGNORE INTO invoice_links (invoice_id, txn_id, kind) VALUES (?,?, 'payment')", (invoice_id, txn_id))
        new_bal = invoice_balance(c, invoice_id)
        status = "paid" if new_bal == 0 else "partially_paid"
        # status is invoice workflow state, not a journal record; invoices table is mutable by design
        c.execute("UPDATE invoices SET status=? WHERE id=?", (status, invoice_id))
        audit(c, actor, "match_payment", {"txn": txn_id, "invoice": inv["number"], "amount_minor": amt, "fee_minor": fee})
    return {"invoice": inv["number"], "applied_minor": amt, "processor_fee_minor": fee, "remaining_balance_minor": new_bal,
            "invoice_status": status, "journal_entry_id": eid}


def ar_aging(c, as_of: str) -> dict:
    buckets = {"current": 0, "1-30": 0, "31-60": 0, "61-90": 0, "90+": 0}
    items = open_invoices(c, as_of)
    for i in items:
        d = i["days_overdue"]
        k = "current" if d <= 0 else "1-30" if d <= 30 else "31-60" if d <= 60 else "61-90" if d <= 90 else "90+"
        i["bucket"] = k
        buckets[k] += i["balance_minor"]
    return {"as_of": as_of, "buckets_minor": buckets, "total_minor": sum(buckets.values()), "invoices": items}


def resolve_review(txn_id: int, account_code: str, *, actor: str = "user") -> dict:
    """Human decision on a review item: reverse the Suspense posting and repost (immutable ledger)."""
    from .categorize import learnable_pattern
    if not coa.is_valid(account_code) or account_code in ("9999", "1200") or account_code not in coa.CATEGORIZABLE:
        raise LedgerError("pick a valid account")
    with tx() as c:
        t = c.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
        if t is None or t["category"] != "9999":
            raise LedgerError("not a review item")
        reason = f"Reviewed by {actor}: {coa.NAMES[account_code]}"
        if t["status"] == "posted":
            old = journal.active_txn_entry(c, txn_id)
            if old:
                journal.reverse(c, old, date=t["date"], reason="reclassified after review", actor=actor)
            journal.post(c, date=t["date"], memo=f"Reclass: {t['description'][:60]}",
                         lines=journal.lines_for_txn(t["funding_account"], account_code, t["amount_minor"]),
                         kind="reclass", actor=actor, txn_id=txn_id)
            c.execute("UPDATE transactions SET category=?, confidence=1.0, cat_method='human', cat_reason=? WHERE id=?",
                      (account_code, reason, txn_id))
        else:
            c.execute("UPDATE transactions SET status='categorized', category=?, confidence=1.0, cat_method='human', cat_reason=?"
                      " WHERE id=?", (account_code, reason, txn_id))
        pattern = learnable_pattern(t["description"])
        c.execute("INSERT INTO rules (pattern, account, origin, note, created_at) VALUES (?,?, 'learned', ?, ?)",
                  (pattern, account_code, f"human review of txn #{txn_id}", now()))
        audit(c, actor, "resolve_review", {"txn": txn_id, "account": account_code, "rule": pattern})
    return {"txn_id": txn_id, "account_code": account_code, "learned_rule": {"match": pattern, "account_code": account_code}}


def invoice_detail(inv_id: int) -> dict | None:
    inv = q1("SELECT i.*, cl.name AS client_name, cl.email FROM invoices i JOIN clients cl ON cl.id=i.client_id WHERE i.id=?", inv_id)
    if inv is None:
        return None
    d = dict(inv)
    d["lines"] = rows(q("SELECT * FROM invoice_lines WHERE invoice_id=?", inv_id))
    d["links"] = rows(q("SELECT l.kind, t.id AS txn_id, t.date, t.description, t.amount_minor FROM invoice_links l"
                        " JOIN transactions t ON t.id=l.txn_id WHERE l.invoice_id=?", inv_id))
    with tx() as c:
        d["balance_minor"] = invoice_balance(c, inv_id)
    return d


def tokens_overlap(a: str, b: str) -> int:
    return len(set(norm_tokens(a)) & set(norm_tokens(b)))
