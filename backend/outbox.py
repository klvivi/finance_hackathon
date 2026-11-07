"""Outbound delivery. For the demo, 'sending' writes to an outbox the UI shows (no real email leaves)."""
from __future__ import annotations

from .db import audit, now, q1, tx
from .journal import LedgerError


def send(*, kind: str, invoice_id: int, to: str, subject: str, body: str, channel: str) -> dict:
    inv = q1("SELECT i.*, c.email FROM invoices i JOIN clients c ON c.id=i.client_id WHERE i.id=?", invoice_id)
    if inv is None:
        raise LedgerError(f"invoice {invoice_id} not found")
    if to.strip().lower() != (inv["email"] or "").lower():
        raise LedgerError(f"recipient {to} is not the client's billing address on file ({inv['email']})")
    with tx() as c:
        c.execute("INSERT INTO outbox (kind, invoice_id, to_addr, subject, body, channel, created_at) VALUES (?,?,?,?,?,?,?)",
                  (kind, invoice_id, to, subject, body, channel, now()))
        if kind == "invoice" and inv["status"] == "draft":
            c.execute("UPDATE invoices SET status='sent', sent_at=? WHERE id=?", (now(), invoice_id))
        audit(c, "agent", f"send_{kind}", {"invoice": inv["number"], "to": to, "channel": channel})
    return {"delivered_to_outbox": True, "invoice": inv["number"], "to": to, "kind": kind,
            "attachment": f"{inv['number']}.pdf"}
