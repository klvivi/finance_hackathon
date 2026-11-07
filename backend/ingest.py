"""Parse uploads into one Transaction shape, dedupe overlaps, link card-payment transfers.

Deterministic. Rows that cannot be parsed are returned as errors (never guessed); the agent can
call ingest_file again with `fixes` for those rows.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pdfplumber

from . import config
from .db import audit, now, tx
from .money import parse_amount

STOP = {"ACH", "CREDIT", "DEBIT", "INC", "COM", "WWW", "REF", "INV", "US", "NYC", "LLC", "THE", "CO",
        "HELP", "PAYGO", "MONTHLY", "SUBSCR", "TO", "IN", "WIRE", "TST", "SQ", "PAYPAL", "AND", "OF", "NY",
        "STORE", "BILL", "PAYMENT", "ONLINE", "POS", "CK", "INTELL", "DWTN", "BKLYN", "WBURG", "BROOKLYN"}


def norm_tokens(text: str) -> list[str]:
    t = re.sub(r"[^A-Za-z ]+", " ", text.upper())
    return [w for w in t.split() if len(w) > 1 and w not in STOP]


def norm_key(text: str) -> str:
    return " ".join(norm_tokens(text))


def counterparty_from(desc: str) -> str:
    d = re.sub(r"^(TST\*|SQ \*|PAYPAL \*|ACH CREDIT|WIRE IN|ZELLE TO|VENMO \*)\s*", "", desc.strip().upper())
    toks = [w for w in re.sub(r"[^A-Za-z&'. ]+", " ", d).split() if w not in {"INC", "INC.", "COM"}]
    name = " ".join(toks[:3]).strip(" .") or desc.strip()
    return name.title()


def parse_date(s: str, default_year: int | None = None) -> str:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d %b %Y", "%b %d %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    if default_year and re.fullmatch(r"\d{1,2}/\d{1,2}", s):
        m, d = s.split("/")
        return date(default_year, int(m), int(d)).isoformat()
    raise ValueError(f"unrecognized date {s!r}")


def fingerprint(d: str, amount_minor: int, desc: str) -> str:
    return f"{d}|{amount_minor}|{norm_key(desc)}"


def detect_kind(name: str, head: bytes) -> str:
    n = name.lower()
    if n.endswith(".pdf"):
        return "card_statement_pdf"
    if n.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        return "receipt_image"
    if n.endswith(".csv"):
        first = head.decode("utf-8", "replace").splitlines()[0].lower() if head else ""
        if "billing" in first and "client" in first:
            return "delivery_log_csv"
        if "account" in first and "amount" in first:
            return "transactions_csv"
        return "bank_csv"
    return "unknown"


# ----------------------------------------------------------------------------- parsers
def parse_bank_csv(raw: str, fname: str) -> tuple[list[dict], list[dict]]:
    rows, errors = [], []
    reader = list(csv.reader(io.StringIO(raw)))
    header = [h.strip().lower() for h in reader[0]]
    idx = {k: next((i for i, h in enumerate(header) if k in h), None) for k in ("date", "desc", "amount", "ref")}
    if idx["date"] is None or idx["amount"] is None:
        return [], [{"row": 0, "raw": ",".join(reader[0]), "reason": "header lacks a date or amount column"}]
    for n, r in enumerate(reader[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        if len(r) > len(header):  # unquoted comma inside description: merge the overflow back
            extra = len(r) - len(header)
            di = idx["desc"]
            r = r[:di] + [",".join(r[di:di + extra + 1])] + r[di + extra + 1:]
        try:
            d = parse_date(r[idx["date"]])
            a = parse_amount(r[idx["amount"]])
            desc = " ".join(r[idx["desc"]].split()).upper()
            if not desc:
                raise ValueError("empty description")
        except (ValueError, IndexError) as e:
            errors.append({"row": n, "raw": ",".join(r), "reason": str(e)})
            continue
        ref = r[idx["ref"]].strip() if idx["ref"] is not None and idx["ref"] < len(r) else ""
        rows.append({"row": n, "date": d, "amount_minor": a, "description": desc, "ref": ref})
    return rows, errors


ACCOUNT_FUNDING = {"checking": "1000", "credit card": "2000", "credit": "2000", "card": "2000"}


def parse_transactions_csv(raw: str, fixes: list[dict] | None = None) -> tuple[list[dict], list[dict], dict]:
    """One export with checking + credit card rows: Date, Description, Account, Amount, Balance, Receipt.

    Sign convention as printed: checking +in/-out; credit card +charge/-credit. Internally every amount is stored
    from the funding account's view (+ money in, - money out), so card amounts are negated.
    Returns rows, errors and the bank statement balances found in the Balance column.
    """
    fx = {str(f.get("row")): f for f in (fixes or [])}
    reader = list(csv.reader(io.StringIO(raw)))
    header = [h.strip().lower() for h in reader[0]]
    col = {k: next((i for i, h in enumerate(header) if h.startswith(k)), None)
           for k in ("date", "description", "account", "amount", "balance", "receipt")}
    if None in (col["date"], col["description"], col["account"], col["amount"]):
        return [], [{"row": 1, "raw": ",".join(reader[0]), "reason": "expected columns Date, Description, Account, Amount"}], {}
    rows, errors = [], []
    bal_rows: list[tuple[int, int]] = []  # (balance, printed amount) for checking rows, in file order
    for n, r in enumerate(reader[1:], start=2):
        if not any(c.strip() for c in r):
            continue

        def cell(k: str) -> str:
            return r[col[k]].strip() if col[k] is not None and col[k] < len(r) else ""

        f = fx.get(str(n), {})
        funding = ACCOUNT_FUNDING.get(cell("account").lower())
        try:
            printed = parse_amount(f.get("amount") or cell("amount"))
        except ValueError:
            printed = None
        if funding == "1000" and cell("balance") and printed is not None:
            try:
                bal_rows.append((parse_amount(cell("balance")), printed))
            except ValueError:
                pass
        try:
            if funding is None:
                raise ValueError(f"unknown account {cell('account')!r} (expected Checking or Credit card)")
            d = parse_date(f.get("date") or cell("date"))
            if printed is None:
                raise ValueError(f"not an amount: {cell('amount')!r}")
            desc = " ".join((f.get("description") or cell("description")).split()).upper()
            if not desc:
                raise ValueError("empty description")
        except ValueError as e:
            errors.append({"row": n, "raw": ",".join(r), "reason": str(e)})
            continue
        amount = printed if funding == "1000" else -printed
        rec = cell("receipt").lower()
        receipt = "ok" if rec in ("ok", "yes", "y") else "missing" if rec in ("missing", "no", "n") else "n/a"
        rows.append({"row": n, "date": d, "amount_minor": amount, "description": desc, "funding": funding,
                     "receipt": receipt, "ref": cell("balance")})
    stmt = {}
    if bal_rows:
        stmt = {"opening_minor": bal_rows[0][0] - bal_rows[0][1], "closing_minor": bal_rows[-1][0]}
    return rows, errors, stmt


LINE_RE = re.compile(r"^(\d{2}/\d{2})\s+(.+?)\s+(-?\$[\d,]+\.\d{2})$")


def parse_card_pdf(path: Path) -> tuple[list[dict], list[dict], dict]:
    rows, errors, meta = [], [], {}
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            for ln_no, line in enumerate(text.splitlines(), start=1):
                line = line.strip()
                m = re.search(r"Statement period:\s*(\d{2}/\d{2}/\d{4})", line)
                if m:
                    meta["year"] = int(m.group(1)[-4:])
                    meta["period_line"] = line
                mm = LINE_RE.match(line)
                if not mm:
                    if re.match(r"^\d{2}/\d{2}\s", line):
                        errors.append({"row": f"p{pno}:{ln_no}", "raw": line, "reason": "could not split date/description/amount"})
                    continue
                try:
                    d = parse_date(mm.group(1), meta.get("year", 2026))
                    charge = parse_amount(mm.group(3))
                except ValueError as e:
                    errors.append({"row": f"p{pno}:{ln_no}", "raw": line, "reason": str(e)})
                    continue
                # Statement shows charges positive; from the card account's view a charge is money out.
                rows.append({"row": f"p{pno}:{ln_no}", "date": d, "amount_minor": -charge,
                             "description": " ".join(mm.group(2).split()).upper(), "ref": ""})
    return rows, errors, meta


def parse_deliveries(raw: str) -> tuple[list[dict], list[dict]]:
    out, errors = [], []
    for n, r in enumerate(csv.DictReader(io.StringIO(raw)), start=2):
        try:
            hours = float(r["hours"]) if r.get("hours") else 0.0
            rate = parse_amount(r["rate"]) if r.get("rate") else 0
            fee = parse_amount(r["fixed_fee"]) if r.get("fixed_fee") else 0
            billing = r["billing"].strip().lower()
            amount = fee if billing == "fixed" else int(round(hours * rate)) if billing == "hourly" else 0
            out.append({"row": n, "date": parse_date(r["date"]), "client": r["client"].strip(),
                        "project": r["project"].strip(), "description": r["description"].strip(),
                        "billing": billing, "hours": hours, "rate_minor": rate, "fixed_fee_minor": fee,
                        "amount_minor": amount, "status": r.get("status", "delivered").strip()})
        except (ValueError, KeyError) as e:
            errors.append({"row": n, "raw": str(r), "reason": str(e)})
    return out, errors


# ----------------------------------------------------------------------------- ingest
def _insert_txn(c, *, d, a, desc, source, ref, funding, cp=None, status="new", receipt=None, receipt_status=None):
    if receipt_status is None:
        receipt_status = "ok" if receipt else "n/a"
    c.execute(
        "INSERT INTO transactions (date, amount_minor, currency, counterparty, description, source, source_ref,"
        " funding_account, fingerprint, status, receipt_file, receipt_status, period, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (d, a, "USD", cp or counterparty_from(desc), desc, source, ref, funding,
         fingerprint(d, a, desc), status, receipt, receipt_status, d[:7], now()),
    )
    return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def _store_rows(c, rows: list[dict], *, fname: str, source: str, funding: str, within_file_dupes: bool):
    new, dupes = [], []
    seen_in_file: set[tuple] = set()
    for r in rows:
        ref = f"{fname}:{r['row']}"
        if c.execute("SELECT 1 FROM transactions WHERE source_ref=?", (ref,)).fetchone():
            continue  # already imported on a previous call (idempotent re-ingest)
        fp = fingerprint(r["date"], r["amount_minor"], r["description"])
        key = (fp, r.get("ref", ""))
        if within_file_dupes and key in seen_in_file:
            tid = _insert_txn(c, d=r["date"], a=r["amount_minor"], desc=r["description"], source=source,
                              ref=ref, funding=funding, status="duplicate")
            dupes.append({"txn_id": tid, "reason": "identical row (same bank reference) exported twice",
                          "row": r["row"], "description": r["description"]})
            continue
        seen_in_file.add(key)
        prior = c.execute(
            "SELECT id, source, period FROM transactions WHERE fingerprint=? AND funding_account=? AND status!='duplicate'"
            " AND source_ref NOT LIKE ?", (fp, funding, f"{fname}:%")).fetchone()
        if prior:
            tid = _insert_txn(c, d=r["date"], a=r["amount_minor"], desc=r["description"], source=source,
                              ref=ref, funding=funding, status="duplicate")
            c.execute("UPDATE transactions SET linked_txn_id=? WHERE id=?", (prior["id"], tid))
            dupes.append({"txn_id": tid, "reason": f"overlaps transaction #{prior['id']} already in the {prior['period']} books",
                          "row": r["row"], "description": r["description"]})
            continue
        tid = _insert_txn(c, d=r["date"], a=r["amount_minor"], desc=r["description"], source=source,
                          ref=ref, funding=funding)
        new.append(tid)
    return new, dupes


def _store_mixed(c, rows: list[dict], *, fname: str):
    """Store rows from a combined checking + card export. A checking row repeated with the same running balance
    is a re-export, not a second payment."""
    new, dupes = [], []
    seen: set[tuple] = set()
    for r in rows:
        ref = f"{fname}:{r['row']}"
        if c.execute("SELECT 1 FROM transactions WHERE source_ref=?", (ref,)).fetchone():
            continue
        fp = fingerprint(r["date"], r["amount_minor"], r["description"])
        key = (fp, r["funding"], r["ref"])
        source = "bank_csv" if r["funding"] == "1000" else "card_csv"
        if key in seen and r["funding"] == "1000" and r["ref"]:
            tid = _insert_txn(c, d=r["date"], a=r["amount_minor"], desc=r["description"], source=source, ref=ref,
                              funding=r["funding"], status="duplicate", receipt_status="n/a")
            dupes.append({"txn_id": tid, "row": r["row"], "description": r["description"],
                          "reason": "identical row with the same running balance: exported twice"})
            continue
        seen.add(key)
        prior = c.execute("SELECT id, period FROM transactions WHERE fingerprint=? AND funding_account=? AND status!='duplicate'"
                          " AND source_ref NOT LIKE ?", (fp, r["funding"], f"{fname}:%")).fetchone()
        if prior:
            tid = _insert_txn(c, d=r["date"], a=r["amount_minor"], desc=r["description"], source=source, ref=ref,
                              funding=r["funding"], status="duplicate", receipt_status="n/a")
            dupes.append({"txn_id": tid, "row": r["row"], "description": r["description"],
                          "reason": f"already in the {prior['period']} books as #{prior['id']}"})
            continue
        new.append(_insert_txn(c, d=r["date"], a=r["amount_minor"], desc=r["description"], source=source, ref=ref,
                               funding=r["funding"], receipt_status=r["receipt"]))
    return new, dupes


def _link_card_payments(c) -> list[dict]:
    """Checking-side card payment and card-side 'payment received' are one transfer: post it once."""
    linked = []
    card_side = c.execute(
        "SELECT * FROM transactions WHERE funding_account='2000' AND amount_minor>0 AND status='new'"
        " AND description LIKE 'PAYMENT RECEIVED%'").fetchall()
    for cs in card_side:
        d0 = date.fromisoformat(cs["date"])
        for bs in c.execute(
                "SELECT * FROM transactions WHERE funding_account='1000' AND amount_minor=? AND status!='duplicate'"
                " AND description LIKE '%CARD PAYMENT%'", (-cs["amount_minor"],)).fetchall():
            if abs((date.fromisoformat(bs["date"]) - d0).days) <= 5:
                c.execute("UPDATE transactions SET status='linked', linked_txn_id=?, category='2000',"
                          " cat_method='transfer', cat_reason=? WHERE id=?",
                          (bs["id"], f"Same transfer as checking txn #{bs['id']}; posted once from the checking side", cs["id"]))
                linked.append({"card_txn_id": cs["id"], "bank_txn_id": bs["id"], "amount_minor": cs["amount_minor"]})
                break
    return linked


def absorb_receipt_txns(c) -> list[dict]:
    """A receipt imported before its statement became its own transaction; fold it into the statement line."""
    merged = []
    for r in c.execute("SELECT * FROM transactions WHERE source='receipt' AND status='new' AND funding_account!='3000'").fetchall():
        d0 = date.fromisoformat(r["date"])
        st = c.execute(
            "SELECT * FROM transactions WHERE source IN ('bank_csv','card_pdf') AND amount_minor=? AND receipt_file IS NULL"
            " AND status NOT IN ('duplicate','linked') AND date BETWEEN ? AND ? ORDER BY ABS(julianday(date)-julianday(?)) LIMIT 1",
            (r["amount_minor"], (d0 - timedelta(days=4)).isoformat(), (d0 + timedelta(days=4)).isoformat(), r["date"])).fetchone()
        if st:
            c.execute("UPDATE transactions SET receipt_file=? WHERE id=?", (r["receipt_file"], st["id"]))
            c.execute("UPDATE transactions SET status='duplicate', linked_txn_id=?, receipt_file=NULL, cat_reason=? WHERE id=?",
                      (st["id"], f"Receipt for statement line #{st['id']}", r["id"]))
            merged.append({"receipt_txn": r["id"], "statement_txn": st["id"]})
    return merged


def ingest_file(name: str, *, fixes: list[dict] | None = None, receipt: dict | None = None,
                actor: str = "agent") -> dict:
    path = config.INBOX / Path(name).name
    if not path.exists():
        avail = sorted(p.name for p in config.INBOX.iterdir() if p.is_file())
        return {"error": f"file {name!r} not found in inbox", "available_files": avail}
    head = path.read_bytes()[:2048]
    kind = detect_kind(path.name, head)
    result: dict = {"file": path.name, "kind": kind}

    with tx() as c:
        if kind == "transactions_csv":
            rows, errors, stmt = parse_transactions_csv(path.read_text(encoding="utf-8-sig"), fixes)
            new, dupes = _store_mixed(c, rows, fname=path.name)
            if stmt:
                period = max((r["date"][:7] for r in rows), default="")
                c.execute("INSERT OR REPLACE INTO statements (file, account, period, opening_minor, closing_minor)"
                          " VALUES (?,?,?,?,?)", (path.name, "1000", period, stmt["opening_minor"], stmt["closing_minor"]))
                result["bank_statement"] = {"opening_balance_minor": stmt["opening_minor"],
                                            "closing_balance_minor": stmt["closing_minor"]}
            result["by_account"] = {"checking": sum(1 for r in rows if r["funding"] == "1000"),
                                    "credit_card": sum(1 for r in rows if r["funding"] == "2000")}
            result["receipts_missing"] = sum(1 for r in rows if r["receipt"] == "missing")
        elif kind == "bank_csv":
            rows, errors = parse_bank_csv(path.read_text(encoding="utf-8-sig"), path.name)
            rows, errors = _apply_fixes(rows, errors, fixes)
            new, dupes = _store_rows(c, rows, fname=path.name, source="bank_csv", funding="1000", within_file_dupes=True)
        elif kind == "card_statement_pdf":
            rows, errors, meta = parse_card_pdf(path)
            rows, errors = _apply_fixes(rows, errors, fixes)
            result["statement"] = meta.get("period_line")
            # A statement is authoritative: two identical lines are two real charges (flagged later as anomalies).
            new, dupes = _store_rows(c, rows, fname=path.name, source="card_pdf", funding="2000", within_file_dupes=False)
        elif kind == "delivery_log_csv":
            dels, errors = parse_deliveries(path.read_text(encoding="utf-8-sig"))
            clients = {r["name"].lower(): r["id"] for r in c.execute("SELECT id, name FROM clients")}
            added, unknown = 0, []
            for d in dels:
                ref = f"{path.name}:{d['row']}"
                if c.execute("SELECT 1 FROM deliveries WHERE source_ref=?", (ref,)).fetchone():
                    continue
                cid = clients.get(d["client"].lower())
                if not cid:
                    unknown.append(d["client"])
                    continue
                if d["status"] != "delivered":
                    continue
                c.execute("INSERT INTO deliveries (date, client_id, project, description, billing, hours, rate_minor,"
                          " fixed_fee_minor, amount_minor, source_ref, period) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (d["date"], cid, d["project"], d["description"], d["billing"], d["hours"], d["rate_minor"],
                           d["fixed_fee_minor"], d["amount_minor"], ref, d["date"][:7]))
                added += 1
            skipped = sum(1 for d in dels if d["status"] != "delivered")
            result.update({"deliveries_added": added, "not_yet_delivered_skipped": skipped,
                           "unknown_clients": unknown, "errors": errors})
            c.execute("UPDATE files SET status='ingested', ingested_at=? WHERE name=?", (now(), path.name))
            audit(c, actor, "ingest_file", {"file": path.name, "kind": kind, "deliveries": added})
            return result
        elif kind == "receipt_image":
            out = _ingest_receipt(c, path.name, receipt, actor)
            if "error" not in out:
                c.execute("UPDATE files SET status='ingested', ingested_at=? WHERE name=?", (now(), path.name))
            return out
        else:
            return {**result, "error": "unsupported file type"}

        linked = _link_card_payments(c)
        result["receipts_attached"] = absorb_receipt_txns(c)
        c.execute("UPDATE files SET status=?, ingested_at=? WHERE name=?",
                  ("ingested_with_errors" if errors else "ingested", now(), path.name))
        audit(c, actor, "ingest_file", {"file": path.name, "new": len(new), "dupes": len(dupes), "errors": len(errors)})

    result.update({
        "rows_parsed": len(rows), "new_transactions": len(new), "duplicates_skipped": dupes,
        "transfers_linked": linked, "errors": errors,
    })
    if errors:
        result["next_step"] = ("Some rows could not be parsed and were NOT imported. Inspect `raw`, then call "
                               "ingest_file again with the same file and fixes=[{row, date?, amount?, description?}] "
                               "for each row. Only fix what the raw text clearly shows; never guess amounts.")
    return result


def _apply_fixes(rows, errors, fixes):
    if not fixes:
        return rows, errors
    by_row = {str(f.get("row")): f for f in fixes}
    still = []
    for e in errors:
        f = by_row.get(str(e["row"]))
        if not f:
            still.append(e)
            continue
        try:
            raw_cells = next(csv.reader([e["raw"]])) if "," in e["raw"] else []
            d = parse_date(f["date"]) if f.get("date") else parse_date(raw_cells[0])
            a = parse_amount(f["amount"]) if f.get("amount") not in (None, "") else parse_amount(raw_cells[2])
            desc = (f.get("description") or (raw_cells[1] if len(raw_cells) > 1 else "")).strip().upper()
            if not desc:
                raise ValueError("description required")
            ref = raw_cells[4] if len(raw_cells) > 4 else ""
            rows.append({"row": e["row"], "date": d, "amount_minor": a, "description": " ".join(desc.split()), "ref": ref})
        except (ValueError, IndexError) as ex:
            still.append({**e, "reason": f"fix rejected: {ex}"})
    return rows, still


def _ingest_receipt(c, fname: str, receipt: dict | None, actor: str) -> dict:
    if not receipt or not receipt.get("total") or not receipt.get("date") or not receipt.get("merchant"):
        return {"file": fname, "kind": "receipt_image",
                "error": "receipt fields missing. Read the image with the `image` tool (path /workspace/inbox/"
                         f"{fname}), then call ingest_file again with receipt={{merchant, date (YYYY-MM-DD), total, "
                         "payment_method (card|debit|cash), note}}."}
    if c.execute("SELECT 1 FROM transactions WHERE receipt_file=?", (fname,)).fetchone():
        return {"file": fname, "kind": "receipt_image", "status": "already ingested"}
    waiting = [r["name"] for r in c.execute(
        "SELECT name FROM files WHERE kind IN ('bank_csv','card_statement_pdf','transactions_csv') AND status='uploaded'")]
    if waiting:
        return {"file": fname, "kind": "receipt_image", "retry_later": True,
                "error": f"Import the statements first ({', '.join(waiting)}) so this receipt can be matched to its charge; "
                         "then call ingest_file for this receipt again with the same fields."}
    try:
        d = parse_date(str(receipt["date"]))
        total = parse_amount(str(receipt["total"]))
    except ValueError as e:
        return {"file": fname, "kind": "receipt_image", "error": str(e)}
    merchant_tokens = set(norm_tokens(receipt["merchant"]))
    d0 = date.fromisoformat(d)
    cands = c.execute(
        "SELECT * FROM transactions WHERE amount_minor=? AND receipt_file IS NULL AND status NOT IN ('duplicate','linked')"
        " AND date BETWEEN ? AND ?",
        (-total, (d0 - timedelta(days=4)).isoformat(), (d0 + timedelta(days=4)).isoformat())).fetchall()
    best = None
    for t in cands:
        # prefer the transaction still waiting for a receipt, then the best merchant-name overlap
        score = (t["receipt_status"] == "missing", len(merchant_tokens & set(norm_tokens(t["description"]))))
        if best is None or score > best[0]:
            best = (score, t)
    note = (receipt.get("note") or "").strip()
    if best:
        t = best[1]
        was = t["receipt_status"]
        c.execute("UPDATE transactions SET receipt_file=?, receipt_status='ok' WHERE id=?", (fname, t["id"]))
        audit(c, actor, "ingest_receipt", {"file": fname, "txn": t["id"], "note": note})
        return {"file": fname, "kind": "receipt_image", "matched_existing_txn": t["id"], "receipt_status": "ok",
                "was": was, "detail": f"receipt attached to #{t['id']} ({t['description'][:40]}); no new transaction created"}
    method = (receipt.get("payment_method") or "").lower()
    if method not in ("cash", "personal"):
        return {"file": fname, "kind": "receipt_image", "matched_existing_txn": None,
                "note": "No transaction in the statements matches this receipt (amount and date). It was not added; "
                        "mention it in the open items so a human can check it."}
    funding = "3000"
    desc = f"{receipt['merchant'].upper()} (RECEIPT{' - ' + note.upper() if note else ''})"
    tid = _insert_txn(c, d=d, a=-total, desc=desc, source="receipt", ref=f"{fname}:receipt",
                      funding=funding, cp=receipt["merchant"].title(), receipt=fname)
    audit(c, actor, "ingest_receipt", {"file": fname, "new_txn": tid, "funding": funding})
    return {"file": fname, "kind": "receipt_image", "new_transaction": tid,
            "funding_account": funding,
            "note": "no bank/card line matches; recorded as paid personally by the owner (credit 3000)" if funding == "3000"
            else "no matching statement line found; recorded against the card"}


def register_upload(name: str, data: bytes) -> dict:
    import hashlib
    config.ensure_dirs()
    safe = Path(name).name
    (config.INBOX / safe).write_bytes(data)
    kind = detect_kind(safe, data[:2048])
    with tx() as c:
        c.execute("INSERT INTO files (name, kind, sha256, status) VALUES (?,?,?, 'uploaded')"
                  " ON CONFLICT(name) DO UPDATE SET sha256=excluded.sha256, kind=excluded.kind, status='uploaded'",
                  (safe, kind, hashlib.sha256(data).hexdigest()))
    return {"name": safe, "kind": kind, "size": len(data)}
