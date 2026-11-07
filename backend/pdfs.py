"""PDF rendering with one shared design system for every report, the close package and invoices.

Numbers come from reports.py; this module only lays them out.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame, KeepTogether, NextPageTemplate, PageBreak,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)

from . import config

# ------------------------------------------------------------------------------------------ design tokens
INK = colors.HexColor("#101828")
INK2 = colors.HexColor("#344054")
MUTED = colors.HexColor("#667085")
LINE = colors.HexColor("#E4E7EC")
SOFT = colors.HexColor("#F9FAFB")
HEAD = colors.HexColor("#F2F4F7")
BRAND = colors.HexColor("#1F4FD8")
BRAND_SOFT = colors.HexColor("#EEF3FF")
OK = colors.HexColor("#067647")
WARN = colors.HexColor("#B54708")
BAD = colors.HexColor("#B42318")

PAGE_W, PAGE_H = letter
MARGIN = 0.75 * inch
CONTENT_W = PAGE_W - 2 * MARGIN

FONT, BOLD = "Helvetica", "Helvetica-Bold"
S = {
    "title": ParagraphStyle("title", fontName=BOLD, fontSize=20, leading=24, textColor=INK, spaceAfter=2),
    "subtitle": ParagraphStyle("subtitle", fontName=FONT, fontSize=10, leading=14, textColor=MUTED, spaceBefore=3, spaceAfter=14),
    "h2": ParagraphStyle("h2", fontName=BOLD, fontSize=12, leading=16, textColor=INK, spaceBefore=14, spaceAfter=6),
    "body": ParagraphStyle("body", fontName=FONT, fontSize=9.5, leading=14, textColor=INK2),
    "bullet": ParagraphStyle("bullet", fontName=FONT, fontSize=9.5, leading=14, textColor=INK2, leftIndent=12, bulletIndent=2),
    "small": ParagraphStyle("small", fontName=FONT, fontSize=8, leading=11, textColor=MUTED),
    "cell": ParagraphStyle("cell", fontName=FONT, fontSize=9, leading=12, textColor=INK2),
    "cellb": ParagraphStyle("cellb", fontName=BOLD, fontSize=9, leading=12, textColor=INK),
    "kpi_l": ParagraphStyle("kpi_l", fontName=FONT, fontSize=8, leading=10, textColor=MUTED),
    "kpi_v": ParagraphStyle("kpi_v", fontName=BOLD, fontSize=15, leading=19, textColor=INK),
    "kpi_d": ParagraphStyle("kpi_d", fontName=FONT, fontSize=7.5, leading=10, textColor=MUTED),
    "right": ParagraphStyle("right", fontName=FONT, fontSize=9, leading=12, textColor=INK2, alignment=TA_RIGHT),
}

REPORT_TITLES = {
    "pnl": "Profit and Loss", "balance_sheet": "Balance Sheet", "cash_flow": "Statement of Cash Flows",
    "spend_by_category": "Spending by Category", "ar_aging": "Accounts Receivable Aging", "anomalies": "Anomalies",
    "mom": "Month-over-Month Comparison", "cash_outlook": "30-Day Cash Outlook",
}


# ------------------------------------------------------------------------------------------ formatting
def acct(minor: int | None) -> str:
    """Accounting format: negatives in parentheses."""
    if minor is None:
        return ""
    m = abs(int(minor))
    s = f"${m // 100:,}.{m % 100:02d}"
    return f"({s})" if minor < 0 else s


def pct(v: float | None) -> str:
    return "–" if v is None else f"{v:+.1f}%"


def month_name(period: str) -> str:
    y, m = map(int, period.split("-"))
    return date(y, m, 1).strftime("%B %Y")


def long_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%B %d, %Y").replace(" 0", " ")


def short_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%b %d, %Y").replace(" 0", " ")


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------------------------------ page chrome
def _chrome(canvas, doc):
    canvas.saveState()
    # top brand rule + header
    canvas.setFillColor(BRAND)
    canvas.rect(0, PAGE_H - 6, PAGE_W, 6, stroke=0, fill=1)
    canvas.setFont(BOLD, 9)
    canvas.setFillColor(INK)
    canvas.drawString(MARGIN, PAGE_H - 0.48 * inch, config.COMPANY["name"])
    canvas.setFont(FONT, 8)
    canvas.setFillColor(MUTED)
    canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 0.48 * inch, getattr(doc, "header_right", ""))
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.6)
    canvas.line(MARGIN, PAGE_H - 0.58 * inch, PAGE_W - MARGIN, PAGE_H - 0.58 * inch)
    # footer
    canvas.line(MARGIN, 0.6 * inch, PAGE_W - MARGIN, 0.6 * inch)
    canvas.drawString(MARGIN, 0.42 * inch, getattr(doc, "footer_left", None) or
                      f"Prepared by Ledgerly · {datetime.now().strftime('%b %d, %Y').replace(' 0', ' ')} · Unaudited, for internal use")
    canvas.drawRightString(PAGE_W - MARGIN, 0.42 * inch, f"Page {doc.page}")
    canvas.restoreState()


def _cover_chrome(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(BRAND)
    canvas.rect(0, PAGE_H - 2.1 * inch, PAGE_W, 2.1 * inch, stroke=0, fill=1)
    canvas.setFillColor(colors.white)
    canvas.setFont(FONT, 10)
    canvas.drawString(MARGIN, PAGE_H - 0.75 * inch, config.COMPANY["name"].upper())
    canvas.setFont(BOLD, 26)
    canvas.drawString(MARGIN, PAGE_H - 1.25 * inch, doc.cover_title)
    canvas.setFont(FONT, 11)
    canvas.drawString(MARGIN, PAGE_H - 1.6 * inch, doc.cover_sub)
    canvas.setStrokeColor(LINE)
    canvas.setFillColor(MUTED)
    canvas.setFont(FONT, 8)
    canvas.line(MARGIN, 0.6 * inch, PAGE_W - MARGIN, 0.6 * inch)
    canvas.drawString(MARGIN, 0.42 * inch, "Prepared by Ledgerly · Unaudited, for internal use")
    canvas.drawRightString(PAGE_W - MARGIN, 0.42 * inch, f"Page {doc.page}")
    canvas.restoreState()


def _doc(path: Path, title: str, header_right: str, *, cover: tuple[str, str] | None = None) -> BaseDocTemplate:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = BaseDocTemplate(str(path), pagesize=letter, leftMargin=MARGIN, rightMargin=MARGIN,
                          topMargin=0.85 * inch, bottomMargin=0.8 * inch, title=title, author="Ledgerly")
    doc.header_right = header_right
    body = Frame(MARGIN, 0.8 * inch, CONTENT_W, PAGE_H - 1.65 * inch, id="body", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    templates = []
    if cover:
        doc.cover_title, doc.cover_sub = cover
        cov = Frame(MARGIN, 0.8 * inch, CONTENT_W, PAGE_H - 2.5 * inch - 0.8 * inch, id="cover", leftPadding=0, rightPadding=0,
                    topPadding=0, bottomPadding=0)
        templates.append(PageTemplate(id="cover", frames=[cov], onPage=_cover_chrome))
    templates.append(PageTemplate(id="body", frames=[body], onPage=_chrome))
    doc.addPageTemplates(templates)
    return doc


# ------------------------------------------------------------------------------------------ building blocks
def title_block(title: str, subtitle: str) -> list:
    return [Paragraph(esc(title), S["title"]), Paragraph(esc(subtitle), S["subtitle"])]


class Rows:
    """Collects table rows with a role per row, then renders one consistent financial table."""

    def __init__(self, headers: list[str], widths: list[float], align: list[str] | None = None):
        self.headers, self.widths = headers, [w * CONTENT_W for w in widths]
        self.align = align or ["L"] + ["R"] * (len(headers) - 1)
        self.data: list[list] = []
        self.roles: list[str] = []

    def add(self, cells: list, role: str = "row") -> None:
        self.data.append(cells)
        self.roles.append(role)

    def section(self, label: str) -> None:
        self.add([label] + [""] * (len(self.headers) - 1), "section")

    def table(self, *, header: bool = True, compact: bool = False) -> Table:
        rows = ([self.headers] if header else []) + [list(r) for r in self.data]
        t = Table(rows, colWidths=self.widths, repeatRows=1 if header else 0)
        pad = 3 if compact else 5
        st = [
            ("FONT", (0, 0), (-1, 0), BOLD, 8), ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
            ("BACKGROUND", (0, 0), (-1, 0), HEAD), ("LINEBELOW", (0, 0), (-1, 0), 0.6, LINE),
            ("FONT", (0, 1), (-1, -1), FONT, 9), ("TEXTCOLOR", (0, 1), (-1, -1), INK2),
            ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
            ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]
        for i, a in enumerate(self.align):
            st.append(("ALIGN", (i, 0), (i, -1), "RIGHT" if a == "R" else "LEFT"))
        if not header:
            st = [x for x in st if x[1][1] != 0 or x[0] not in ("BACKGROUND", "LINEBELOW", "TEXTCOLOR", "FONT")]
        for i, role in enumerate(self.roles, start=1 if header else 0):
            if role == "row":
                st.append(("LINEBELOW", (0, i), (-1, i), 0.25, LINE))
                st.append(("LEFTPADDING", (0, i), (0, i), 16))
            elif role == "plain":
                st.append(("LINEBELOW", (0, i), (-1, i), 0.25, LINE))
            elif role == "section":
                st += [("FONT", (0, i), (-1, i), BOLD, 9), ("TEXTCOLOR", (0, i), (-1, i), INK),
                       ("BACKGROUND", (0, i), (-1, i), SOFT), ("TOPPADDING", (0, i), (-1, i), 7)]
            elif role == "strong":
                st += [("FONT", (0, i), (-1, i), BOLD, 9), ("TEXTCOLOR", (0, i), (-1, i), INK), ("LINEBELOW", (0, i), (-1, i), 0.25, LINE)]
            elif role == "subtotal":
                st += [("FONT", (0, i), (-1, i), BOLD, 9), ("TEXTCOLOR", (0, i), (-1, i), INK),
                       ("LINEABOVE", (1, i), (-1, i), 0.6, INK2), ("BOTTOMPADDING", (0, i), (-1, i), 8)]
            elif role == "total":
                st += [("FONT", (0, i), (-1, i), BOLD, 9.5), ("TEXTCOLOR", (0, i), (-1, i), INK),
                       ("BACKGROUND", (0, i), (-1, i), BRAND_SOFT), ("LINEABOVE", (0, i), (-1, i), 0.8, INK),
                       ("LINEBELOW", (0, i), (-1, i), 1.6, INK)]
        t.setStyle(TableStyle(st))
        return t


def kpi_strip(items: list[tuple[str, str, str]]) -> Table:
    """items: (label, value, note)."""
    cells = [[Paragraph(esc(l), S["kpi_l"]), Paragraph(esc(v), S["kpi_v"]), Paragraph(n, S["kpi_d"])] for l, v, n in items]
    t = Table([cells], colWidths=[CONTENT_W / len(items)] * len(items))  # each cell: label, value, note
    t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.6, LINE),
        ("BACKGROUND", (0, 0), (-1, -1), colors.white), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9), ("LEFTPADDING", (0, 0), (-1, -1), 10),
    ]))
    return t


def delta_note(cur: int, prev: int, label: str = "vs prior month", good_up: bool = True) -> str:
    d = cur - prev
    if prev == 0 and d == 0:
        return label
    color = "#067647" if (d >= 0) == good_up else "#B42318"
    arrow = "▲" if d >= 0 else "▼"
    return f'<font color="{color}">{arrow} {acct(abs(d))}</font> {label}'


def bar_chart(items: list[tuple[str, int]], width: float = CONTENT_W, bar_h: float = 13) -> Drawing:
    label_w, value_w = 2.3 * inch, 0.95 * inch
    h = len(items) * (bar_h + 6) + 4
    d = Drawing(width, h)
    mx = max((v for _, v in items), default=1) or 1
    span = width - label_w - value_w - 8
    for i, (lab, v) in enumerate(items):
        y = h - (i + 1) * (bar_h + 6)
        d.add(String(0, y + 3, lab[:38], fontName=FONT, fontSize=8, fillColor=INK2))
        d.add(Rect(label_w, y, span, bar_h, fillColor=HEAD, strokeColor=None))
        d.add(Rect(label_w, y, max(span * v / mx, 1.5), bar_h, fillColor=BRAND, strokeColor=None))
        d.add(String(width, y + 3, acct(v), fontName=FONT, fontSize=8, fillColor=INK2, textAnchor="end"))
    return d


def callout(text: str, tone: str = "info") -> Table:
    bg, fg = {"info": (BRAND_SOFT, BRAND), "warn": (colors.HexColor("#FFFAEB"), WARN), "ok": (colors.HexColor("#ECFDF3"), OK)}[tone]
    t = Table([[Paragraph(text, ParagraphStyle("c", parent=S["body"], textColor=fg, fontSize=9))]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("LEFTPADDING", (0, 0), (-1, -1), 10),
                           ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    return t


# ------------------------------------------------------------------------------------------ report bodies
def story_for(rep: dict) -> list:
    k = rep["report"]
    s: list = []
    if k == "pnl":
        cur, prev = month_name(rep["period"]), month_name(rep["prior"]["period"])
        s += title_block(REPORT_TITLES[k], f"For the month ended {long_date(_month_end(rep['period']))} · compared with {prev}")
        r = Rows(["", cur, prev, "Change"], [.52, .17, .17, .14])
        r.section("Revenue")
        for l in rep["revenue"]:
            r.add([l["name"], acct(l["amount_minor"]), acct(l["prior_minor"]), pct(l["change_pct"])])
        r.add(["Total revenue", acct(rep["total_revenue_minor"]), acct(rep["prior"]["total_revenue_minor"]), ""], "subtotal")
        r.section("Operating expenses")
        for l in sorted(rep["expenses"], key=lambda x: x["account"]):
            r.add([l["name"], acct(l["amount_minor"]), acct(l["prior_minor"]), pct(l["change_pct"])])
        r.add(["Total operating expenses", acct(rep["total_expenses_minor"]), acct(rep["prior"]["total_expenses_minor"]), ""], "subtotal")
        if rep["suspense_pending_review_minor"]:
            r.add(["Uncategorized, awaiting review", acct(rep["suspense_pending_review_minor"]), "–", ""], "plain")
        r.add(["Net income", acct(rep["net_income_minor"]), acct(rep["prior"]["net_income_minor"]), ""], "total")
        s.append(r.table())
        if rep["suspense_pending_review_minor"]:
            s += [Spacer(1, 8), callout(f"{acct(rep['suspense_pending_review_minor'])} of spending is held in Suspense until it is "
                                        "reviewed. Net income will change once those items are categorized.", "warn")]
    elif k == "balance_sheet":
        s += title_block(REPORT_TITLES[k], f"As of {long_date(rep['as_of'])}")
        r = Rows(["", "Amount"], [.75, .25])
        for title, key, tot in (("Assets", "assets", "total_assets_minor"), ("Liabilities", "liabilities", "total_liabilities_minor"),
                                ("Equity", "equity", "total_equity_minor")):
            r.section(title)
            for l in rep[key]:
                r.add([l["name"], acct(l["amount_minor"])])
            r.add([f"Total {title.lower()}", acct(rep[tot])], "subtotal")
        r.add(["Total liabilities and equity", acct(rep["total_liabilities_minor"] + rep["total_equity_minor"])], "total")
        s.append(r.table())
        s += [Spacer(1, 8), callout("The balance sheet is in balance and the trial balance ties." if rep["balanced"] and rep["trial_balance_ok"]
                                    else "Warning: the balance sheet does not balance. Do not rely on these figures.",
                                    "ok" if rep["balanced"] else "warn")]
    elif k == "cash_flow":
        s += title_block(REPORT_TITLES[k], f"For the month of {month_name(rep['period'])} · direct method, operating checking account")
        r = Rows(["", "Amount"], [.75, .25])
        r.add(["Cash at beginning of month", acct(rep["opening_cash_minor"])], "strong")
        for sec in ("operating", "investing", "financing"):
            if sec in rep["sections"]:
                r.section(f"Cash flows from {sec} activities")
                lines = sorted(rep["sections"][sec], key=lambda l: (l["amount_minor"] < 0, -abs(l["amount_minor"])))
                for l in lines:
                    r.add([_cash_label(l["line"], l["amount_minor"]), acct(l["amount_minor"])])
                r.add([f"Net cash from {sec} activities", acct(rep["section_totals_minor"][sec])], "subtotal")
        r.add(["Net change in cash", acct(rep["net_change_minor"])], "plain")
        r.add(["Cash at end of month", acct(rep["closing_cash_minor"])], "total")
        s.append(r.table())
        if rep["reconciles"]:
            s += [Spacer(1, 8), callout("Ending cash agrees to the checking account balance in the ledger.", "ok")]
    elif k == "spend_by_category":
        s += title_block(REPORT_TITLES[k], f"For the month of {month_name(rep['period'])} · compared with the prior month")
        cats = [c for c in rep["categories"] if c["amount_minor"] > 0]
        s += [bar_chart([(c["name"], c["amount_minor"]) for c in cats]), Spacer(1, 14)]
        r = Rows(["Category", "This month", "Share", "Prior month", "Change"], [.4, .16, .1, .16, .18])
        for c in rep["categories"]:
            r.add([c["name"], acct(c["amount_minor"]), f"{c['share_pct']:.1f}%", acct(c["prior_minor"]), pct(c["change_pct"])], "plain")
        r.add(["Total", acct(rep["total_minor"]), "100%", acct(rep["prior_total_minor"]), ""], "total")
        s.append(r.table())
    elif k == "ar_aging":
        s += title_block(REPORT_TITLES[k], f"As of {long_date(rep['as_of'])}")
        b = rep["buckets_minor"]
        names = {"current": "Not yet due", "1-30": "1–30 days", "31-60": "31–60 days", "61-90": "61–90 days", "90+": "Over 90 days"}
        s += [kpi_strip([(names[k2], acct(v), "") for k2, v in b.items()]), Spacer(1, 14)]
        r = Rows(["Invoice", "Client", "Due date", "Days past due", "Balance"], [.14, .34, .18, .14, .2], ["L", "L", "L", "R", "R"])
        for i in rep["invoices"]:
            r.add([i["number"], i["client"], short_date(i["due_date"]), str(i["days_overdue"]) if i["days_overdue"] else "–",
                   acct(i["balance_minor"])], "plain")
        r.add(["Total receivables", "", "", "", acct(rep["total_minor"])], "total")
        s.append(r.table())
    elif k == "anomalies":
        s += title_block(REPORT_TITLES[k], f"Items in {month_name(rep['period'])} that look unusual and deserve a second look")
        tag = {"possible_duplicate_charge": ("Possible duplicate", BAD), "unusual_amount": ("Unusual amount", WARN),
               "new_recurring_subscription": ("New subscription", BRAND)}
        r = Rows(["Type", "What we found", "Suggested action"], [.2, .45, .35], ["L", "L", "L"])
        for a in rep["anomalies"]:
            lab, col = tag[a["type"]]
            r.add([Paragraph(f'<font color="{col.hexval().replace("0x", "#")}"><b>{lab}</b></font>', S["cell"]),
                   Paragraph(esc(a["detail"]), S["cell"]), Paragraph(esc(a["suggested_action"]), S["cell"])], "plain")
        if not rep["anomalies"]:
            r.add(["None", "Nothing unusual this month.", ""], "plain")
        s.append(r.table())
        if rep["import_duplicates_removed"]:
            s += [Spacer(1, 8), Paragraph(f"{len(rep['import_duplicates_removed'])} duplicate rows were removed automatically during import.", S["small"])]
    elif k == "mom":
        s += title_block(REPORT_TITLES[k], f"{month_name(rep['period'])} compared with {month_name(rep['prior_period'])}")
        r = Rows(["", month_name(rep["period"]), month_name(rep["prior_period"]), "Change"], [.46, .18, .18, .18])
        for lab, key in (("Revenue", "revenue_minor"), ("Operating expenses", "expenses_minor"), ("Net income", "net_income_minor")):
            a, b = rep[key]
            r.add([lab, acct(a), acct(b), acct(a - b)], "total" if key == "net_income_minor" else "plain")
        s += [r.table(), Paragraph("Largest changes by account", S["h2"])]
        r = Rows(["Account", "This month", "Prior month", "Change"], [.46, .18, .18, .18])
        for m in rep["biggest_movers"]:
            r.add([m["name"], acct(m["amount_minor"]), acct(m["prior_minor"]), acct(m["delta_minor"])], "plain")
        s.append(r.table())
    elif k == "cash_outlook":
        s += title_block(REPORT_TITLES[k], f"{long_date(rep['as_of'])} to {long_date(rep['horizon'])}")
        r = Rows(["", "Amount"], [.75, .25])
        r.add(["Cash in checking today", acct(rep["cash_minor"])], "strong")
        r.add(["Add: client invoices due in the period", acct(rep["expected_collections_minor"])], "plain")
        r.add(["Less: recurring costs (payroll, rent, utilities, insurance)", acct(-rep["recurring_outflows_minor"])], "plain")
        r.add(["Less: credit card balance due", acct(-rep["card_balance_due_minor"])], "plain")
        r.add(["Projected cash in 30 days", acct(rep["projected_cash_minor"])], "total")
        s.append(r.table())
        if rep["collections"]:
            s.append(Paragraph("Expected collections", S["h2"]))
            r = Rows(["Invoice", "Client", "Due", "Amount"], [.16, .44, .2, .2], ["L", "L", "L", "R"])
            for cl in rep["collections"]:
                r.add([cl["number"], cl["client"], short_date(cl["due"]), acct(cl["balance_minor"])], "plain")
            s.append(r.table())
        s += [Spacer(1, 8), Paragraph("Estimate only: assumes clients pay on their due dates and recurring costs repeat at this month's level.", S["small"])]
    return s


def _cash_label(line: str, amount: int) -> str:
    if line.startswith("Accounts Receivable"):
        return "Collections from clients"
    if line.startswith("Meridian Business Card"):
        return "Paid: credit card balance"
    if line.startswith("Suspense"):
        return "Paid: awaiting categorization"
    return ("Paid: " if amount < 0 else "Received: ") + line


def _month_end(period: str) -> str:
    import calendar
    y, m = map(int, period.split("-"))
    return f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


# ------------------------------------------------------------------------------------------ documents
def render_report(rep: dict, period: str) -> Path:
    path = config.OUT / "reports" / f"{rep['report']}_{period}.pdf"
    doc = _doc(path, f"{REPORT_TITLES[rep['report']]} - {month_name(period)}", f"{REPORT_TITLES[rep['report']]} · {month_name(period)}")
    doc.build(story_for(rep))
    return path


def md_to_story(md: str) -> list:
    out: list = []
    for line in md.strip().split("\n"):
        line = line.rstrip()
        if not line:
            out.append(Spacer(1, 3))
            continue
        e = esc(line)
        e = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", e)
        e = re.sub(r"`([^`]+)`", r"\1", e)
        if line.lstrip().startswith("#"):
            out.append(Paragraph(e.lstrip("# "), S["h2"]))
        elif line.lstrip().startswith(("- ", "* ")):
            out.append(Paragraph(e.lstrip()[2:], S["bullet"], bulletText="•"))
        else:
            out.append(Paragraph(e, S["body"]))
    return out


def render_close_package(period: str, summary_md: str, reps: list[dict]) -> Path:
    path = config.OUT / "reports" / f"close_package_{period}.pdf"
    by = {r["report"]: r for r in reps}
    doc = _doc(path, f"Month-end close - {month_name(period)}", f"Month-end close · {month_name(period)}",
               cover=("Month-end close", f"{month_name(period)}  ·  Financial statements and close summary"))
    story: list = [NextPageTemplate("body")]
    if "pnl" in by and "balance_sheet" in by:
        p, bs = by["pnl"], by["balance_sheet"]
        cash = next((a["amount_minor"] for a in bs["assets"] if a["account"] == "1000"), 0)
        co = by.get("cash_flow")
        story += [kpi_strip([
            ("Revenue", acct(p["total_revenue_minor"]), delta_note(p["total_revenue_minor"], p["prior"]["total_revenue_minor"])),
            ("Operating expenses", acct(p["total_expenses_minor"]), delta_note(p["total_expenses_minor"], p["prior"]["total_expenses_minor"], good_up=False)),
            ("Net income", acct(p["net_income_minor"]), delta_note(p["net_income_minor"], p["prior"]["net_income_minor"])),
            ("Cash in bank", acct(cash), delta_note(cash, co["opening_cash_minor"], "this month") if co else ""),
        ]), Spacer(1, 16)]
    story += md_to_story(summary_md)
    story += [Spacer(1, 12), Paragraph("Contents", S["h2"])]
    contents = Rows(["Included in this package"], [1.0], ["L"])
    for r in reps:
        contents.add([REPORT_TITLES[r["report"]]], "plain")
    story.append(contents.table())
    for r in reps:
        story += [PageBreak()] + story_for(r)
    doc.build(story)
    return path


def render_invoice(inv: dict) -> Path:
    path = config.OUT / "invoices" / f"{inv['number']}.pdf"
    doc = _doc(path, f"Invoice {inv['number']}", f"Invoice {inv['number']}")
    doc.footer_left = f"{config.COMPANY['name']} · {config.COMPANY['email']} · Questions about this invoice? Just reply to the email."
    head = Table([[
        [Paragraph("INVOICE", ParagraphStyle("i", parent=S["title"], fontSize=24, leading=28, textColor=BRAND)),
         Paragraph(esc(inv["number"]), ParagraphStyle("n", parent=S["subtitle"], spaceBefore=4))],
        [Paragraph(f"<b>{esc(config.COMPANY['name'])}</b>", S["right"]), Paragraph(esc(config.COMPANY["address"]), S["right"]),
         Paragraph(esc(config.COMPANY["email"]), S["right"])],
    ]], colWidths=[CONTENT_W * .5, CONTENT_W * .5])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    paid = inv["total_minor"] - inv.get("balance_minor", inv["total_minor"])
    meta = Table([[
        [Paragraph("BILL TO", S["kpi_l"]), Paragraph(f"<b>{esc(inv['client_name'])}</b>", S["body"]), Paragraph(esc(inv["email"]), S["body"])],
        [Paragraph("ISSUE DATE", S["kpi_l"]), Paragraph(short_date(inv["issue_date"]), S["body"])],
        [Paragraph("DUE DATE", S["kpi_l"]), Paragraph(short_date(inv["due_date"]), S["body"])],
        [Paragraph("AMOUNT DUE", S["kpi_l"]), Paragraph(f"<b>{acct(inv.get('balance_minor', inv['total_minor']))}</b>",
                                                      ParagraphStyle("ad", parent=S["kpi_v"], fontSize=14))],
    ]], colWidths=[CONTENT_W * .4, CONTENT_W * .2, CONTENT_W * .2, CONTENT_W * .2])
    meta.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.6, LINE),
                              ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 8),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("LEFTPADDING", (0, 0), (-1, -1), 10)]))
    r = Rows(["Description", "Qty", "Rate", "Amount"], [.58, .1, .14, .18], ["L", "R", "R", "R"])
    for l in inv["lines"]:
        r.add([Paragraph(esc(l["description"]), S["cell"]), f"{l['qty']:g}", acct(l["unit_price_minor"]), acct(l["amount_minor"])], "plain")
    if paid:
        r.add(["Payments received", "", "", acct(-paid)], "plain")
    r.add(["Total due", "", "", acct(inv.get("balance_minor", inv["total_minor"]))], "total")
    story = [head, Spacer(1, 20), meta, Spacer(1, 18), r.table(), Spacer(1, 18),
             callout("<b>Payment:</b> ACH to Harbor State Bank, account ending 8820. Please include the invoice number "
                     f"{esc(inv['number'])} with your payment. Thank you for your business!", "info")]
    doc.build(story)
    return path


# ------------------------------------------------------------------------------------------ books package (guidelines)
def _status_cell(text: str):
    color = {"Open": BAD, "OK": OK, "Missing": BAD}.get(text)
    if text.startswith("Resolved"):
        color = OK
    if color is None:
        return text
    return Paragraph(f'<font color="{color.hexval().replace("0x", "#")}"><b>{esc(text)}</b></font>', S["cell"])


def books_story(data: dict, period: str, *, status: str, notes_md: str = "") -> list:
    L, E, R, I, C, O = (data[k] for k in ("ledger", "expenses", "reconciliation", "income", "contractors", "open_items"))
    start, end = f"{period}-01", _month_end(period)
    story: list = [NextPageTemplate("body")]
    rec_label = {"matched": "Matched", "difference": "Difference", "difference_acknowledged": "Difference (reviewed)",
                 "no_statement": "No statement", "not_started": "Not started"}[R["status"]]
    story += [kpi_strip([
        ("Income", acct(L["total_income_minor"]), f"{len(I['items'])} deposits and credits"),
        ("Expenses", acct(L["total_expenses_minor"]), f"{L['count']} ledger entries"),
        ("Bank reconciliation", rec_label,
         ("difference " + acct(R["difference_minor"])) if R["difference_minor"] else "statement agrees"),
        ("Open items", str(O["open_count"]), f"{O['resolved_count']} resolved"),
    ]), Spacer(1, 16)]
    meta = Rows(["", ""], [.3, .7], ["L", "L"])
    meta.add(["Books of", config.COMPANY["name"]], "plain")
    meta.add(["Period covered", f"{long_date(start)} to {long_date(end)}"], "plain")
    meta.add(["Accounts", "Checking (Harbor State Bank) and business credit card (Meridian)"], "plain")
    meta.add(["Status", status], "plain")
    meta.add(["Prepared", datetime.now().strftime("%B %d, %Y").replace(" 0", " ") + " by Ledgerly"], "plain")
    story += [meta.table(header=False), Spacer(1, 10)]
    if notes_md:
        story += [Paragraph("Notes", S["h2"])] + md_to_story(notes_md)
    story += [Spacer(1, 8), Paragraph("Contents: Ledger · Expenses · Reconciliation · Income · Contractors · Open items", S["small"])]

    # Ledger
    story += [PageBreak()] + title_block("Ledger", f"Every transaction in {month_name(period)}, both accounts")
    r = Rows(["Date", "Description", "Account", "Category", "Type", "Amount", "Receipt"],
             [.08, .36, .11, .13, .09, .13, .10], ["L", "L", "L", "L", "L", "R", "L"])
    for e in L["entries"]:
        desc = e["description"] if len(e["description"]) <= 40 else e["description"][:39] + "…"
        cat = _status_cell("Open") if e["category"] == "Open item" else e["category"]
        rec = _status_cell(e["receipt"]) if e["receipt"] in ("Missing", "OK") else e["receipt"]
        r.add([short_date(e["date"])[:-6], desc, e["account"], cat, e["type"], acct(e["amount_minor"]), rec], "plain")
    r.add(["", "Total income", "", "", "", acct(L["total_income_minor"]), ""], "subtotal")
    r.add(["", "Total expenses", "", "", "", acct(L["total_expenses_minor"]), ""], "total")
    t = r.table(compact=True)
    t.setStyle(TableStyle([("FONT", (0, 1), (-1, -1), FONT, 8), ("LEFTPADDING", (0, 1), (0, -1), 7)]))
    story.append(t)

    # Expenses
    story += [PageBreak()] + title_block("Expenses", f"{month_name(period)} by category, compared with {month_name(E['prior_period'])}")
    story += [bar_chart([(x["category"], x["amount_minor"]) for x in E["categories"] if x["amount_minor"]]), Spacer(1, 12)]
    r = Rows(["Category", month_name(period), month_name(E["prior_period"])], [.5, .25, .25])
    for x in E["categories"]:
        label = x["category"] + (" (awaiting review)" if x["category"] == "Open item" else "")
        r.add([label, acct(x["amount_minor"]), acct(x["prior_minor"])], "plain")
    r.add(["Total", acct(E["total_minor"]), acct(E["prior_total_minor"])], "total")
    story.append(r.table())

    # Reconciliation
    story += [PageBreak()] + title_block("Reconciliation", f"{R['account']} · {month_name(period)}")
    prev_day = short_date((date.fromisoformat(start) - timedelta(days=1)).isoformat())
    r = Rows(["", "Amount"], [.72, .28])
    r.add([f"Money we already have (books, {prev_day})", acct(R["money_we_had_minor"])], "strong")
    r.add(["Add: income received", acct(R["income_minor"])], "plain")
    r.add(["Less: money spent", acct(-R["spend_minor"])], "plain")
    r.add(["Theoretical money", acct(R["theoretical_minor"])], "subtotal")
    stmt = acct(R["statement_balance_minor"]) if R["statement_balance_minor"] is not None else "not provided"
    r.add(["Balance per bank statement", stmt], "strong")
    r.add(["Difference", acct(R["difference_minor"] or 0)], "total")
    story += [r.table(), Spacer(1, 10)]
    if R["status"] == "matched":
        story.append(callout("<b>Reconciled.</b> Theoretical money equals the bank statement balance.", "ok"))
    elif R["status"] == "no_statement":
        story.append(callout("<b>Not reconciled.</b> The transactions file has no bank balance column. Flagged for review.", "warn"))
    else:
        txt = f"<b>Does not match.</b> Difference of {acct(R['difference_minor'])}. Flagged for human review."
        if R["acknowledgement"]:
            txt += f" Reviewer note: {esc(R['acknowledgement'])}"
        if R["files_with_unimported_rows"]:
            txt += " Some rows in the file could not be imported, which can cause this."
        story.append(callout(txt, "warn"))
    if not R["opening_agrees"]:
        story += [Spacer(1, 6), callout(f"Opening balance per statement ({acct(R['statement_opening_minor'])}) differs from the "
                                        f"books ({acct(R['money_we_had_minor'])}).", "warn")]
    cc = R["credit_card"]
    story.append(Paragraph("Credit card activity (for reference)", S["h2"]))
    r = Rows(["", "Amount"], [.72, .28])
    r.add(["Charges (expenses)", acct(cc["charges_minor"])], "plain")
    r.add(["Credits (income)", acct(cc["credits_minor"])], "plain")
    story.append(r.table())

    # Income
    story += [PageBreak()] + title_block("Income", f"Money received in {month_name(period)}")
    r = Rows(["From", "Date", "Amount", "Document"], [.36, .16, .18, .3], ["L", "L", "R", "L"])
    for x in I["items"]:
        doc_cell = _status_cell("Missing") if x["document"] == "Missing" else Paragraph(esc(x["document"]), S["cell"])
        r.add([Paragraph(esc(x["from"]), S["cell"]), short_date(x["date"]), acct(x["amount_minor"]), doc_cell], "plain")
    r.add(["Total income", "", acct(I["total_minor"]), ""], "total")
    story.append(r.table())

    # Contractors
    story += [PageBreak()] + title_block("Contractors", f"Payments to contractors · year to date through {long_date(end)}")
    r = Rows(["Contractor", "Total (year to date)", "This month", "Form"], [.26, .19, .16, .39], ["L", "R", "R", "L"])
    for x in C["contractors"]:
        form = x["form"] + (" · 1099-NEC required" if x["1099_required"] else "")
        form_cell = _status_cell("Missing") if not x["form_ok"] else Paragraph(esc(form), S["cell"])
        r.add([x["contractor"], acct(x["total_minor"]), acct(x["this_month_minor"]), form_cell], "plain")
    r.add(["Total paid this month", "", acct(C["total_this_month_minor"]), ""], "total")
    story += [r.table(), Spacer(1, 6), Paragraph(
        f"1099-NEC is required for US contractors paid {acct(C['threshold_1099_minor'])} or more in the year "
        "(2026 threshold). Payments through platforms such as Upwork are reported by the platform.", S["small"])]

    # Open items
    story += [PageBreak()] + title_block("Open items", f"{O['open_count']} open · {O['resolved_count']} resolved")
    r = Rows(["Item", "Amount", "Reason", "Status"], [.3, .13, .35, .22], ["L", "R", "L", "L"])
    for x in O["items"]:
        r.add([Paragraph(esc(x["item"][:60]), S["cell"]), acct(x["amount_minor"]), Paragraph(esc(x["reason"]), S["cell"]),
               _status_cell(x["status"])], "plain")
    if not O["items"]:
        r.add(["None", "", "Nothing needs review.", ""], "plain")
    story.append(r.table())
    return story


def render_books_package(period: str, data: dict, *, status: str, notes_md: str = "") -> Path:
    path = config.OUT / "reports" / f"books_{period}.pdf"
    doc = _doc(path, f"Books - {month_name(period)}", f"Books · {month_name(period)}",
               cover=("Monthly books", f"{config.COMPANY['name']}  ·  {month_name(period)}"))
    doc.build(books_story(data, period, status=status, notes_md=notes_md))
    return path
