"""Ledgerly backend: one FastAPI process serving the UI, the ledger API, the Outbox MCP server and webhooks.

    .venv/Scripts/python -m uvicorn backend.app:app --port 8000
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from zoowork import WebhookSignatureError, unwrap_webhook

from . import books, categories as cats, coa, config, db, invoicing, reports, reset as reset_mod, tools
from .ingest import register_upload
from .journal import LedgerError
from .money import fmt
from .outbox_mcp import asgi_app as mcp_asgi, server as mcp_server
from .runtime import runtime

logging.basicConfig(level=logging.INFO)
config.ensure_dirs()
if not config.DB_PATH.exists():
    db.conn()
reset_mod.ensure_reference_clients()

mcp_app = mcp_asgi()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp_server.session_manager.run():
        await runtime.start()
        yield
        await runtime.stop()


app = FastAPI(title="Ledgerly", lifespan=lifespan)
PERIOD = "2026-10"


def err(e: Exception, code: int = 400):
    raise HTTPException(code, str(e))


# ------------------------------------------------------------------------------------------- status
@app.get("/api/status")
async def status():
    ok, why = runtime.ready()
    st = runtime.state.data
    from .runtime import STEPS
    cur = st.get("current_session")
    last = st.get("sessions", {}).get(cur, {}).get("progress") if cur else None
    return {"ready": ok, "message": why, "agent_id": st.get("agent_id"), "model": st.get("model"),
            "mcp": bool(config.PUBLIC_BASE_URL), "public_url": config.PUBLIC_BASE_URL or None,
            "current_session": cur, "runs": st.get("runs", []), "active_run": runtime.active_run(),
            "last_progress": last, "steps": STEPS, "pending_teach": len(st.get("pending_teach") or []),
            "webhook": bool(st.get("webhook_id"))}


# -------------------------------------------------------------------------------------------- files
@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    out = []
    for f in files:
        out.append(register_upload(f.filename or "upload", await f.read()))
    runtime.hub.publish("system", text="Uploaded: " + ", ".join(o["name"] for o in out))
    return {"files": out}


@app.post("/api/demo-files")
async def demo_files(scenario: str | None = None):
    out = [register_upload(p.name, p.read_bytes()) for p in sorted(config.DEMO_DIR.rglob("*")) if p.is_file()]
    if scenario:
        sdir = config.DATA / "scenarios" / Path(scenario).name
        if not sdir.is_dir():
            err(FileNotFoundError(scenario), 404)
        out += [register_upload(p.name, p.read_bytes()) for p in sorted(sdir.rglob("*")) if p.is_file()]
    runtime.hub.publish("system", text=f"Loaded demo files{' (' + scenario + ')' if scenario else ''}: synthetic data for a fictional agency")
    return {"files": out}


@app.get("/api/files")
async def files():
    return db.rows(db.q("SELECT name, kind, status, workspace_path FROM files ORDER BY name"))


# -------------------------------------------------------------------------------------------- close
class CloseReq(BaseModel):
    period: str = PERIOD
    message: str = "Close October"
    demo_assist: bool = False


@app.post("/api/close")
async def close(req: CloseReq):
    if runtime.active_run():
        err(RuntimeError("A close is already in progress. Finish or stop it before starting another."), 409)
    try:
        sid = await runtime.start_close(req.period, req.message, demo_assist=req.demo_assist)
    except Exception as e:
        err(e)
    return {"session_id": sid}


class MsgReq(BaseModel):
    text: str


@app.post("/api/message")
async def message(req: MsgReq):
    try:
        return {"session_id": await runtime.send_message(req.text)}
    except Exception as e:
        err(e)


class AskReq(BaseModel):
    question: str


@app.post("/api/ask")
async def ask(req: AskReq):
    if not req.question.strip():
        err(ValueError("ask a question"))
    try:
        return {"session_id": await runtime.ask(req.question.strip()[:1000])}
    except Exception as e:
        err(e)


@app.post("/api/ask/reset")
async def ask_reset():
    runtime.state.data.pop("ask_session", None)
    runtime.state.save()
    return {"ok": True}


@app.get("/api/transactions/{txn_id}")
async def transaction_detail(txn_id: int):
    t = db.q1("SELECT * FROM transactions WHERE id=?", txn_id)
    if t is None:
        err(FileNotFoundError("transaction"), 404)
    t = dict(t)
    entries = []
    for e in db.rows(db.q("SELECT * FROM journal_entries WHERE txn_id=? ORDER BY id", txn_id)):
        e["lines"] = [{**l, "account_name": coa.NAMES.get(l["account"], l["account"])}
                      for l in db.rows(db.q("SELECT account, debit_minor, credit_minor FROM journal_lines WHERE entry_id=?", e["id"]))]
        entries.append(e)
    inv = db.q1("SELECT number FROM invoices WHERE id=?", t["invoice_id"]) if t["invoice_id"] else None
    audit_rows = db.rows(db.q("SELECT ts, actor, action, detail FROM audit_log WHERE detail LIKE ? OR detail LIKE ? ORDER BY id",
                              f'%"txn": {txn_id}%', f'%"txn_id": {txn_id}%'))
    method = {"rule": "Bookkeeping rule", "memory_rule": "Rule Ledgerly learned from you (memory)", "similarity": "Matched to a past month",
              "model": "AI judgment", "payment_match": "Matched to an invoice", "human": "Set by you in review",
              "history": "Approved in an earlier close", "reconciliation": "Found by the bank reconciliation"}.get(t["cat_method"] or "", "Not categorized")
    src_file, _, src_row = (t["source_ref"] or "").partition(":")
    return {"txn": t, "category": cats.label(t["category"]) if t["status"] != "new" else cats.OPEN_ITEM,
            "type": cats.txn_type(t["amount_minor"]), "account": cats.ACCOUNT_LABEL.get(t["funding_account"]),
            "method": method, "entries": entries, "invoice": inv["number"] if inv else None,
            "source": {"file": src_file, "row": src_row}, "audit": audit_rows,
            "receipt_url": f"/inbox/{t['receipt_file']}" if t["receipt_file"] else None}


@app.get("/inbox/{name}")
async def inbox_file(name: str):
    p = config.INBOX / Path(name).name
    if not p.exists() or p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".pdf"):
        err(FileNotFoundError(name), 404)
    return FileResponse(p)


@app.post("/api/interrupt")
async def interrupt():
    sid = runtime.state.data.get("current_session")
    if not (runtime.client and sid):
        err(RuntimeError("no session"))
    r = await runtime.client.post_events(runtime.state.agent_id, sid, [{"type": "user.interrupt"}])
    return {"accepted": bool(r and r[0].get("accepted"))}


@app.get("/api/feed")
async def feed(request: Request):
    q = runtime.hub.subscribe()

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(item, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            runtime.hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------------------------------- ledger
@app.get("/api/overview")
async def overview(period: str = PERIOD):
    with db.tx() as c:
        st = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) AS n FROM transactions WHERE period=? GROUP BY status", (period,))}
        methods = {r["cat_method"] or "uncategorized": r["n"] for r in c.execute(
            "SELECT cat_method, COUNT(*) AS n FROM transactions WHERE period=? AND status NOT IN ('duplicate','linked') GROUP BY cat_method", (period,))}
        p = reports.pnl(c, period)
        cash = reports.journal.balances(c, end=reports.bounds(period)[1]).get("1000", 0)
        summ = c.execute("SELECT markdown, created_at FROM close_summaries WHERE period=?", (period,)).fetchone()
        tb = reports.journal.trial_balance_ok(c)
        entries = c.execute("SELECT COUNT(*) AS n FROM journal_entries WHERE date LIKE ?", (period + "%",)).fetchone()["n"]
        prior_cash = reports.journal.balances(c, end=reports.bounds(reports.prev_period(period))[1]).get("1000", 0)
        anomalies = len(reports.anomalies(c, period)["anomalies"]) if st else 0
        ar = invoicing.open_invoices(c, reports.bounds(period)[1])
        drafts = c.execute("SELECT COUNT(*) AS n FROM invoices WHERE status='draft'").fetchone()["n"]
        review = c.execute("SELECT COUNT(*) AS n FROM transactions WHERE period=? AND category='9999'", (period,)).fetchone()["n"]
        L = books.ledger(c, period)
        rec = books.reconciliation(c, period)
        oi = books.open_items(c, period)
        delivered = c.execute("SELECT * FROM deliveries_log WHERE period=? ORDER BY rowid DESC LIMIT 1", (period,)).fetchone()
    kpi = lambda v, prev=None: {"minor": v, "fmt": fmt(v), **({"prev_fmt": fmt(prev), "delta_minor": v - prev} if prev is not None else {})}
    pr = p["prior"]
    return {"period": period, "by_status": st, "by_method": methods, "entries": entries, "trial_balance_ok": tb,
            "revenue": kpi(p["total_revenue_minor"], pr["total_revenue_minor"]),
            "expenses": kpi(p["total_expenses_minor"], pr["total_expenses_minor"]),
            "net_income": kpi(p["net_income_minor"], pr["net_income_minor"]), "cash": kpi(cash, prior_cash),
            "suspense": kpi(p["suspense_pending_review_minor"]), "anomalies": anomalies, "review_count": review,
            "receivables": kpi(sum(i["balance_minor"] for i in ar)), "open_invoices": len(ar), "draft_invoices": drafts,
            "imported": sum(v for k, v in st.items() if k not in ("duplicate", "linked")),
            "books": {"income": kpi(L["total_income_minor"]), "expenses": kpi(L["total_expenses_minor"]), "count": L["count"],
                      "reconciliation": {"status": rec["status"], "difference": fmt(rec["difference_minor"] or 0),
                                         "theoretical": fmt(rec["theoretical_minor"]),
                                         "statement": fmt(rec["statement_balance_minor"]) if rec["statement_balance_minor"] is not None else None},
                      "open_items": oi["open_count"], "resolved_items": oi["resolved_count"],
                      "delivered": dict(delivered) if delivered else None},
            "summary": dict(summ) if summ else None}


@app.get("/api/transactions")
async def transactions(period: str = PERIOD):
    return db.rows(db.q("SELECT id, date, amount_minor, counterparty, description, source, funding_account, status, category,"
                        " confidence, cat_method, cat_reason, receipt_file, invoice_id FROM transactions WHERE period=? ORDER BY date, id", period))


@app.get("/api/review")
async def review(period: str = PERIOD):
    return db.rows(db.q("SELECT id, date, amount_minor, description, source, confidence, cat_reason, receipt_file FROM transactions"
                        " WHERE period=? AND category='9999' ORDER BY amount_minor", period))


class ResolveReq(BaseModel):
    account_code: str


@app.post("/api/review/{txn_id}")
async def resolve(txn_id: int, req: ResolveReq):
    try:
        r = invoicing.resolve_review(txn_id, req.account_code)
    except LedgerError as e:
        err(e)
    runtime.hub.publish("ledger", text=f"Review item #{txn_id} reclassified to {req.account_code} (reversing entry + repost)")
    pending = runtime.state.data.setdefault("pending_teach", [])
    t = db.q1("SELECT description FROM transactions WHERE id=?", txn_id)
    pending.append({**r["learned_rule"], "name": coa.NAMES[req.account_code], "example": t["description"]})
    runtime.state.save()
    return r


@app.post("/api/teach")
async def teach():
    items = runtime.state.data.get("pending_teach") or []
    if not items:
        err(RuntimeError("Resolve at least one review item first"))
    lines = "\n".join(f'- match "{i["match"]}" => account {i["account_code"]} ({i["name"]}); example: {i["example"]}' for i in items)
    text = ("I resolved the review list. Save each of these as a durable categorization rule in memory, exactly as your "
            f"playbook describes, so next month needs fewer reviews:\n{lines}")
    try:
        sid = await runtime.send_message(text)
    except Exception as e:
        err(e)
    runtime.state.data["pending_teach"] = []
    runtime.state.save()
    return {"session_id": sid, "rules": len(items)}


@app.get("/api/accounts")
async def accounts():
    return [{"code": cats.ACCOUNT[c], "name": c} for c in cats.CATEGORIES]


@app.get("/api/open-items")
async def open_items(period: str = PERIOD):
    with db.tx() as c:
        return books.open_items(c, period)


class OpenItemReq(BaseModel):
    resolution: str = ""
    note: str = ""
    category: str | None = None


@app.post("/api/open-items/{key:path}")
async def resolve_open_item(key: str, req: OpenItemReq):
    try:
        if key.startswith("cat:"):
            if req.category not in cats.CATEGORIES:
                raise LedgerError("choose a category")
            tid = int(key[4:])
            t = db.q1("SELECT amount_minor, description FROM transactions WHERE id=?", tid)
            if not cats.fits(req.category, t["amount_minor"]):
                raise LedgerError("Income is only for money in; Rent, Contractors, Software and Bank fees only for money out")
            r = invoicing.resolve_review(tid, cats.account_for(req.category, description=t["description"]))
            pending = runtime.state.data.setdefault("pending_teach", [])
            pending.append({**r["learned_rule"], "name": req.category, "example": t["description"]})
            runtime.state.save()
        else:
            r = books.resolve(key, resolution=req.resolution, note=req.note)
    except LedgerError as e:
        err(e)
    runtime.hub.publish("ledger", text=f"Open item {key} resolved")
    return r


@app.post("/api/receipt/{txn_id}")
async def upload_receipt(txn_id: int, file: UploadFile = File(...)):
    reg = register_upload(f"receipt_txn{txn_id}_{Path(file.filename or 'receipt').name}", await file.read())
    with db.tx() as c:
        c.execute("UPDATE transactions SET receipt_file=?, receipt_status='ok' WHERE id=?", (reg["name"], txn_id))
        c.execute("UPDATE files SET status='ingested' WHERE name=?", (reg["name"],))
        c.execute("INSERT OR REPLACE INTO open_item_resolutions (key, resolution, note, resolved_by, resolved_at) VALUES (?,?,?,?,?)",
                  (f"rcpt:{txn_id}", "attached", "Receipt uploaded by reviewer", "user", db.now()))
    runtime.hub.publish("ledger", text=f"Receipt attached to #{txn_id}")
    return {"txn_id": txn_id, "receipt": reg["name"]}


class BalanceReq(BaseModel):
    closing: str
    period: str = PERIOD


@app.post("/api/statement-balance")
async def statement_balance(req: BalanceReq):
    """Bank statement ending balance typed by the user (when the export has no Balance column)."""
    from .money import parse_amount
    try:
        closing = parse_amount(req.closing)
    except ValueError as e:
        err(e)
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO statements (file, account, period, opening_minor, closing_minor) VALUES ('entered by user', '1000', ?, NULL, ?)",
                  (req.period, closing))
        db.audit(c, "user", "statement_balance", {"period": req.period, "closing_minor": closing})
    runtime.hub.publish("ledger", text="Statement balance entered")
    return {"closing_minor": closing, "closing": fmt(closing)}


@app.get("/api/reconcile/investigate")
async def investigate(period: str = PERIOD):
    with db.tx() as c:
        return books.investigate(c, period)


@app.post("/api/deliver")
async def deliver(period: str = PERIOD):
    out = tools.build_books(period, final=True, actor="user")
    runtime.hub.publish("ledger", text=f"Books delivered: {out['status']}")
    return {"status": out["status"], "open_items": out["open_items"], "url": f"/out/reports/{out['path'].name}"}


@app.get("/api/invoices")
async def invoices():
    out = []
    for i in db.rows(db.q("SELECT i.*, c.name AS client, c.email FROM invoices i JOIN clients c ON c.id=i.client_id ORDER BY i.number DESC")):
        with db.tx() as c:
            i["balance_minor"] = invoicing.invoice_balance(c, i["id"])
        i["pdf"] = f"/out/invoices/{Path(i['pdf_path']).name}" if i.get("pdf_path") else None
        out.append(i)
    return out


@app.get("/api/outbox")
async def outbox():
    return db.rows(db.q("SELECT o.*, i.number FROM outbox o LEFT JOIN invoices i ON i.id=o.invoice_id ORDER BY o.id DESC"))


@app.get("/api/reports/{name}")
async def report(name: str, period: str = PERIOD):
    fn = books.SECTIONS.get(name) or reports.REPORTS.get(name)
    if fn is None:
        err(ValueError("unknown report"), 404)
    with db.tx() as c:
        return fn(c, period)


@app.get("/api/report-files")
async def report_files():
    d = config.OUT / "reports"
    return sorted(({"name": p.name, "url": f"/out/reports/{p.name}"} for p in d.glob("*.pdf")), key=lambda x: x["name"])


@app.get("/api/audit")
async def audit_log(limit: int = 200):
    return db.rows(db.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", limit))


@app.get("/out/{kind}/{name}")
async def out_file(kind: str, name: str):
    p = (config.OUT / kind / Path(name).name)
    if kind not in ("reports", "invoices") or not p.exists():
        err(FileNotFoundError(name), 404)
    return FileResponse(p, media_type="application/pdf")


# ----------------------------------------------------------------------------------------- platform
@app.get("/api/approvals")
async def approvals():
    try:
        return await runtime.approvals()
    except Exception as e:
        err(e, 502)


class DecisionReq(BaseModel):
    decision: str  # allow-once | allow-always | deny


@app.post("/api/approvals/{approval_id}")
async def decide(approval_id: str, req: DecisionReq):
    if req.decision not in ("allow-once", "allow-always", "deny"):
        err(ValueError("bad decision"))
    try:
        return await runtime.resolve_approval(approval_id, req.decision)
    except Exception as e:
        err(e, 502)


@app.get("/api/artifacts")
async def artifacts():
    sid = runtime.state.data.get("current_session")
    if sid and runtime.client:
        return await runtime.refresh_artifacts(sid)
    return []


@app.get("/api/artifacts/{artifact_id}")
async def artifact(artifact_id: str):
    try:
        return RedirectResponse(await runtime.artifact_url(artifact_id))
    except Exception as e:
        err(e, 502)


@app.post("/api/schedule/trigger")
async def trigger_schedule():
    if not runtime.client or not runtime.state.agent_id:
        err(RuntimeError("agent not provisioned"))
    try:
        r = await runtime.client.trigger_schedule(runtime.state.agent_id, "weekly-overdue-check")
    except Exception as e:
        err(e, 502)
    runtime.hub.publish("system", text="Weekly overdue-invoice check triggered (runs as a scheduled turn in a new session)")
    return r


_seen_hooks: set[str] = set()


@app.post("/webhooks/zoowork")
async def webhook(request: Request):
    raw = await request.body()
    secret = runtime.state.data.get("webhook_secret")
    if not secret:
        return Response(status_code=503)
    try:
        ev = unwrap_webhook(headers=request.headers, raw_body=raw, secret=secret)
    except WebhookSignatureError:
        return Response(status_code=400)
    if ev.id != request.headers.get("webhook-id"):
        return Response(status_code=400)
    if ev.id in _seen_hooks:
        return Response(status_code=204)
    _seen_hooks.add(ev.id)
    sid = ev.data.get("session_id")
    runtime.hub.publish("webhook", text=f"Webhook: {ev.type}", event_type=ev.type, session_id=sid)
    if ev.type in ("session.created", "schedule.dispatched") and sid and sid not in runtime.state.data["sessions"]:
        runtime.state.data["sessions"][sid] = {"kind": "scheduled", "cursor": None, "active": True}
        runtime.state.save()
        runtime.follow(sid)
    return Response(status_code=204)


@app.post("/api/reset")
async def do_reset(mode: str = "clean"):
    if runtime.active_run():
        err(RuntimeError("A close is in progress. Stop or finish it before clearing data."), 409)
    db.close()
    if mode == "demo":
        r = reset_mod.reset()
        msg = "Demo reset: ledger restored (September closed, October not started). Agent memory kept."
    else:
        r = reset_mod.reset_empty()
        msg = "All local ledger data, uploads, reports, and close history were cleared. Agent setup and memory were kept."
        runtime.state.data["sessions"] = {}
        runtime.state.data["runs"] = []
        runtime.state.data.pop("ask_session", None)
    runtime.state.data["current_session"] = None
    runtime.state.data["pending_teach"] = []
    runtime.state.save()
    runtime.hub.buffer.clear()
    runtime.hub.publish("system", text=msg)
    runtime.hub.publish("ledger", text="reset")
    return r


@app.get("/")
async def index():
    return FileResponse(config.STATIC / "index.html")


app.mount("/static", StaticFiles(directory=config.STATIC), name="static")
# MCP (streamable HTTP at /mcp) is mounted last so the API routes above take precedence.
app.mount("/", mcp_app)
