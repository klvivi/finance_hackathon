"""Custom tool declarations (what the model sees) and their execution (deterministic backend code)."""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from . import books, categorize, coa, config, invoicing, journal, pdfs, reports
from .db import audit, now, q, q1, rows, tx
from .ingest import ingest_file
from .journal import LedgerError

PERIOD = {"type": "string", "pattern": "^\\d{4}-\\d{2}$", "description": "Accounting period YYYY-MM, e.g. 2026-10"}

DECLARATIONS: list[dict] = [
    {
        "name": "ingest_file",
        "description": (
            "Import one uploaded file from the inbox into the ledger's normalized Transaction shape (integer minor units, "
            "currency, counterparty, description, source). Handles bank CSV, card statement PDF, delivery/time log CSV, and "
            "receipt images. Dedupes rows already in the books, removes exported-twice rows, links the card-payment transfer "
            "so it posts once. Returns parse errors for rows it could not read: call again with `fixes` for those rows. "
            "For a receipt image, first read it with the `image` tool at /workspace/inbox/<file>, then pass `receipt`. "
            "Safe to call repeatedly (idempotent)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "File name in the inbox, e.g. harbor_checking_2026-10.csv"},
                "fixes": {"type": "array", "description": "Corrections for rows reported in `errors`", "items": {
                    "type": "object", "properties": {"row": {"type": ["integer", "string"]}, "date": {"type": "string"},
                                                     "amount": {"type": "string"}, "description": {"type": "string"}},
                    "required": ["row"]}},
                "receipt": {"type": "object", "description": "Fields read from a receipt image", "properties": {
                    "merchant": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "total": {"type": "string", "description": "Total exactly as printed, e.g. 86.40"},
                    "currency": {"type": "string"}, "payment_method": {"type": "string", "enum": ["card", "debit", "cash", "personal"]},
                    "note": {"type": "string", "description": "Business purpose printed or handwritten on the receipt"}}},
            },
            "required": ["file"],
        },
    },
    {
        "name": "categorize",
        "description": (
            "Categorize transactions into the ONLY allowed categories: Income, Rent, Contractors, Software, Bank fees. "
            "Income is money in (checking +, credit card credit); the other four are money out. "
            "mode='auto': runs deterministic rules (built-in + `learned_rules` you recalled from memory) and similarity to "
            "approved history, and returns `needs_judgment` items it could not decide. "
            "mode='decide': submit decisions=[{txn_id, category, confidence, reason}]. If no category fits or you do not "
            "know, use category 'Open item' so a human decides in review; never force a guess. "
            f"Confidence below {config.REVIEW_THRESHOLD} also becomes an open item. Client deposits that pay an open invoice "
            "are recorded with match_payment instead."),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["auto", "decide"]},
                "period": PERIOD,
                "learned_rules": {"type": "array", "description": "Rules recalled from memory: {match: descriptor substring, account_code (4000 Income, 6100 Rent, 5000 Contractors, 6200 Software, 6700 Bank fees), note}",
                                  "items": {"type": "object", "properties": {"match": {"type": "string"}, "account_code": {"type": "string"},
                                                                              "note": {"type": "string"}}, "required": ["match", "account_code"]}},
                "decisions": {"type": "array", "items": {"type": "object", "properties": {
                    "txn_id": {"type": "integer"},
                    "category": {"type": "string", "enum": ["Income", "Rent", "Contractors", "Software", "Bank fees", "Open item"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1}, "reason": {"type": "string"}},
                    "required": ["txn_id", "category", "confidence", "reason"]}},
            },
            "required": ["mode"],
        },
    },
    {
        "name": "post_entry",
        "description": (
            "Post balanced double-entry journal entries. Entries are immutable. "
            "post_ready=true posts every categorized (and review/Suspense) transaction in the period: the backend builds the "
            "debit/credit lines. To fix a posted entry use reverse_entry_id (creates a reversing entry). A manual entry "
            "(e.g. an accrual) is only for amounts taken verbatim from a document; the backend rejects unbalanced entries."),
        "input_schema": {
            "type": "object",
            "properties": {
                "post_ready": {"type": "boolean"}, "period": PERIOD,
                "reverse_entry_id": {"type": "integer"}, "reason": {"type": "string"},
                "manual": {"type": "object", "properties": {
                    "date": {"type": "string"}, "memo": {"type": "string"},
                    "lines": {"type": "array", "items": {"type": "object", "properties": {
                        "account_code": {"type": "string"}, "debit_minor": {"type": "integer"}, "credit_minor": {"type": "integer"}},
                        "required": ["account_code"]}}}, "required": ["date", "memo", "lines"]},
            },
        },
    },
    {
        "name": "create_invoice",
        "description": (
            "Draft an invoice for delivered, unbilled work (from query_ledger kind=unbilled_deliveries). The backend computes "
            "line amounts and the total from the delivery records, assigns the number, posts AR/revenue, and renders a PDF into "
            "/workspace/invoices/. Link evidence transactions (e.g. a deposit that already paid it) with linked_txn_ids. "
            "Creating a draft does NOT send anything."),
        "input_schema": {
            "type": "object",
            "properties": {"client_id": {"type": "string"}, "delivery_ids": {"type": "array", "items": {"type": "integer"}},
                           "issue_date": {"type": "string", "description": "YYYY-MM-DD, normally the last day of the period"},
                           "linked_txn_ids": {"type": "array", "items": {"type": "integer"}}, "notes": {"type": "string"}},
            "required": ["client_id", "delivery_ids", "issue_date"],
        },
    },
    {
        "name": "match_payment",
        "description": (
            "Apply a checking deposit to an open invoice: posts Dr Cash / Cr AR, links the transaction, updates invoice status "
            "(paid or partially_paid). Use query_ledger kind=payment_candidates for scored suggestions. Set "
            "allow_processor_fee=true only when the deposit came through a card processor (e.g. Stripe) and is short by a "
            "processor-sized fee; the backend books the difference to 6700 or refuses."),
        "input_schema": {
            "type": "object",
            "properties": {"txn_id": {"type": "integer"}, "invoice_id": {"type": "integer"}, "allow_processor_fee": {"type": "boolean"}},
            "required": ["txn_id", "invoice_id"],
        },
    },
    {
        "name": "run_report",
        "description": (
            "Generate a report from the ledger (deterministic). Books sections: ledger, expenses, reconciliation (money we "
            "already have + income - spend vs the bank statement balance; flags any difference), income (from, date, amount, "
            "document), contractors (total, this month, tax form), open_items. Also: pnl, balance_sheet, cash_flow, "
            "spend_by_category, ar_aging, anomalies (duplicate charges, unusual amounts, new subscriptions), mom, cash_outlook. "
            "Returns the numbers in minor units (with formatted siblings) and renders a PDF into /workspace/reports/."),
        "input_schema": {
            "type": "object",
            "properties": {"report": {"type": "string", "enum": list(books.SECTIONS) + list(reports.REPORTS)}, "period": PERIOD},
            "required": ["report", "period"],
        },
    },
    {
        "name": "query_ledger",
        "description": (
            "Read the ledger. kinds: close_status (counts by status for the period), transactions (filters: status, "
            "source, search), payment_candidates (deposits with suggested invoices), open_invoices, unbilled_deliveries, "
            "review_list, open_items, clients, chart_of_accounts, account_balance (filters.account), invoice (filters.invoice_id)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["close_status", "transactions", "payment_candidates", "open_invoices",
                                                    "unbilled_deliveries", "review_list", "open_items", "clients", "chart_of_accounts",
                                                    "account_balance", "invoice"]},
                "period": PERIOD,
                "filters": {"type": "object", "properties": {"status": {"type": "string"}, "source": {"type": "string"},
                                                             "search": {"type": "string"}, "account": {"type": "string"},
                                                             "invoice_id": {"type": "integer"}, "limit": {"type": "integer"}}},
            },
            "required": ["kind"],
        },
    },
    {
        "name": "reconcile",
        "description": (
            "Investigate and resolve a bank reconciliation difference using evidence. action='investigate' walks the bank "
            "statement's running balance line by line and returns findings: statement_gap (the balance moved with no "
            "transaction listed: something is missing from the export), not_imported (a row failed to parse), "
            "removed_duplicate. action='record_statement_gap' with gap_id books a gap the bank balance proves, as an OPEN ITEM "
            "for a human to identify (unknown category, receipt missing). Never use it to force a match without a finding."),
        "input_schema": {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["investigate", "record_statement_gap"]},
                           "period": PERIOD, "gap_id": {"type": "string"}},
            "required": ["action"],
        },
    },
    {
        "name": "save_close_summary",
        "description": (
            "Save your short close notes (markdown) and build the books package for human review: Cover, Ledger, Expenses, "
            "Reconciliation, Income, Contractors, Open items, rendered to /workspace/reports/books_<period>.pdf for you to "
            "publish. Write numbers exactly as the tools returned them (formatted dollars); do not compute new figures."),
        "input_schema": {
            "type": "object",
            "properties": {"period": PERIOD, "summary_markdown": {"type": "string"}},
            "required": ["period", "summary_markdown"],
        },
    },
]

# Fallback outbound tool when no public MCP endpoint is configured. Gated by always_ask either way.
SEND_FALLBACK = {
    "name": "send_email",
    "description": ("Send an invoice or payment reminder email to a client. REQUIRES HUMAN APPROVAL. Attach the invoice "
                    "by invoice_id; the backend renders the PDF. kind: invoice | reminder."),
    "input_schema": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["invoice", "reminder"]}, "invoice_id": {"type": "integer"},
        "to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
        "required": ["kind", "invoice_id", "to", "subject", "body"]},
}

Uploader = Callable[[Path, str], Awaitable[str | None]]


def _period(args: dict) -> str:
    return args.get("period") or "2026-10"


def _summ(d: dict, limit: int = 60) -> dict:
    for k, v in list(d.items()):
        if isinstance(v, list) and len(v) > limit:
            d[k] = v[:limit]
            d[f"{k}_truncated"] = f"{len(v) - limit} more not shown"
    return d


async def execute(name: str, args: dict, *, upload: Uploader, actor: str = "agent") -> dict:
    """Run one custom tool. Raises LedgerError for business-rule violations (returned to the model as is_error)."""
    if name == "ingest_file":
        return ingest_file(args["file"], fixes=args.get("fixes"), receipt=args.get("receipt"), actor=actor)

    if name == "categorize":
        if args.get("mode") == "decide":
            return categorize.decide(args.get("decisions") or [], actor=actor)
        return _summ(categorize.auto(learned_rules=args.get("learned_rules"), actor=actor, period=args.get("period")))

    if name == "post_entry":
        if args.get("reverse_entry_id"):
            with tx() as c:
                e = c.execute("SELECT date FROM journal_entries WHERE id=?", (args["reverse_entry_id"],)).fetchone()
                if e is None:
                    raise LedgerError("entry not found")
                rid = journal.reverse(c, args["reverse_entry_id"], date=e["date"], reason=args.get("reason") or "correction", actor=actor)
            return {"reversal_entry_id": rid}
        if args.get("manual"):
            m = args["manual"]
            with tx() as c:
                eid = journal.post(c, date=m["date"], memo=m["memo"], kind="manual", actor=actor,
                                   lines=[(l["account_code"], int(l.get("debit_minor") or 0), int(l.get("credit_minor") or 0)) for l in m["lines"]])
            return {"entry_id": eid}
        if args.get("post_ready"):
            return post_ready(_period(args), actor=actor)
        raise LedgerError("pass post_ready=true, manual, or reverse_entry_id")

    if name == "create_invoice":
        inv = invoicing.create_invoice(client_id=args["client_id"], delivery_ids=args["delivery_ids"], issue_date=args["issue_date"],
                                       linked_txn_ids=args.get("linked_txn_ids"), notes=args.get("notes", ""), actor=actor)
        det = invoicing.invoice_detail(inv["invoice_id"])
        pdf = pdfs.render_invoice(det)
        ws = await upload(pdf, f"invoices/{pdf.name}")
        with tx() as c:
            c.execute("UPDATE invoices SET pdf_path=?, workspace_path=? WHERE id=?", (str(pdf), ws, inv["invoice_id"]))
        inv["pdf_workspace_path"] = ws
        return inv

    if name == "match_payment":
        return invoicing.match_payment(txn_id=args["txn_id"], invoice_id=args["invoice_id"],
                                       allow_processor_fee=bool(args.get("allow_processor_fee")), actor=actor)

    if name == "run_report":
        kind, period = args["report"], _period(args)
        if kind in books.SECTIONS:
            with tx() as c:
                rep = books.SECTIONS[kind](c, period)
            if kind == "ledger":  # 100 rows: return the totals and the uncategorized rows, not every line
                rep = {**rep, "entries": [e for e in rep["entries"] if e["category"] == "Open item"],
                       "note": "entries shows only rows still waiting for a category; the full ledger is in the books PDF"}
            return _summ(rep, 60)
        if kind not in reports.REPORTS:
            raise LedgerError(f"unknown report {kind}")
        with tx() as c:
            rep = reports.REPORTS[kind](c, period)
        pdf = pdfs.render_report(rep, period)
        rep["pdf_workspace_path"] = await upload(pdf, f"reports/{pdf.name}")
        return _summ(rep, 40)

    if name == "query_ledger":
        return _summ(query(args["kind"], _period(args), args.get("filters") or {}))

    if name == "reconcile":
        if args.get("action") == "record_statement_gap":
            if not args.get("gap_id"):
                raise LedgerError("gap_id is required (from action=investigate)")
            return books.record_statement_gap(args["gap_id"], actor=actor)
        with tx() as c:
            return books.investigate(c, _period(args))

    if name == "save_close_summary":
        period = _period(args)
        out = build_books(period, notes_md=args["summary_markdown"], final=False, actor=actor)
        ws = await upload(out["path"], f"reports/{out['path'].name}")
        with tx() as c:
            c.execute("INSERT INTO close_summaries (period, markdown, pdf_path, workspace_path, created_at) VALUES (?,?,?,?,?)"
                      " ON CONFLICT(period) DO UPDATE SET markdown=excluded.markdown, pdf_path=excluded.pdf_path,"
                      " workspace_path=excluded.workspace_path, created_at=excluded.created_at",
                      (period, args["summary_markdown"], str(out["path"]), ws, now()))
            audit(c, actor, "save_close_summary", {"period": period})
        return {"saved": True, "pdf_workspace_path": ws, "status": out["status"], "open_items": out["open_items"]}

    if name == "send_email":
        from .outbox import send
        return send(kind=args["kind"], invoice_id=args["invoice_id"], to=args["to"], subject=args["subject"],
                    body=args["body"], channel="custom_tool")

    raise LedgerError(f"unknown tool {name}")


def build_books(period: str, *, notes_md: str | None = None, final: bool, actor: str = "agent") -> dict:
    """Render the books package. Draft after the agent's run; final when a human delivers it after review."""
    with tx() as c:
        data = books.package(c, period)
        if notes_md is None:
            r = c.execute("SELECT markdown FROM close_summaries WHERE period=?", (period,)).fetchone()
            notes_md = r["markdown"] if r else ""
        n_open = data["open_items"]["open_count"]
        if final:
            status = "Final" if n_open == 0 else f"Final, with {n_open} open item{'s' if n_open != 1 else ''} carried forward"
        else:
            status = f"Draft for human review ({n_open} open item{'s' if n_open != 1 else ''})"
        version = (c.execute("SELECT COUNT(*) AS n FROM deliveries_log WHERE period=?", (period,)).fetchone()["n"] or 0) + 1
    path = pdfs.render_books_package(period, data, status=status, notes_md=notes_md)
    with tx() as c:
        c.execute("INSERT INTO deliveries_log (period, version, status, pdf_path, open_items, created_at) VALUES (?,?,?,?,?,?)",
                  (period, version, status, str(path), n_open, now()))
        audit(c, actor, "deliver_books" if final else "draft_books", {"period": period, "status": status})
    return {"path": path, "status": status, "open_items": n_open, "version": version}


def post_ready(period: str, *, actor: str = "agent") -> dict:
    posted, total = 0, 0
    with tx() as c:
        for t in rows(c.execute("SELECT * FROM transactions WHERE period=? AND status IN ('categorized','review')", (period,))):
            if journal.active_txn_entry(c, t["id"]):
                continue
            journal.post(c, date=t["date"], memo=t["description"][:60], kind="txn", actor=actor, txn_id=t["id"],
                         lines=journal.lines_for_txn(t["funding_account"], t["category"], t["amount_minor"]))
            c.execute("UPDATE transactions SET status='posted' WHERE id=?", (t["id"],))
            posted += 1
            total += abs(t["amount_minor"])
        left = c.execute("SELECT COUNT(*) AS n FROM transactions WHERE period=? AND status='new'", (period,)).fetchone()["n"]
        ok = journal.trial_balance_ok(c)
    return {"entries_posted": posted, "gross_amount_minor": total, "uncategorized_remaining": left, "trial_balance_ok": ok}


def query(kind: str, period: str, f: dict) -> dict:
    with tx() as c:
        if kind == "close_status":
            st = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) AS n FROM transactions WHERE period=? GROUP BY status", (period,))}
            files = rows(c.execute("SELECT name, kind, status FROM files"))
            return {"period": period, "transactions_by_status": st, "files": files,
                    "unbilled_deliveries": len(invoicing.unbilled_deliveries(c, period)),
                    "open_invoices": len(invoicing.open_invoices(c)), "trial_balance_ok": journal.trial_balance_ok(c)}
        if kind == "transactions":
            sql, a = "SELECT id, date, amount_minor, description, source, funding_account, status, category, confidence, cat_method, cat_reason, receipt_file FROM transactions WHERE period=?", [period]
            if f.get("status"):
                sql += " AND status=?"
                a.append(f["status"])
            if f.get("source"):
                sql += " AND source=?"
                a.append(f["source"])
            if f.get("search"):
                sql += " AND description LIKE ?"
                a.append(f"%{f['search'].upper()}%")
            return {"transactions": rows(c.execute(sql + " ORDER BY date LIMIT ?", a + [int(f.get("limit") or 60)]))}
        if kind == "payment_candidates":
            return {"deposits": invoicing.payment_candidates(c, period)}
        if kind == "open_invoices":
            return {"invoices": invoicing.open_invoices(c, reports.bounds(period)[1])}
        if kind == "unbilled_deliveries":
            return {"deliveries": invoicing.unbilled_deliveries(c, period),
                    "note": "retainer and non-billable work is excluded; amounts are computed from hours x rate or fixed fee"}
        if kind == "open_items":
            return books.open_items(c, period)
        if kind == "review_list":
            return {"review": rows(c.execute("SELECT id, date, amount_minor, description, confidence, cat_reason FROM transactions"
                                             " WHERE period=? AND category='9999'", (period,)))}
        if kind == "clients":
            return {"clients": rows(c.execute("SELECT id, name, email, terms_days, revenue_account, aliases FROM clients"))}
        if kind == "chart_of_accounts":
            return {"accounts": coa.as_list()}
        if kind == "account_balance":
            acct = f.get("account", "1000")
            return {"account": acct, "name": coa.NAMES.get(acct), "balance_minor_debit_positive": journal.balances(c, end=reports.bounds(period)[1]).get(acct, 0)}
        if kind == "invoice":
            pass
    if kind == "invoice":
        return invoicing.invoice_detail(int(f.get("invoice_id"))) or {"error": "not found"}
    raise LedgerError(f"unknown query kind {kind}")


def humanize(obj):
    """Add a formatted sibling for every *_minor integer so the model quotes figures instead of computing them."""
    from .money import fmt
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out[k] = humanize(v)
            if k.endswith("_minor") and isinstance(v, int) and k[:-6] not in obj:
                out[k[:-6]] = fmt(v)
            elif k.endswith("_minor") and isinstance(v, dict) and k[:-6] not in obj:
                out[k[:-6]] = {kk: fmt(vv) for kk, vv in v.items() if isinstance(vv, int)}
            elif k.endswith("_minor") and isinstance(v, list) and all(isinstance(x, int) for x in v) and k[:-6] not in obj:
                out[k[:-6]] = [fmt(x) for x in v]
        return out
    if isinstance(obj, list):
        return [humanize(x) for x in obj]
    return obj


def result_text(d: dict) -> str:
    return json.dumps(d, default=str)
