"""Generate the synthetic Brightwave Studio dataset (fictional agency; no real financial data).

Writes:
  data/seed/history.json                         September 2026 (already closed), opening balances, clients,
                                                 contractor registry, invoices
  data/demo/brightwave_transactions_2026-10.csv  ~100 October transactions, checking + credit card, with the
                                                 bank statement balance and receipt status
  data/demo/brightwave_deliveries_2026-10.csv    delivery log (work done but not yet billed)
  data/demo/receipts/*.png                       receipts for two transactions marked "missing" in the CSV

Sign convention in the CSV (as on the statements):
  Checking:     + money in (income),  - money out (expense)
  Credit card:  + charge (expense),   - credit (income)

Categories used: Income, Rent, Contractors, Software, Bank fees. A few rows deliberately fit none of them
(they must end up as open items for a human).

Deterministic: same output every run.
"""
from __future__ import annotations

import csv
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "data" / "seed"
DEMO = ROOT / "data" / "demo"
R = random.Random(20261031)

# Ledger account behind each category (the chart of accounts stays double-entry).
CAT = {"Income": "4000", "Interest": "4900", "Rent": "6100", "Contractors": "5000", "Software": "6200", "Bank fees": "6700"}

CLIENTS = [
    {"id": "northwind", "name": "Northwind Coffee Co", "email": "ap@northwindcoffee.example", "aliases": ["NORTHWIND COFFEE"], "terms_days": 10, "revenue_account": "4200"},
    {"id": "halcyon", "name": "Halcyon Health", "email": "ap@halcyonhealth.example", "aliases": ["HALCYON HEALTH"], "terms_days": 30, "revenue_account": "4100"},
    {"id": "pinecrest", "name": "Pinecrest Realty", "email": "billing@pinecrestrealty.example", "aliases": ["PINECREST RLTY", "PINECREST REALTY"], "terms_days": 15, "revenue_account": "4000"},
    {"id": "orbit", "name": "Orbit Fitness", "email": "finance@orbitfitness.example", "aliases": ["ORBIT FITNESS"], "terms_days": 30, "revenue_account": "4100"},
    {"id": "lumen", "name": "Lumen Labs", "email": "accounts@lumenlabs.example", "aliases": ["LUMEN LABS"], "terms_days": 30, "revenue_account": "4000"},
    {"id": "kestrel", "name": "Kestrel Outdoor", "email": "ap@kestreloutdoor.example", "aliases": ["KESTREL OUTDOOR"], "terms_days": 15, "revenue_account": "4000"},
    {"id": "maple", "name": "Maple & Co", "email": "hello@mapleandco.example", "aliases": [], "terms_days": 0, "revenue_account": "4000"},
]

# Who we pay as contractors, how to recognise them, and the tax form on file.
CONTRACTORS = [
    {"name": "A. Nguyen", "match": ["A. NGUYEN"], "form": "W-9 on file", "needs_1099": True, "ytd_before_sep_minor": 1260000},
    {"name": "D. Kim", "match": ["D.KIM"], "form": "W-9 on file (paid through Gusto)", "needs_1099": True, "ytd_before_sep_minor": 1400000},
    {"name": "S. Ito", "match": ["S. ITO"], "form": "W-9 on file", "needs_1099": True, "ytd_before_sep_minor": 330000},
    {"name": "L. Okafor", "match": ["L OKAFOR"], "form": "W-8BEN on file (foreign contractor)", "needs_1099": False, "ytd_before_sep_minor": 540000},
    {"name": "Upwork freelancers", "match": ["UPWORK"], "form": "Paid through Upwork (platform reports on 1099-K)", "needs_1099": False, "ytd_before_sep_minor": 785000},
    {"name": "M. Garcia", "match": ["M. GARCIA"], "form": "", "needs_1099": True, "ytd_before_sep_minor": 0},
]

BUILTIN_RULES = [
    {"pattern": "GREENPOINT PROPERTIES", "account": "6100", "note": "Studio landlord"},
    {"pattern": "STORESAFE", "account": "6100", "note": "Storage unit"},
    {"pattern": "MONTHLY SERVICE FEE", "account": "6700", "note": "Bank fee"},
    {"pattern": "INTEREST PAYMENT", "account": "4900", "note": "Bank interest"},
]

# (descriptor, base amount $, jitter) - all Software, charged to the credit card
SAAS = [
    ("FIGMA *MONTHLY", 75.00, 0), ("SLACK T0ABC123", 87.50, 0), ("NOTION LABS INC", 48.00, 0),
    ("ADOBE *CREATIVE CLOUD", 359.88, 0), ("GOOGLE *WORKSPACE_BRIGHTW", 84.00, 0), ("ZOOM.US 888-799-9666", 15.99, 0),
    ("GITHUB, INC.", 44.00, 0), ("WEBFLOW.COM", 74.00, 0), ("1PASSWORD", 23.94, 0), ("LINEAR.APP", 40.00, 0),
    ("LOOM, INC.", 60.00, 0), ("DROPBOX*BUSINESS", 30.00, 0), ("AWS EMEA AWS.AMAZON.CO", 380.00, 0.06),
    ("VERCEL INC.", 20.00, 0), ("CANVA* PRO TEAMS", 30.00, 0), ("MIRO.COM", 48.00, 0), ("CALENDLY", 36.00, 0),
    ("MAILCHIMP *MONTHLY", 110.00, 0), ("HUBSPOT INC", 90.00, 0), ("GRAMMARLY", 30.00, 0), ("ZAPIER.COM/CHARGE", 69.00, 0),
    ("AIRTABLE.COM", 45.00, 0), ("ASANA.COM", 54.99, 0), ("CLOUDFLARE", 25.00, 0), ("SENTRY.IO", 26.00, 0),
    ("POSTMARK APP", 15.00, 0), ("TYPEFORM", 59.00, 0), ("INTERCOM INC", 74.00, 0), ("HOTJAR", 39.00, 0),
    ("DESCRIPT INC", 24.00, 0), ("SHUTTERSTOCK", 49.00, 0), ("MIDJOURNEY INC.", 30.00, 0),
]
USAGE = [("TWILIO SENDGRID", 18.00, 0.4, 5), ("NAMECHEAP.COM", 14.98, 0.3, 3)]  # several charges a month


def amt(base: float, jit: float) -> float:
    return round(base * (1 + R.uniform(-jit, jit)), 2) if jit else base


def day(lo: int = 1, hi: int = 28) -> int:
    return R.randint(lo, hi)


def tx(date, desc, account, amount, category, receipt="ok"):
    """amount uses the CSV convention for its account (checking: +in/-out, credit: +charge/-credit)."""
    return {"date": date, "desc": desc, "account": account, "amount": round(amount, 2), "category": category, "receipt": receipt}


def card_month(y: int, m: int) -> list[dict]:
    rows = [tx(f"{y}-{m:02d}-{day():02d}", d, "credit", amt(b, j), "Software") for d, b, j in SAAS]
    for d, b, j, n in USAGE:
        rows += [tx(f"{y}-{m:02d}-{day():02d}", d, "credit", amt(b, j), "Software") for _ in range(n)]
    rows += [tx(f"{y}-{m:02d}-{day():02d}", "FOREIGN TRANSACTION FEE", "credit", amt(1.45, 0.3), "Bank fees") for _ in range(2)]
    return rows


def checking_month(y: int, m: int, mm: str) -> list[dict]:
    P = f"{y}-{m:02d}"
    return [
        tx(f"{P}-01", f"GREENPOINT PROPERTIES RENT {mm}", "checking", -3200.00, "Rent"),
        tx(f"{P}-01", "STORESAFE BROOKLYN UNIT 114", "checking", -189.00, "Rent"),
        tx(f"{P}-06", "UPWORK -642019 ESCROW", "checking", -950.00, "Contractors"),
        tx(f"{P}-08", "ZELLE TO A. NGUYEN", "checking", -1800.00, "Contractors"),
        tx(f"{P}-09", "WIRE OUT L OKAFOR LAGOS NG", "checking", -1150.00, "Contractors"),
        tx(f"{P}-09", "WIRE TRANSFER FEE", "checking", -25.00, "Bank fees"),
        tx(f"{P}-14", "GUSTO CONTRACTOR PMT D.KIM", "checking", -2000.00, "Contractors"),
        tx(f"{P}-16", "ZELLE TO S. ITO", "checking", -650.00, "Contractors"),
        tx(f"{P}-20", "UPWORK -651180 ESCROW", "checking", -1275.00, "Contractors"),
        tx(f"{P}-24", "ZELLE TO A. NGUYEN", "checking", -900.00, "Contractors"),
        tx(f"{P}-30", "MONTHLY SERVICE FEE", "checking", -15.00, "Bank fees"),
        tx(f"{P}-30", "INTEREST PAYMENT", "checking", 2.87 if m == 9 else 3.12, "Interest", receipt=""),
    ] + [tx(f"{P}-{d:02d}", "ACH BATCH FEE", "checking", -5.00, "Bank fees") for d in (7, 14, 21, 28)]


# ---------------------------------------------------------------- September (history, already closed)
def september() -> tuple[list[dict], list[dict], dict]:
    rows = card_month(2026, 9) + checking_month(2026, 9, "SEP") + [
        tx("2026-09-04", "ACH CREDIT NORTHWIND COFFEE INV-1035", "checking", 6000.00, "Income", receipt=""),
        tx("2026-09-18", "WIRE IN HALCYON HEALTH INV-1033", "checking", 9800.00, "Income", receipt=""),
        tx("2026-09-18", "INCOMING WIRE FEE", "checking", -15.00, "Bank fees"),
        tx("2026-09-22", "ACH CREDIT PINECREST RLTY INV-1036", "checking", 4200.00, "Income", receipt=""),
    ]
    rows.sort(key=lambda r: (r["date"], r["desc"]))
    invoices = [
        {"number": "INV-1033", "client": "halcyon", "issue": "2026-08-20", "due": "2026-09-19", "lines": [["Patient portal build - phase 1", 1, 9800.00]], "opening": True},
        {"number": "INV-1036", "client": "pinecrest", "issue": "2026-08-25", "due": "2026-09-09", "lines": [["Listing microsite design", 1, 4200.00]], "opening": True},
        {"number": "INV-1035", "client": "northwind", "issue": "2026-09-01", "due": "2026-09-11", "lines": [["September retainer", 1, 6000.00]]},
        {"number": "INV-1038", "client": "halcyon", "issue": "2026-09-15", "due": "2026-10-15", "lines": [["Patient portal build - phase 2", 1, 14200.00]]},
        {"number": "INV-1040", "client": "pinecrest", "issue": "2026-09-20", "due": "2026-10-05", "lines": [["Open house campaign: print + social kit", 1, 3750.00]]},
        {"number": "INV-1041", "client": "northwind", "issue": "2026-10-01", "due": "2026-10-11", "lines": [["October retainer", 1, 6000.00]]},
    ]
    opening_ar = sum(sum(q * p for _, q, p in i["lines"]) for i in invoices if i.get("opening"))
    opening = {"date": "2026-08-31", "checking": 41800.00,
               "lines": [["1000", 41800.00, 0], ["1200", opening_ar, 0], ["1500", 6400.00, 0], ["2000", 0, 2980.00], ["3000", 0, 20000.00]]}
    dr = sum(l[1] for l in opening["lines"])
    cr = sum(l[2] for l in opening["lines"])
    opening["lines"].append(["3900", 0, round(dr - cr, 2)])
    return rows, invoices, opening


# ---------------------------------------------------------------- October (the file the user uploads)
def october() -> list[dict]:
    rows = [r for r in card_month(2026, 10) if not r["desc"].startswith("AWS")]
    rows += checking_month(2026, 10, "OCT")
    rows += [
        # credit card: anomalies and items that fit no category
        tx("2026-10-14", "AWS EMEA AWS.AMAZON.CO", "credit", 1690.42, "Software"),           # 4.7x usual
        tx("2026-10-13", "FIGMA *MONTHLY", "credit", 75.00, "Software"),                       # charged twice
        tx("2026-10-06", "FRAMER.COM SUBSCR", "credit", 30.00, "Software"),                    # new subscription
        tx("2026-10-07", "OPENAI *CHATGPT TEAM", "credit", 150.00, "Software"),                # new subscription
        tx("2026-10-02", "ANNUAL MEMBERSHIP FEE", "credit", 95.00, "Bank fees"),
        tx("2026-10-04", "FIGMA *SEAT ADD-ON", "credit", 15.00, "Software"),
        tx("2026-10-04", "GOOGLE *WORKSPACE ADD'L USER", "credit", 14.00, "Software"),
        tx("2026-10-10", "SLACK SEAT TRUE-UP", "credit", 21.88, "Software"),
        tx("2026-10-16", "LOOM ADD'L CREATOR SEAT", "credit", 15.00, "Software"),
        tx("2026-10-18", "ADOBE STOCK", "credit", 29.99, "Software"),
        tx("2026-10-20", "SQUARESPACE DOMAINS", "credit", 20.00, "Software"),
        tx("2026-10-08", "CALENDLY ADD'L SEAT", "credit", 12.00, "Software"),
        tx("2026-10-21", "ZAPIER TASK OVERAGE", "credit", 19.50, "Software"),
        tx("2026-10-22", "DROPBOX*EXTRA STORAGE", "credit", 10.00, "Software"),
        tx("2026-10-29", "MIRO GUEST SEATS", "credit", 16.00, "Software"),
        tx("2026-10-16", "FOREIGN TRANSACTION FEE", "credit", 0.90, "Bank fees"),
        tx("2026-10-27", "FOREIGN TRANSACTION FEE", "credit", 1.72, "Bank fees"),
        tx("2026-10-26", "CASHBACK REWARD CREDIT", "credit", -42.18, "Income", receipt=""),
        tx("2026-10-11", "AMZN MKTP US*2K41T", "credit", 64.20, None, receipt="missing"),       # receipt image provided
        tx("2026-10-19", "AMZN MKTP US*7Q2LM", "credit", 38.99, None, receipt="missing"),
        tx("2026-10-23", "DELTA AIR 0062341788901", "credit", 412.30, None, receipt="missing"),  # receipt image provided
        tx("2026-10-24", "MARRIOTT AUSTIN DWTN", "credit", 578.44, None),
        tx("2026-10-25", "UBER *TRIP HELP.UBER.COM", "credit", 31.40, None),
        tx("2026-10-09", "APPLE.COM/BILL 866-712-7753", "credit", 1299.00, None),
        # checking: income
        tx("2026-10-03", "ACH CREDIT NORTHWIND COFFEE INV-1041", "checking", 6000.00, "Income", receipt=""),
        tx("2026-10-12", "WIRE IN HALCYON HEALTH REF 88213", "checking", 14200.00, "Income", receipt=""),
        tx("2026-10-12", "INCOMING WIRE FEE", "checking", -15.00, "Bank fees"),
        tx("2026-10-17", "STRIPE TRANSFER ST-K2L9P1", "checking", 1164.90, "Income", receipt=""),   # Maple & Co, net of fees
        tx("2026-10-21", "ACH CREDIT PINECREST RLTY", "checking", 2000.00, "Income", receipt=""),   # partial payment
        tx("2026-10-23", "ACH CREDIT BLUEBIRD BAKERY", "checking", 2400.00, "Income", receipt=""),  # no invoice on file
        # checking: contractors / unknowns
        tx("2026-10-22", "ZELLE TO M. GARCIA", "checking", -2400.00, "Contractors", receipt="missing"),  # new, no W-9
        tx("2026-10-29", "WIRE OUT L OKAFOR LAGOS NG", "checking", -600.00, "Contractors"),
        tx("2026-10-29", "WIRE TRANSFER FEE", "checking", -25.00, "Bank fees"),
        tx("2026-10-30", "UPWORK -655902 ESCROW", "checking", -430.00, "Contractors"),
        tx("2026-10-13", "UPWORK -648771 ESCROW", "checking", -680.00, "Contractors"),
        tx("2026-10-27", "ZELLE TO S. ITO", "checking", -650.00, "Contractors"),
        tx("2026-10-28", "GUSTO CONTRACTOR PMT D.KIM", "checking", -2000.00, "Contractors"),
        tx("2026-10-15", "ATM WITHDRAWAL 0442 BEDFORD AV", "checking", -200.00, None, receipt="missing"),
        tx("2026-10-15", "ATM FEE", "checking", -3.50, "Bank fees"),
        tx("2026-10-18", "VENMO *JPATEL", "checking", -350.00, None, receipt="missing"),
        tx("2026-10-27", "CHECK 1043", "checking", -1250.00, None, receipt="missing"),
        tx("2026-10-28", "TRANSFER TO SAVINGS ...4471", "checking", -5000.00, None, receipt=""),
    ]
    rows.sort(key=lambda r: (r["date"], r["account"], r["desc"]))
    return rows


def write_csv(rows: list[dict], opening_checking: float) -> None:
    bal = opening_checking
    out = []
    for r in rows:
        balance = ""
        if r["account"] == "checking":
            bal = round(bal + r["amount"], 2)
            balance = f"{bal:.2f}"
        rec = r["receipt"] if r["receipt"] else ""
        out.append([r["date"], r["desc"], "Checking" if r["account"] == "checking" else "Credit card",
                    f"{r['amount']:.2f}", balance, rec.upper() if rec else ""])
    # ---- realistic messiness ----
    for o in out:
        if o[1].startswith("STORESAFE"):
            o[0] = "10/01/2026"                  # US date format
        if o[1] == "WIRE IN HALCYON HEALTH REF 88213":
            o[3] = "$14,200.00"                  # currency symbol + thousands separator
        if o[1] == "CHECK 1043":
            o[0] = "2026-10-2 7"                 # broken date: rejected, the agent must fix and retry
        if o[1].startswith("ZOOM"):
            o[1] = "  zoom.us 888-799-9666  "    # case / whitespace
    dup = next(o for o in out if o[1] == "INCOMING WIRE FEE")
    out.insert(out.index(dup) + 1, list(dup))    # exported twice: exact duplicate row
    with open(DEMO / "brightwave_transactions_2026-10.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Description", "Account", "Amount", "Balance", "Receipt"])
        w.writerows(out)
    # Scenario: the bank export silently dropped one transaction (the cheque). Balances still come from the bank.
    scen = ROOT / "data" / "scenarios" / "missing_transaction"
    scen.mkdir(parents=True, exist_ok=True)
    with open(scen / "brightwave_transactions_2026-10.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Description", "Account", "Amount", "Balance", "Receipt"])
        w.writerows([o for o in out if o[1] != "CHECK 1043"])


def font(size: int):
    for name in ("consola.ttf", "cour.ttf", "DejaVuSansMono.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def write_receipt(path: Path, lines: list[str], tilt: float) -> None:
    W, H = 440, 40 + 26 * len(lines)
    img = Image.new("RGB", (W, H), (250, 248, 240))
    d = ImageDraw.Draw(img)
    f = font(18)
    for i, ln in enumerate(lines):
        d.text((24, 20 + i * 26), ln, fill=(30, 30, 30), font=f)
    img.rotate(tilt, expand=True, fillcolor=(205, 200, 190)).save(path)


def main() -> None:
    SEED.mkdir(parents=True, exist_ok=True)
    (DEMO / "receipts").mkdir(parents=True, exist_ok=True)
    for old in DEMO.rglob("*"):
        if old.is_file():
            old.unlink()

    sep_rows, invoices, opening = september()
    sep_checking = opening["checking"] + sum(r["amount"] for r in sep_rows if r["account"] == "checking")
    hist = {"clients": CLIENTS, "contractors": CONTRACTORS, "rules": BUILTIN_RULES, "category_accounts": CAT,
            "opening": opening, "transactions": sep_rows, "invoices": invoices}
    (SEED / "history.json").write_text(json.dumps(hist, indent=1))

    oct_rows = october()
    write_csv(oct_rows, round(sep_checking, 2))

    with open(DEMO / "brightwave_deliveries_2026-10.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "client", "project", "description", "billing", "hours", "rate", "fixed_fee", "status"])
        w.writerows([
            ["2026-10-06", "Northwind Coffee Co", "Retainer", "Seasonal menu boards", "retainer", "14", "", "", "delivered"],
            ["2026-10-08", "Halcyon Health", "Patient portal", "Post-launch fixes (warranty)", "non-billable", "6", "", "", "delivered"],
            ["2026-10-10", "Orbit Fitness", "Website rebuild", "Sprint 1: IA, design system, home + class pages", "hourly", "48", "150", "", "delivered"],
            ["2026-10-24", "Orbit Fitness", "Website rebuild", "Sprint 2: booking flow, CMS, QA", "hourly", "48", "150", "", "delivered"],
            ["2026-10-15", "Lumen Labs", "Brand identity", "Milestone 2: logo suite + brand guidelines", "fixed", "", "", "9500", "delivered"],
            ["2026-10-29", "Kestrel Outdoor", "Fall campaign", "Campaign key visuals + 12 social assets", "hourly", "22", "140", "", "delivered"],
            ["2026-10-14", "Maple & Co", "Logo refresh", "Logo refresh (paid by card via Stripe)", "fixed", "", "", "1200", "delivered"],
            ["2026-10-30", "Lumen Labs", "Brand identity", "Milestone 3: packaging (in progress)", "fixed", "", "", "6000", "in_progress"],
        ])

    write_receipt(DEMO / "receipts" / "receipt_delta_air.png", [
        "      DELTA AIR LINES", "   E-TICKET RECEIPT", "   Issued 10/23/2026", "-----------------------------",
        "Passenger: DANA REYES", "JFK -> AUS  10/24  DL 1187", "AUS -> JFK  10/26  DL 1342",
        "Base fare            356.28", "Taxes & fees          56.02", "TOTAL USD            412.30",
        "Card: MERIDIAN ****4417", "Purpose: Orbit Fitness kickoff",
    ], tilt=1.6)
    write_receipt(DEMO / "receipts" / "receipt_amazon_order.png", [
        "  amazon.com  Order summary", "  Order date: 10/11/2026", "  Order #113-4471982-55102", "-----------------------------",
        "USB-C hub (2)          41.98", "HDMI cable 2m           12.99", "Subtotal               54.97",
        "Tax                     9.23", "Order total            64.20", "Payment: card ****4417",
    ], tilt=-1.3)

    n_oct = len(oct_rows) + 1  # + the duplicate row
    print(f"September history: {len(sep_rows)} txns | October CSV: {n_oct} rows "
          f"({sum(1 for r in oct_rows if r['account'] == 'checking')} checking, {sum(1 for r in oct_rows if r['account'] == 'credit')} card) "
          f"| statement balance Sep 30: {sep_checking:,.2f}")


if __name__ == "__main__":
    main()
