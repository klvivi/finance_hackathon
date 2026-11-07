"""Ledgerly Outbox: a remote MCP server (streamable HTTP) for outbound client communication.

The platform calls it directly over a public URL (cloudflared tunnel to this backend). The Agent's
MCP declaration sets permission=always_ask, so every send pauses for a human approval first.
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .outbox import send

server = MCPServer(
    name="ledgerly-outbox",
    instructions="Outbound email delivery for Ledgerly. Every call is reviewed by a human before it runs.",
)


@server.tool(description="Email a drafted invoice (PDF attached by invoice_id) to the client's billing address. Requires approval.")
def send_invoice_email(invoice_id: int, to: str, subject: str, body: str) -> dict:
    return send(kind="invoice", invoice_id=invoice_id, to=to, subject=subject, body=body, channel="mcp")


@server.tool(description="Email a polite payment reminder for an overdue invoice. Requires approval.")
def send_payment_reminder(invoice_id: int, to: str, subject: str, body: str) -> dict:
    return send(kind="reminder", invoice_id=invoice_id, to=to, subject=subject, body=body, channel="mcp")


def asgi_app():
    # Stateless JSON responses: simplest for a hosted caller; DNS-rebinding protection is off because
    # requests arrive via the tunnel's public hostname (no browser is involved).
    return server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
