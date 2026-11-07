"""Reset local bookkeeping activity while retaining demo client reference data.

    python -m backend.reset              # wipe ledger, inbox, generated PDFs; keep agent + memory + run history
    python -m backend.reset --regenerate # also regenerate the synthetic dataset first

Agent memory lives on ZooWork and is intentionally NOT wiped: re-running October after a reset is
how the demo shows that learned rules from memory reduce the review list.
"""
from __future__ import annotations

import json
import shutil
import sys

from . import config, db, journal
from .ingest import _insert_txn
from .money import parse_amount


def _insert_reference_clients(c) -> None:
    hist = json.loads((config.SEED_DIR / "history.json").read_text())
    for cl in hist["clients"]:
        c.execute(
            "INSERT OR IGNORE INTO clients (id, name, email, aliases, terms_days, revenue_account) VALUES (?,?,?,?,?,?)",
            (cl["id"], cl["name"], cl["email"], json.dumps(cl["aliases"]), cl["terms_days"], cl["revenue_account"]),
        )


def ensure_reference_clients() -> int:
    """Keep bundled demo clients available so delivery logs can be matched after a clean reset."""
    before = db.q1("SELECT COUNT(*) AS n FROM clients")["n"]
    with db.tx() as c:
        _insert_reference_clients(c)
    after = db.q1("SELECT COUNT(*) AS n FROM clients")["n"]
    return after - before


def reset_empty() -> dict:
    db.close()
    for p in (config.DB_PATH, config.DB_PATH.with_suffix(".sqlite-wal"), config.DB_PATH.with_suffix(".sqlite-shm")):
        p.unlink(missing_ok=True)
    for d in (config.INBOX, config.OUT):
        shutil.rmtree(d, ignore_errors=True)
    config.ensure_dirs()
    with db.tx() as c:
        _insert_reference_clients(c)
        db.audit(c, "system", "reset_empty", {"mode": "clean_slate"})
        ok = journal.trial_balance_ok(c)
    n = db.q1("SELECT COUNT(*) AS n FROM transactions")["n"]
    clients = db.q1("SELECT COUNT(*) AS n FROM clients")["n"]
    return {"transactions": n, "clients": clients, "trial_balance_ok": ok, "mode": "clean_slate"}


def reset(regenerate: bool = False) -> dict:
    if regenerate:
        from seed import generate
        generate.main()
    db.close()
    for p in (config.DB_PATH, config.DB_PATH.with_suffix(".sqlite-wal"), config.DB_PATH.with_suffix(".sqlite-shm")):
        p.unlink(missing_ok=True)
    for d in (config.INBOX, config.OUT):
        shutil.rmtree(d, ignore_errors=True)
    config.ensure_dirs()

    hist = json.loads((config.SEED_DIR / "history.json").read_text())
    with db.tx() as c:
        _insert_reference_clients(c)
        for r in hist["rules"]:
            c.execute("INSERT INTO rules (pattern, account, origin, note, created_at) VALUES (?,?, 'builtin', ?, ?)",
                      (r["pattern"], r["account"], r["note"], db.now()))
        op = hist["opening"]
        journal.post(c, date=op["date"], memo="Opening balances (Aug 31, 2026)", kind="opening", actor="seed",
                     lines=[(a, parse_amount(d) if d else 0, parse_amount(k) if k else 0) for a, d, k in op["lines"]])
        inv_ids = {}
        for inv in hist["invoices"]:
            total = sum(parse_amount(p) * q for _, q, p in inv["lines"])
            cur = c.execute("INSERT INTO invoices (number, client_id, issue_date, due_date, total_minor, status, created_by, created_at, sent_at)"
                            " VALUES (?,?,?,?,?, 'sent', 'seed', ?, ?)",
                            (inv["number"], inv["client"], inv["issue"], inv["due"], total, db.now(), inv["issue"]))
            iid = cur.lastrowid
            inv_ids[inv["number"]] = iid
            for desc, q, p in inv["lines"]:
                c.execute("INSERT INTO invoice_lines (invoice_id, description, qty, unit_price_minor, amount_minor) VALUES (?,?,?,?,?)",
                          (iid, desc, q, parse_amount(p), parse_amount(p) * q))
            if not inv.get("opening"):
                rev = c.execute("SELECT revenue_account FROM clients WHERE id=?", (inv["client"],)).fetchone()[0]
                eid = journal.post(c, date=inv["issue"], memo=f"{inv['number']}", kind="invoice", actor="seed", invoice_id=iid,
                                   lines=[("1200", total, 0), (rev, 0, total)])
                c.execute("UPDATE invoices SET entry_id=? WHERE id=?", (eid, iid))
        for k in hist.get("contractors", []):
            c.execute("INSERT INTO contractors (name, match, form, needs_1099, ytd_before_minor) VALUES (?,?,?,?,?)",
                      (k["name"], json.dumps(k["match"]), k["form"], int(k["needs_1099"]), k["ytd_before_sep_minor"]))
        cat_acct = hist.get("category_accounts", {})
        for t in hist["transactions"]:
            # history rows use the statement sign convention (checking +in/-out, card +charge/-credit)
            funding = "1000" if t["account"] == "checking" else "2000"
            a = parse_amount(t["amount"]) * (1 if funding == "1000" else -1)
            inv_no = next((n for n in inv_ids if n in t["desc"]), None)
            tid = _insert_txn(c, d=t["date"], a=a, desc=t["desc"], source="bank_csv" if funding == "1000" else "card_csv",
                              ref=f"history:{t['date']}:{t['desc']}:{a}", funding=funding, status="posted",
                              receipt_status=t["receipt"] or "n/a")
            t["acct"] = cat_acct.get(t["category"], "9999")
            if inv_no:
                t["invoice"] = inv_no
                iid = inv_ids[t["invoice"]]
                eid = journal.post(c, date=t["date"], memo=f"Payment for {t['invoice']}", kind="payment", actor="seed",
                                   txn_id=tid, invoice_id=iid, lines=[("1000", a, 0), ("1200", 0, a)])
                c.execute("INSERT INTO payments (txn_id, invoice_id, amount_minor, entry_id, created_at) VALUES (?,?,?,?,?)",
                          (tid, iid, a, eid, db.now()))
                c.execute("UPDATE invoices SET status='paid' WHERE id=?", (iid,))
                c.execute("UPDATE transactions SET category='1200', invoice_id=?, cat_method='history', confidence=1 WHERE id=?", (iid, tid))
            else:
                journal.post(c, date=t["date"], memo=t["desc"][:60], kind="txn", actor="seed", txn_id=tid,
                             lines=journal.lines_for_txn(funding, t["acct"], a))
                c.execute("UPDATE transactions SET category=?, cat_method='history', confidence=1, cat_reason='Approved in September close'"
                          " WHERE id=?", (t["acct"], tid))
        db.audit(c, "system", "reset", {"history_txns": len(hist["transactions"])})
        ok = journal.trial_balance_ok(c)
    n = db.q1("SELECT COUNT(*) AS n FROM transactions")["n"]
    return {"transactions": n, "trial_balance_ok": ok}


if __name__ == "__main__":
    print(reset(regenerate="--regenerate" in sys.argv))
