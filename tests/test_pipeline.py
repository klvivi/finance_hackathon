"""Offline end-to-end test of the bookkeeping guideline flow (no ZooWork calls, no cost).

Plays the agent's part with scripted decisions: Pull -> Categorize -> Reconcile -> Review -> Deliver.
    .venv/Scripts/python -m tests.test_pipeline
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from backend import books, config, db, invoicing, journal, reset, tools
from backend import categories as cats
from backend.money import fmt

# Never touch the live ledger: run against a throwaway data directory.
_TMP = Path(tempfile.mkdtemp(prefix="ledgerly-test-"))
config.RUNTIME, config.INBOX, config.OUT = _TMP, _TMP / "inbox", _TMP / "out"
config.DB_PATH = _TMP / "ledgerly.sqlite"
db.close()

P = "2026-10"
CSV = "brightwave_transactions_2026-10.csv"
UNKNOWN = ("AMZN", "DELTA", "MARRIOTT", "UBER", "APPLE.COM", "ATM WITHDRAWAL", "VENMO", "CHECK 1043", "TRANSFER TO SAVINGS")


async def _upload(path, ws):
    return f"/workspace/{ws}"


def call(name, **args):
    return asyncio.run(tools.execute(name, args, upload=_upload, actor="test"))


def load_demo_files():
    from backend.ingest import register_upload
    for p in sorted(config.DEMO_DIR.rglob("*")):
        if p.is_file():
            register_upload(p.name, p.read_bytes())


def section(name):
    with db.tx() as c:
        return books.SECTIONS[name](c, P)


def run_close(learned_rules=None, fix_broken_row=True):
    reset.reset()
    load_demo_files()
    early = call("ingest_file", file="receipt_delta_air.png", receipt={"merchant": "Delta Air Lines", "date": "2026-10-23", "total": "412.30", "payment_method": "card"})
    assert early.get("retry_later"), early

    # 1. Pull transactions
    r = call("ingest_file", file=CSV)
    assert r["by_account"] == {"checking": 34, "credit_card": 65}, r["by_account"]
    assert len(r["duplicates_skipped"]) == 1 and r["errors"][0]["raw"].startswith("2026-10-2 7"), r
    assert r["bank_statement"]["opening_balance_minor"] == 4961387
    if fix_broken_row:
        r2 = call("ingest_file", file=CSV, fixes=[{"row": r["errors"][0]["row"], "date": "2026-10-27"}])
        assert r2["new_transactions"] == 1 and not r2["errors"]
    call("ingest_file", file="brightwave_deliveries_2026-10.csv")
    for f, rc in (("receipt_delta_air.png", {"merchant": "Delta Air Lines", "date": "2026-10-23", "total": "412.30"}),
                  ("receipt_amazon_order.png", {"merchant": "Amazon.com", "date": "2026-10-11", "total": "64.20"})):
        out = call("ingest_file", file=f, receipt={**rc, "payment_method": "card"})
        assert out["matched_existing_txn"] and out["was"] == "missing", out

    # 2. Income: payments to invoices, Stripe deposit -> invoice + fee, bill the rest
    cands = call("query_ledger", kind="payment_candidates", period=P)["deposits"]
    for d in cands:
        if d["suggested_invoices"]:
            call("match_payment", txn_id=d["txn_id"], invoice_id=d["suggested_invoices"][0]["invoice_id"])
    unb = call("query_ledger", kind="unbilled_deliveries", period=P)["deliveries"]
    by_client: dict[str, list[int]] = {}
    for d in unb:
        by_client.setdefault(d["client_id"], []).append(d["id"])
    stripe = next(d for d in cands if d["description"].startswith("STRIPE"))
    maple = call("create_invoice", client_id="maple", delivery_ids=by_client.pop("maple"), issue_date="2026-10-14", linked_txn_ids=[stripe["txn_id"]])
    assert call("match_payment", txn_id=stripe["txn_id"], invoice_id=maple["invoice_id"], allow_processor_fee=True)["processor_fee_minor"] == 3510
    for cl, ids in by_client.items():
        call("create_invoice", client_id=cl, delivery_ids=ids, issue_date="2026-10-31")

    # 3. Categorize: only five categories, unknowns -> open items
    auto = call("categorize", mode="auto", period=P, learned_rules=learned_rules or [])
    decisions = []
    for i in auto["needs_judgment"]:
        d = i["description"]
        if any(k in d for k in UNKNOWN):
            decisions.append({"txn_id": i["txn_id"], "category": "Open item", "confidence": 0.3, "reason": "not one of the five categories"})
        elif i["amount_minor"] > 0:
            decisions.append({"txn_id": i["txn_id"], "category": "Income", "confidence": 0.9, "reason": "money in"})
        elif "GARCIA" in d:
            decisions.append({"txn_id": i["txn_id"], "category": "Contractors", "confidence": 0.8, "reason": "Zelle to a person, like other contractors"})
        else:
            decisions.append({"txn_id": i["txn_id"], "category": "Software", "confidence": 0.9, "reason": "SaaS"})
    bad = call("categorize", mode="decide", decisions=[{"txn_id": decisions[0]["txn_id"], "category": "Travel", "confidence": 1, "reason": "x"}])
    assert bad["rejected"], "categories outside the five must be rejected"
    dec = call("categorize", mode="decide", decisions=decisions)
    assert not dec["rejected"], dec["rejected"]
    post = call("post_entry", post_ready=True, period=P)
    assert post["trial_balance_ok"] and post["uncategorized_remaining"] == 0, post
    return auto, dec


def test_guideline_close():
    auto1, dec1 = run_close()
    rec = section("reconciliation")
    assert rec["status"] == "matched", rec   # theoretical money == bank statement balance
    L, E, I, C, O = (section(k) for k in ("ledger", "expenses", "income", "contractors", "open_items"))
    assert L["count"] == 99
    assert {x["category"] for x in E["categories"]} == {"Rent", "Contractors", "Software", "Bank fees", "Open item"}
    docs = {x["from"]: x["document"] for x in I["items"]}
    assert docs["Northwind Coffee Co"].startswith("Invoice") and docs.get("Bluebird Bakery") == "Missing", docs
    garcia = next(k for k in C["contractors"] if k["contractor"] == "M. Garcia")
    assert not garcia["form_ok"]
    kinds: dict[str, int] = {}
    for i in O["items"]:
        kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
    assert kinds == {"category": 10, "receipt": 5, "document": 1, "contractor_form": 1}, kinds
    n_open_1 = sum(1 for i in O["items"] if i["kind"] == "category")

    # Human review: categorize, waive a receipt, document income, record the W-9, then deliver.
    learned = []
    for i in O["items"]:
        if i["kind"] == "category" and "AMZN" in i["item"]:
            learned.append(invoicing.resolve_review(i["txn_id"], cats.ACCOUNT["Software"])["learned_rule"])
    books.resolve(next(i["key"] for i in O["items"] if i["kind"] == "receipt"), resolution="not needed")
    books.resolve(next(i["key"] for i in O["items"] if i["kind"] == "document"), resolution="documented", note="Workshop fee, invoice to follow")
    books.resolve("w9:M. Garcia", resolution="received", note="W-9 received Nov 3")
    O2 = section("open_items")
    assert O2["open_count"] < O["open_count"]
    out = tools.build_books(P, notes_md="Test notes", final=True, actor="test")
    assert out["path"].exists() and out["status"].startswith("Final")
    with db.tx() as c:
        assert journal.trial_balance_ok(c)

    # Next run with the rules the agent would have saved to memory: fewer unknown categories
    run_close(learned_rules=learned)
    O3 = section("open_items")
    n_open_2 = sum(1 for i in O3["items"] if i["kind"] == "category")
    assert n_open_2 < n_open_1, (n_open_1, n_open_2)
    print(f"open category items: run1={n_open_1} run2={n_open_2}; reconciliation {rec['status']}; "
          f"income {fmt(L['total_income_minor'])}, expenses {fmt(L['total_expenses_minor'])}; open items {O['open_count']} -> {O2['open_count']}")


def test_reconciliation_flags_a_missing_row():
    run_close(fix_broken_row=False)
    rec = section("reconciliation")
    assert rec["status"] == "difference" and rec["difference_minor"] == -125000, rec
    assert any(i["kind"] == "reconciliation" and i["status"] == "Open" for i in section("open_items")["items"])


def test_finds_a_transaction_missing_from_the_export():
    reset.reset()
    load_demo_files()
    from backend.ingest import register_upload
    sc = config.DATA / "scenarios" / "missing_transaction" / CSV
    register_upload(sc.name, sc.read_bytes())  # replaces the full export with one that dropped the cheque
    call("ingest_file", file=CSV)
    auto = call("categorize", mode="auto", period=P)
    call("categorize", mode="decide", decisions=[{"txn_id": i["txn_id"], "category": "Open item", "confidence": .3, "reason": "test"} for i in auto["needs_judgment"]])
    call("post_entry", post_ready=True, period=P)
    assert section("reconciliation")["difference_minor"] == -125000
    inv = call("reconcile", action="investigate", period=P)
    gap = next(f for f in inv["findings"] if f["type"] == "statement_gap")
    assert inv["fully_explained"] and gap["between"] == ["2026-10-24", "2026-10-27"], inv
    call("reconcile", action="record_statement_gap", gap_id=gap["id"])
    call("post_entry", post_ready=True, period=P)
    assert section("reconciliation")["status"] == "matched"
    assert any("UNLISTED BANK DEBIT" in i["item"] and i["status"] == "Open" for i in section("open_items")["items"])


def test_journal_is_immutable():
    reset.reset()
    import sqlite3
    try:
        with db.tx() as c:
            c.execute("UPDATE journal_lines SET debit_minor=1 WHERE id=1")
        raise AssertionError("update should fail")
    except sqlite3.IntegrityError:
        pass
    try:
        with db.tx() as c:
            journal.post(c, date="2026-10-01", memo="x", kind="manual", actor="t", lines=[("1000", 100, 0), ("6200", 0, 99)])
        raise AssertionError("unbalanced should fail")
    except journal.LedgerError:
        pass


if __name__ == "__main__":
    test_journal_is_immutable()
    test_reconciliation_flags_a_missing_row()
    test_finds_a_transaction_missing_from_the_export()
    test_guideline_close()
    print("ok")
