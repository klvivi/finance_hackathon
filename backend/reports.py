"""Reports are pure functions of the journal. Integer minor units; the model only narrates them."""
from __future__ import annotations

import calendar
import re
import statistics
from collections import defaultdict
from datetime import date, timedelta

from . import coa, invoicing, journal
from .db import rows
from .ingest import norm_key
from .money import fmt, pct_change

SUBSCRIPTION_HINT = re.compile(r"SUBSCR|MONTHLY|TEAM|PLAN|\.COM|\.APP|\.IO")


def bounds(period: str) -> tuple[str, str]:
    y, m = map(int, period.split("-"))
    return f"{y}-{m:02d}-01", f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


def prev_period(period: str) -> str:
    y, m = map(int, period.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def _pl_accounts(bal: dict[str, int]) -> dict:
    rev = {a: -v for a, v in bal.items() if coa.acct_type(a) == "revenue" and v}
    exp = {a: v for a, v in bal.items() if coa.acct_type(a) == "expense" and v}
    sus = bal.get("9999", 0)
    tr, te = sum(rev.values()), sum(exp.values())
    return {"revenue": rev, "expenses": exp, "suspense": sus, "total_revenue": tr, "total_expenses": te,
            "net_income": tr - te - sus}


def pnl(c, period: str) -> dict:
    s, e = bounds(period)
    cur = _pl_accounts(journal.balances(c, start=s, end=e))
    ps, pe = bounds(prev_period(period))
    prev = _pl_accounts(journal.balances(c, start=ps, end=pe))
    def lines(kind):
        accts = sorted(set(cur[kind]) | set(prev[kind]))
        return [{"account": a, "name": coa.NAMES[a], "amount_minor": cur[kind].get(a, 0),
                 "prior_minor": prev[kind].get(a, 0), "change_pct": pct_change(cur[kind].get(a, 0), prev[kind].get(a, 0))}
                for a in accts]
    return {"report": "pnl", "period": period, "revenue": lines("revenue"), "expenses": lines("expenses"),
            "suspense_pending_review_minor": cur["suspense"],
            "total_revenue_minor": cur["total_revenue"], "total_expenses_minor": cur["total_expenses"],
            "net_income_minor": cur["net_income"], "prior": {"period": prev_period(period),
            "total_revenue_minor": prev["total_revenue"], "total_expenses_minor": prev["total_expenses"],
            "net_income_minor": prev["net_income"]}}


def balance_sheet(c, period: str) -> dict:
    _, e = bounds(period)
    bal = journal.balances(c, end=e)
    def sect(t, sign):
        return [{"account": a, "name": coa.NAMES[a], "amount_minor": sign * v}
                for a, v in sorted(bal.items()) if coa.acct_type(a) == t and v]
    assets, liabs, equity = sect("asset", 1), sect("liability", -1), sect("equity", -1)
    earnings = -sum(v for a, v in bal.items() if coa.acct_type(a) in ("revenue", "expense", "suspense"))
    equity.append({"account": "-", "name": "Current-period earnings (since opening)", "amount_minor": earnings})
    ta, tl, te = (sum(x["amount_minor"] for x in s) for s in (assets, liabs, equity))
    return {"report": "balance_sheet", "as_of": e, "assets": assets, "liabilities": liabs, "equity": equity,
            "total_assets_minor": ta, "total_liabilities_minor": tl, "total_equity_minor": te,
            "balanced": ta == tl + te, "trial_balance_ok": journal.trial_balance_ok(c)}


def cash_flow(c, period: str) -> dict:
    """Direct method from the checking account: each cash movement classified by its counter-account."""
    s, e = bounds(period)
    opening = journal.balances(c, end=(date.fromisoformat(s) - timedelta(days=1)).isoformat()).get("1000", 0)
    sections: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in c.execute(
            "SELECT e.id, l.account, l.debit_minor - l.credit_minor AS cash FROM journal_lines l JOIN journal_entries e ON e.id=l.entry_id"
            " WHERE l.account='1000' AND e.date BETWEEN ? AND ?", (s, e)).fetchall():
        others = c.execute("SELECT account, debit_minor + credit_minor AS amt FROM journal_lines WHERE entry_id=? AND account!='1000'"
                           " ORDER BY amt DESC", (r["id"],)).fetchall()
        counter = others[0]["account"] if others else "9999"
        sect = coa.BY_CODE[counter][4]
        label = coa.NAMES[counter]
        sections[sect][label] += r["cash"]
    out = {k: [{"line": n, "amount_minor": v} for n, v in sorted(d.items(), key=lambda x: x[1])] for k, d in sections.items()}
    totals = {k: sum(d.values()) for k, d in sections.items()}
    net = sum(totals.values())
    closing = journal.balances(c, end=e).get("1000", 0)
    return {"report": "cash_flow", "period": period, "opening_cash_minor": opening, "sections": out,
            "section_totals_minor": totals, "net_change_minor": net, "closing_cash_minor": closing,
            "reconciles": opening + net == closing}


def spend_by_category(c, period: str) -> dict:
    p = pnl(c, period)
    total = p["total_expenses_minor"] or 1
    items = sorted(p["expenses"], key=lambda x: -x["amount_minor"])
    for i in items:
        i["share_pct"] = round(i["amount_minor"] / total * 100, 1)
    return {"report": "spend_by_category", "period": period, "categories": items,
            "total_minor": p["total_expenses_minor"], "prior_total_minor": p["prior"]["total_expenses_minor"]}


def ar_aging(c, period: str) -> dict:
    _, e = bounds(period)
    as_of = min(e, date.today().isoformat())  # a weekly check mid-month ages invoices as of today
    return {"report": "ar_aging", **invoicing.ar_aging(c, as_of)}


def anomalies(c, period: str) -> dict:
    s, e = bounds(period)
    txns = rows(c.execute("SELECT * FROM transactions WHERE period=? AND status NOT IN ('duplicate','linked')", (period,)))
    hist = rows(c.execute("SELECT * FROM transactions WHERE period<? AND status='posted'", (period,)))
    by_key: dict[str, list[int]] = defaultdict(list)
    for h in hist:
        by_key[norm_key(h["description"])].append(abs(h["amount_minor"]))
    found = []
    # 1) possible duplicate charges: a subscription billed twice in one month, or any other charge
    #    of $20+ repeated by the same merchant on the same day
    seen: dict[tuple, dict] = {}
    for t in sorted(txns, key=lambda t: t["date"]):
        if t["amount_minor"] > -2000:
            continue
        k = (norm_key(t["description"]), t["amount_minor"])
        subscription = t["category"] in ("6200", "6250")
        if k in seen and (subscription or t["date"] == seen[k]["date"]):
            p = seen[k]
            found.append({"type": "possible_duplicate_charge", "txn_ids": [p["id"], t["id"]],
                          "detail": f"{t['description']} charged {fmt(-t['amount_minor'])} on {p['date']} and {t['date']}",
                          "amount_minor": -t["amount_minor"], "suggested_action": "Ask the vendor for a refund if only one seat/plan was bought"})
        seen[k] = t
    # 2) unusual amounts vs history for the same merchant
    for t in txns:
        h = by_key.get(norm_key(t["description"]))
        if t["amount_minor"] < 0 and h:
            med = statistics.median(h)
            a = -t["amount_minor"]
            if a >= 2.5 * med and a - med >= 20000:
                found.append({"type": "unusual_amount", "txn_ids": [t["id"]],
                              "detail": f"{t['description']} {fmt(a)} vs typical {fmt(int(med))} ({a / med:.1f}x)",
                              "amount_minor": a, "typical_minor": int(med),
                              "suggested_action": "Check usage/billing console for a cost spike or misconfiguration"})
    # 3) new recurring subscriptions: merchants never seen before, categorized as software/hosting
    for t in txns:
        if (t["amount_minor"] < 0 and t["category"] in ("6200", "6250") and norm_key(t["description"]) not in by_key
                and SUBSCRIPTION_HINT.search(t["description"])):
            found.append({"type": "new_recurring_subscription", "txn_ids": [t["id"]],
                          "detail": f"New subscription: {t['description']} {fmt(-t['amount_minor'])}/mo",
                          "amount_minor": -t["amount_minor"], "annualized_minor": -t["amount_minor"] * 12,
                          "suggested_action": "Confirm someone approved this tool and who owns it"})
    dupes = rows(c.execute("SELECT id, description, cat_reason FROM transactions WHERE period=? AND status='duplicate'", (period,)))
    return {"report": "anomalies", "period": period, "anomalies": found,
            "import_duplicates_removed": [{"txn_id": d["id"], "description": d["description"]} for d in dupes]}


def month_over_month(c, period: str) -> dict:
    p = pnl(c, period)
    movers = sorted(p["expenses"] + p["revenue"], key=lambda x: -abs(x["amount_minor"] - x["prior_minor"]))[:6]
    return {"report": "mom", "period": period, "prior_period": p["prior"]["period"],
            "revenue_minor": [p["total_revenue_minor"], p["prior"]["total_revenue_minor"]],
            "expenses_minor": [p["total_expenses_minor"], p["prior"]["total_expenses_minor"]],
            "net_income_minor": [p["net_income_minor"], p["prior"]["net_income_minor"]],
            "biggest_movers": [{**m, "delta_minor": m["amount_minor"] - m["prior_minor"]} for m in movers]}


def cash_outlook(c, period: str, days: int = 30) -> dict:
    _, e = bounds(period)
    end = date.fromisoformat(e)
    horizon = (end + timedelta(days=days)).isoformat()
    cash = journal.balances(c, end=e).get("1000", 0)
    card = -journal.balances(c, end=e).get("2000", 0)
    ar = invoicing.open_invoices(c, e)
    expected_in = sum(i["balance_minor"] for i in ar if i["due_date"] <= horizon)
    # Recurring outflows: merchants paid from checking in both this and the prior month (payroll, rent, utilities...)
    cur = rows(c.execute("SELECT description, amount_minor FROM transactions WHERE period=? AND funding_account='1000'"
                         " AND amount_minor<0 AND status='posted' AND category NOT IN ('2000')", (period,)))
    prior_keys = {norm_key(r["description"]) for r in c.execute(
        "SELECT description FROM transactions WHERE period=? AND funding_account='1000' AND amount_minor<0", (prev_period(period),))}
    recurring = sum(-r["amount_minor"] for r in cur if norm_key(r["description"]) in prior_keys)
    projected = cash + expected_in - recurring - card
    return {"report": "cash_outlook", "as_of": e, "horizon": horizon, "cash_minor": cash,
            "expected_collections_minor": expected_in, "recurring_outflows_minor": recurring,
            "card_balance_due_minor": card, "projected_cash_minor": projected,
            "collections": [{"number": i["number"], "client": i["client"], "balance_minor": i["balance_minor"], "due": i["due_date"]}
                            for i in ar if i["due_date"] <= horizon],
            "method": "cash + open AR due within horizon - recurring checking outflows seen in both months - card balance"}


REPORTS = {"pnl": pnl, "balance_sheet": balance_sheet, "cash_flow": cash_flow, "spend_by_category": spend_by_category,
           "ar_aging": ar_aging, "anomalies": anomalies, "mom": month_over_month, "cash_outlook": cash_outlook}
