"""The month-end deliverable, per the bookkeeping guidelines.

Sections: Ledger, Expenses, Reconciliation, Income, Contractors, Open items. All figures are integer minor units
computed here from the ledger; the agent only narrates them.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from . import categories as cats
from . import config, journal
from .db import audit, now, rows, tx
from .journal import LedgerError
from .reports import bounds, prev_period

# 1099-NEC reporting threshold for payments made in 2026 (raised from $600 to $2,000 by the 2025 tax law).
# Confirm with your accountant; configurable here.
CONTRACTOR_1099_THRESHOLD_MINOR = 200000

LIVE = "status NOT IN ('duplicate','linked')"


def _txns(c, period: str) -> list[dict]:
    return rows(c.execute(f"SELECT * FROM transactions WHERE period=? AND {LIVE} ORDER BY date, id", (period,)))


def _resolutions(c) -> dict[str, dict]:
    return {r["key"]: r for r in rows(c.execute("SELECT * FROM open_item_resolutions"))}


def income_document(c, t: dict) -> str | None:
    if t.get("invoice_id"):
        r = c.execute("SELECT number FROM invoices WHERE id=?", (t["invoice_id"],)).fetchone()
        return f"Invoice {r['number']}" if r else None
    d = t["description"].upper()
    if "INTEREST" in d:
        return "Bank statement (interest)"
    if "CASHBACK" in d or "REWARD" in d:
        return "Card statement (cashback)"
    return t.get("document")


# ------------------------------------------------------------------------------------------- sections
def ledger(c, period: str) -> dict:
    items = []
    for t in _txns(c, period):
        items.append({"txn_id": t["id"], "date": t["date"], "description": t["description"],
                      "account": cats.ACCOUNT_LABEL.get(t["funding_account"], t["funding_account"]),
                      "category": cats.label(t["category"]) if t["status"] != "new" else cats.OPEN_ITEM,
                      "type": cats.txn_type(t["amount_minor"]), "amount_minor": abs(t["amount_minor"]),
                      "receipt": {"ok": "OK", "missing": "Missing", "waived": "Not needed"}.get(t["receipt_status"], "–")})
    inc = sum(i["amount_minor"] for i in items if i["type"] == "Income")
    exp = sum(i["amount_minor"] for i in items if i["type"] == "Expense")
    return {"report": "ledger", "period": period, "entries": items, "count": len(items),
            "total_income_minor": inc, "total_expenses_minor": exp}


def expenses(c, period: str) -> dict:
    def by_cat(p):
        out = {k: 0 for k in cats.EXPENSE_CATEGORIES + [cats.OPEN_ITEM]}
        for t in _txns(c, p):
            if t["amount_minor"] < 0:
                lab = cats.label(t["category"]) if t["status"] != "new" else cats.OPEN_ITEM
                out[lab if lab in out else cats.OPEN_ITEM] += -t["amount_minor"]
        return out
    cur, prev = by_cat(period), by_cat(prev_period(period))
    lines = [{"category": k, "amount_minor": v, "prior_minor": prev.get(k, 0)} for k, v in cur.items()]
    return {"report": "expenses", "period": period, "prior_period": prev_period(period), "categories": lines,
            "total_minor": sum(cur.values()), "prior_total_minor": sum(prev.values())}


def reconciliation(c, period: str) -> dict:
    """Theoretical money = money we already have + income - spend, compared with the bank statement balance."""
    start, end = bounds(period)
    have = journal.balances(c, end=(date.fromisoformat(start) - timedelta(days=1)).isoformat()).get("1000", 0)
    chk = [t for t in _txns(c, period) if t["funding_account"] == "1000"]
    income = sum(t["amount_minor"] for t in chk if t["amount_minor"] > 0)
    spend = sum(-t["amount_minor"] for t in chk if t["amount_minor"] < 0)
    theoretical = have + income - spend
    st = c.execute("SELECT * FROM statements WHERE account='1000' AND period=? ORDER BY rowid DESC LIMIT 1", (period,)).fetchone()
    stmt_close = st["closing_minor"] if st else None
    stmt_open = st["opening_minor"] if st else None
    diff = None if stmt_close is None else stmt_close - theoretical
    card = [t for t in _txns(c, period) if t["funding_account"] == "2000"]
    failed = [r["name"] for r in c.execute("SELECT name FROM files WHERE status='ingested_with_errors'")]
    res = _resolutions(c).get(f"recon:{period}")
    status = ("not_started" if not _txns(c, period) else "no_statement" if stmt_close is None else "matched" if diff == 0 else
              "difference_acknowledged" if res else "difference")
    return {
        "report": "reconciliation", "period": period, "account": "Checking (Harbor State Bank)",
        "money_we_had_minor": have, "income_minor": income, "spend_minor": spend, "theoretical_minor": theoretical,
        "statement_balance_minor": stmt_close, "statement_opening_minor": stmt_open,
        "opening_agrees": stmt_open is None or stmt_open == have, "difference_minor": diff, "status": status,
        "matched": diff == 0, "files_with_unimported_rows": failed,
        "acknowledgement": res["note"] if res else None,
        "credit_card": {"charges_minor": sum(-t["amount_minor"] for t in card if t["amount_minor"] < 0),
                        "credits_minor": sum(t["amount_minor"] for t in card if t["amount_minor"] > 0), "count": len(card)},
        "formula": "money we already have + income - spend = theoretical money; compare with the bank statement balance",
    }


def income(c, period: str) -> dict:
    items = []
    for t in _txns(c, period):
        if t["amount_minor"] <= 0:
            continue
        src = t["counterparty"]
        if t.get("invoice_id"):
            r = c.execute("SELECT cl.name FROM invoices i JOIN clients cl ON cl.id=i.client_id WHERE i.id=?", (t["invoice_id"],)).fetchone()
            src = r["name"] if r else src
        items.append({"txn_id": t["id"], "from": src, "date": t["date"], "amount_minor": t["amount_minor"],
                      "account": cats.ACCOUNT_LABEL.get(t["funding_account"]), "document": income_document(c, t) or "Missing"})
    return {"report": "income", "period": period, "items": items, "total_minor": sum(i["amount_minor"] for i in items)}


def contractors(c, period: str) -> dict:
    start, end = bounds(period)
    year_start = f"{period[:4]}-01-01"
    reg = rows(c.execute("SELECT * FROM contractors"))
    paid = rows(c.execute(f"SELECT * FROM transactions WHERE category='5000' AND {LIVE} AND date BETWEEN ? AND ?", (year_start, end)))
    out: dict[str, dict] = {}
    for t in paid:
        d = t["description"].upper()
        k = next((r for r in reg if any(m.upper() in d for m in json.loads(r["match"]))), None)
        name = k["name"] if k else t["counterparty"]
        row = out.setdefault(name, {"contractor": name, "registered": bool(k), "total_minor": k["ytd_before_minor"] if k else 0,
                                    "this_month_minor": 0, "form": (k["form"] if k else "") or "",
                                    "needs_1099": bool(k["needs_1099"]) if k else True})
        row["total_minor"] += -t["amount_minor"]
        if t["period"] == period:
            row["this_month_minor"] += -t["amount_minor"]
    items = sorted(out.values(), key=lambda r: -r["this_month_minor"])
    for r in items:
        if not r["form"]:
            r["form"] = "W-9 missing"
        r["form_ok"] = r["form"] != "W-9 missing"
        r["1099_required"] = r["needs_1099"] and r["total_minor"] >= CONTRACTOR_1099_THRESHOLD_MINOR
    return {"report": "contractors", "period": period, "contractors": [r for r in items if r["this_month_minor"] or r["total_minor"]],
            "total_this_month_minor": sum(r["this_month_minor"] for r in items), "threshold_1099_minor": CONTRACTOR_1099_THRESHOLD_MINOR}


def open_items(c, period: str) -> dict:
    res = _resolutions(c)
    items = []
    for t in _txns(c, period):
        amt = abs(t["amount_minor"])
        if t["category"] == "9999" or t["status"] == "new":
            why = (t["cat_reason"] or "").replace("Open item: ", "").strip()
            why = (why[:1].upper() + why[1:]) if why else ""
            items.append({"key": f"cat:{t['id']}", "kind": "category", "txn_id": t["id"], "item": t["description"],
                          "amount_minor": amt, "date": t["date"], "reason": f"Category unknown. {why}".strip(), "status": "Open"})
        elif t["cat_method"] == "human":
            items.append({"key": f"cat:{t['id']}", "kind": "category", "txn_id": t["id"], "item": t["description"],
                          "amount_minor": amt, "date": t["date"], "reason": "Category was unknown",
                          "status": f"Resolved: {cats.label(t['category'])}"})
        if t["amount_minor"] < 0 and t["receipt_status"] in ("missing", "waived") or f"rcpt:{t['id']}" in res:
            r = res.get(f"rcpt:{t['id']}")
            status = "Open" if t["receipt_status"] == "missing" else f"Resolved: {r['resolution'] if r else t['receipt_status']}"
            items.append({"key": f"rcpt:{t['id']}", "kind": "receipt", "txn_id": t["id"], "item": t["description"],
                          "amount_minor": amt, "date": t["date"], "reason": "Receipt missing", "status": status})
        if t["amount_minor"] > 0 and not income_document(c, t):
            r = res.get(f"doc:{t['id']}")
            items.append({"key": f"doc:{t['id']}", "kind": "document", "txn_id": t["id"], "item": t["description"],
                          "amount_minor": amt, "date": t["date"], "reason": "Income without an invoice or supporting document",
                          "status": f"Resolved: {r['note'] or r['resolution']}" if r else "Open"})
    rec = reconciliation(c, period)
    if rec["status"] in ("difference", "difference_acknowledged", "no_statement"):
        r = res.get(f"recon:{period}")
        items.append({"key": f"recon:{period}", "kind": "reconciliation", "item": "Bank reconciliation (checking)",
                      "amount_minor": abs(rec["difference_minor"] or 0), "date": bounds(period)[1],
                      "reason": "No bank statement balance found in the transactions file" if rec["status"] == "no_statement" else
                      f"Theoretical money differs from the bank statement by {_fmt(rec['difference_minor'])}",
                      "status": f"Resolved: {r['note']}" if r else "Open"})
    for k in contractors(c, period)["contractors"]:
        if k["this_month_minor"] and not k["form_ok"]:
            r = res.get(f"w9:{k['contractor']}")
            items.append({"key": f"w9:{k['contractor']}", "kind": "contractor_form", "item": f"Contractor {k['contractor']}",
                          "amount_minor": k["this_month_minor"], "date": bounds(period)[1],
                          "reason": "No W-9 on file for a contractor paid this month", "status": f"Resolved: {r['note']}" if r else "Open"})
    open_n = sum(1 for i in items if i["status"] == "Open")
    return {"report": "open_items", "period": period, "items": items, "open_count": open_n,
            "resolved_count": len(items) - open_n}


def _fmt(m):
    from .money import fmt
    return fmt(m or 0)


# ------------------------------------------------------------------------------------------- human review
def resolve(key: str, *, resolution: str, note: str = "", actor: str = "user") -> dict:
    """Record a human decision on a non-category open item (receipt, document, reconciliation, contractor form)."""
    kind, _, ref = key.partition(":")
    with tx() as c:
        if kind == "rcpt":
            if resolution not in ("attached", "not needed"):
                raise LedgerError("receipt resolution must be 'attached' or 'not needed'")
            c.execute("UPDATE transactions SET receipt_status=? WHERE id=?", ("ok" if resolution == "attached" else "waived", int(ref)))
        elif kind == "doc":
            c.execute("UPDATE transactions SET document=? WHERE id=?", (note or "Confirmed by reviewer", int(ref)))
        elif kind == "w9":
            if resolution == "received":
                c.execute("UPDATE contractors SET form='W-9 on file' WHERE name=?", (ref,))
                if c.execute("SELECT changes() AS n").fetchone()["n"] == 0:
                    c.execute("INSERT INTO contractors (name, match, form) VALUES (?,?, 'W-9 on file')", (ref, json.dumps([ref.upper()])))
        elif kind != "recon":
            raise LedgerError("unknown open item")
        c.execute("INSERT OR REPLACE INTO open_item_resolutions (key, resolution, note, resolved_by, resolved_at) VALUES (?,?,?,?,?)",
                  (key, resolution, note or resolution, actor, now()))
        audit(c, actor, "resolve_open_item", {"key": key, "resolution": resolution, "note": note})
    return {"key": key, "resolution": resolution}


# ------------------------------------------------------------------------------------------- reconciliation investigation
def _statement_lines(name: str) -> list[dict]:
    """Checking rows of a transactions CSV, in file order, with their printed amount and running balance."""
    import csv
    import io
    from .money import parse_amount
    path = config.INBOX / name
    if not path.exists():
        return []
    reader = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    header = [h.strip().lower() for h in reader[0]]
    col = {k: next((i for i, h in enumerate(header) if h.startswith(k)), None) for k in ("date", "description", "account", "amount", "balance")}
    out = []
    for n, r in enumerate(reader[1:], start=2):
        def cell(k):
            return r[col[k]].strip() if col[k] is not None and col[k] < len(r) else ""
        if cell("account").lower() != "checking" or not cell("balance"):
            continue
        try:
            out.append({"row": n, "date": cell("date"), "description": " ".join(cell("description").split()).upper(),
                        "amount_minor": parse_amount(cell("amount")), "balance_minor": parse_amount(cell("balance")),
                        "raw": ",".join(r)})
        except ValueError:
            continue
    return out


def investigate(c, period: str) -> dict:
    """Explain a reconciliation difference from evidence: the statement's running balance, rows that were not
    imported, and duplicates that were removed. Deterministic; the agent decides what to do with the findings."""
    rec = reconciliation(c, period)
    findings: list[dict] = []
    for f in c.execute("SELECT name FROM files WHERE kind='transactions_csv'").fetchall():
        lines = _statement_lines(f["name"])
        prev = None
        for ln in lines:
            ref = f"{f['name']}:{ln['row']}"
            t = c.execute("SELECT id, status FROM transactions WHERE source_ref=?", (ref,)).fetchone()
            is_dup = t is not None and t["status"] == "duplicate"
            if prev is not None and not is_dup:
                expected = prev["balance_minor"] + ln["amount_minor"]
                gap = ln["balance_minor"] - expected
                if gap:
                    findings.append({
                        "type": "statement_gap", "id": f"gap:{f['name']}:{ln['row']}", "amount_minor": gap,
                        "between": [prev["date"], ln["date"]], "after_row": prev["row"], "before_row": ln["row"],
                        "explanation": (f"The bank balance moves by {_fmt(gap)} more than the listed transactions between "
                                        f"{prev['description'][:30]} ({prev['date']}) and {ln['description'][:30]} ({ln['date']}). "
                                        "A transaction is missing from the export."),
                        "suggested_action": "record_statement_gap (adds it as an open item so the books match the bank), "
                                            "then a human identifies it."})
            if t is None:
                findings.append({"type": "not_imported", "id": ref, "amount_minor": ln["amount_minor"], "row": ln["row"],
                                 "raw": ln["raw"], "explanation": f"Row {ln['row']} is in the file but was not imported "
                                 "(it failed to parse).", "suggested_action": "ingest_file with fixes for this row"})
            if not is_dup:
                prev = ln
    for d in rows(c.execute("SELECT id, description, amount_minor FROM transactions WHERE period=? AND status='duplicate'"
                            " AND funding_account='1000'", (period,))):
        if rec["difference_minor"] and d["amount_minor"] == rec["difference_minor"]:
            findings.append({"type": "removed_duplicate", "id": f"dup:{d['id']}", "amount_minor": d["amount_minor"],
                             "explanation": f"A removed duplicate ({d['description']}) equals the difference; it may have been real.",
                             "suggested_action": "ask a human; do not re-add it without evidence"})
    explained = sum(x["amount_minor"] for x in findings if x["type"] in ("statement_gap", "not_imported"))
    return {"report": "reconciliation_investigation", "period": period, "status": rec["status"],
            "difference_minor": rec["difference_minor"], "findings": findings, "explained_minor": explained,
            "fully_explained": rec["difference_minor"] is not None and explained == rec["difference_minor"]}


def record_statement_gap(gap_id: str, *, actor: str = "agent") -> dict:
    """Book a transaction that the bank balance proves happened but that is missing from the export.
    It is recorded as an open item (unknown category, receipt missing) for a human to identify."""
    from .ingest import _insert_txn
    with tx() as c:
        inv = investigate(c, bounds_period(gap_id, c))
        g = next((x for x in inv["findings"] if x["id"] == gap_id and x["type"] == "statement_gap"), None)
        if g is None:
            raise LedgerError("no such statement gap; run the investigation again")
        ref = f"{gap_id}:recorded"
        if c.execute("SELECT 1 FROM transactions WHERE source_ref=?", (ref,)).fetchone():
            raise LedgerError("this gap was already recorded")
        from .ingest import parse_date
        d = parse_date(g["between"][1])
        kind = "DEBIT" if g["amount_minor"] < 0 else "CREDIT"
        tid = _insert_txn(c, d=d, a=g["amount_minor"], desc=f"UNLISTED BANK {kind} (FOUND FROM STATEMENT BALANCE)",
                          source="bank_csv", ref=ref, funding="1000", status="review",
                          receipt_status="missing" if g["amount_minor"] < 0 else "n/a")
        c.execute("UPDATE transactions SET category='9999', cat_method='reconciliation', confidence=0, cat_reason=? WHERE id=?",
                  (f"Open item: the bank balance shows {_fmt(g['amount_minor'])} moved between {g['between'][0]} and "
                   f"{g['between'][1]} with no line in the export. Ask the bank or check the cheque book.", tid))
        audit(c, actor, "record_statement_gap", {"gap": gap_id, "txn": tid, "amount_minor": g["amount_minor"]})
    return {"txn_id": tid, "amount_minor": g["amount_minor"], "status": "recorded as open item",
            "next_step": "post_entry post_ready=true, then run_report reconciliation again"}


def bounds_period(gap_id: str, c) -> str:
    r = c.execute("SELECT period FROM statements WHERE file=?", (gap_id.split(":")[1],)).fetchone()
    return r["period"] if r else ""


SECTIONS = {"ledger": ledger, "expenses": expenses, "reconciliation": reconciliation, "income": income,
            "contractors": contractors, "open_items": open_items}


def package(c, period: str) -> dict:
    return {k: f(c, period) for k, f in SECTIONS.items()}
