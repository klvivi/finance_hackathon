"""The business categories users see, mapped onto the double-entry chart of accounts.

Per the bookkeeping guidelines there are exactly five categories. Anything that fits none of them is an
open item for a human (posted to Suspense 9999 so the ledger still balances).

Types follow the statements' sign convention:
  Checking:     money in = Income,  money out = Expense
  Credit card:  credit   = Income,  charge    = Expense
Internally every amount is stored from the funding account's view (+ in / - out), so
Type = Income if amount_minor > 0 else Expense, for both accounts.
"""
from __future__ import annotations

CATEGORIES = ["Income", "Rent", "Contractors", "Software", "Bank fees"]
EXPENSE_CATEGORIES = ["Rent", "Contractors", "Software", "Bank fees"]
OPEN_ITEM = "Open item"

ACCOUNT = {"Income": "4000", "Rent": "6100", "Contractors": "5000", "Software": "6200", "Bank fees": "6700"}

_LABEL = {"4000": "Income", "4100": "Income", "4200": "Income", "4900": "Income", "1200": "Income",
          "5000": "Contractors", "6100": "Rent", "6200": "Software", "6250": "Software", "6700": "Bank fees",
          "9999": OPEN_ITEM}

ACCOUNT_LABEL = {"1000": "Checking", "2000": "Credit card", "3000": "Paid personally"}


def label(code: str | None) -> str:
    if not code:
        return OPEN_ITEM
    return _LABEL.get(code, OPEN_ITEM)


def txn_type(amount_minor: int) -> str:
    return "Income" if amount_minor > 0 else "Expense"


def account_for(category: str, *, description: str = "") -> str | None:
    if category not in ACCOUNT:
        return None
    if category == "Income" and "INTEREST" in description.upper():
        return "4900"
    return ACCOUNT[category]


def fits(category: str, amount_minor: int) -> bool:
    """Income only on money in; expense categories only on money out."""
    return (category == "Income") == (amount_minor > 0)
