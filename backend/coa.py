"""Fixed chart of accounts. Categorization may only pick from this list; it never invents one."""
from __future__ import annotations

# code, name, type, normal balance, cash-flow section, description used by the model
ACCOUNTS: list[tuple[str, str, str, str, str, str]] = [
    ("1000", "Checking - Harbor State Bank", "asset", "debit", "cash", "Operating bank account"),
    ("1200", "Accounts Receivable", "asset", "debit", "operating", "Client invoices not yet paid; invoice payments clear this"),
    ("1300", "Prepaid Expenses", "asset", "debit", "operating", "Annual prepayments (insurance, software paid yearly)"),
    ("1500", "Computer Equipment", "asset", "debit", "investing", "Laptops, monitors, hardware over $500 per item"),
    ("2000", "Meridian Business Card", "liability", "credit", "operating", "Company credit card; card payments from checking go here"),
    ("2100", "Accrued Liabilities", "liability", "credit", "operating", "Bills incurred but not yet paid"),
    ("3000", "Owner's Equity / Contributions", "equity", "credit", "financing", "Owner contributions, incl. business costs paid personally"),
    ("3100", "Owner's Draws", "equity", "debit", "financing", "Money taken out by the owner"),
    ("3900", "Retained Earnings", "equity", "credit", "financing", "Prior-period earnings"),
    ("4000", "Design Services Revenue", "revenue", "credit", "operating", "Brand, web and campaign design work"),
    ("4100", "Development Services Revenue", "revenue", "credit", "operating", "Website and app development work"),
    ("4200", "Retainer Revenue", "revenue", "credit", "operating", "Monthly retainers"),
    ("4900", "Interest & Other Income", "revenue", "credit", "operating", "Bank interest, refunds of prior-year costs"),
    ("5000", "Contractors & Freelancers", "expense", "debit", "operating", "Payments to freelance designers/developers (Zelle, Upwork, Venmo)"),
    ("6000", "Payroll & Payroll Fees", "expense", "debit", "operating", "Gusto payroll runs and payroll service fees"),
    ("6100", "Rent & Coworking", "expense", "debit", "operating", "Studio rent, coworking day passes"),
    ("6200", "Software & Subscriptions", "expense", "debit", "operating", "SaaS tools: design, collaboration, AI assistants"),
    ("6250", "Cloud Hosting", "expense", "debit", "operating", "AWS, Vercel, hosting for client sites"),
    ("6300", "Advertising & Marketing", "expense", "debit", "operating", "Ads, sponsorships, marketing assets"),
    ("6400", "Meals & Entertainment", "expense", "debit", "operating", "Client and team meals, coffee"),
    ("6450", "Travel & Transport", "expense", "debit", "operating", "Flights, hotels, rideshare, transit"),
    ("6500", "Office Supplies & Equipment <$500", "expense", "debit", "operating", "Supplies, small hardware, Amazon orders"),
    ("6600", "Professional Fees", "expense", "debit", "operating", "Accounting, legal, consultants"),
    ("6700", "Bank & Payment Processing Fees", "expense", "debit", "operating", "Bank service fees, Stripe/PayPal fees"),
    ("6800", "Utilities & Internet", "expense", "debit", "operating", "Electricity, internet, phone"),
    ("6900", "Insurance", "expense", "debit", "operating", "Business insurance premiums"),
    ("9999", "Suspense - Needs Review", "suspense", "debit", "operating", "Temporary holding account for items awaiting human review"),
]

BY_CODE = {a[0]: a for a in ACCOUNTS}
NAMES = {a[0]: a[1] for a in ACCOUNTS}

# Accounts a bank/card transaction may be categorized to (everything except the funding accounts' own cash).
CATEGORIZABLE = [a[0] for a in ACCOUNTS if a[0] not in ("1000", "3900")]


def acct_type(code: str) -> str:
    return BY_CODE[code][2]


def is_valid(code: str) -> bool:
    return code in BY_CODE


def as_list() -> list[dict]:
    return [
        {"code": c, "name": n, "type": t, "description": d}
        for c, n, t, _nb, _cf, d in ACCOUNTS
    ]
