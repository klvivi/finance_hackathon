"""Provision and manage the Ledgerly Agent. Every command that creates/starts things is explicit.

    python -m backend.provision check                  # key present? catalog + balance (no Agent created)
    python -m backend.provision models                 # list selectable models
    python -m backend.provision create --model ALIAS   # create + start the Agent (billable resource)
    python -m backend.provision update                 # push playbook/tools/MCP changes (bumps config_version)
    python -m backend.provision schedule create|trigger|pause|delete|runs
    python -m backend.provision webhook create|test|delete
    python -m backend.provision status
    python -m backend.provision teardown               # delete schedules + webhooks, stop, delete the Agent
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys

from zoowork import ZooworkError, create_zoowork_client

from . import agent_spec, config
from .runtime import State


def _resource(model: str | None) -> dict:
    r = agent_spec.resource(model)
    r.setdefault("mcp", [])  # explicit empty list so an update removes a stale tunnel URL
    return r


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="provision")
    ap.add_argument("cmd")
    ap.add_argument("sub", nargs="?")
    ap.add_argument("--model", default=config.MODEL or None)
    a = ap.parse_args(argv)
    st = State()

    async with create_zoowork_client() as zw:
        if a.cmd == "check":
            models = await zw.list_models()
            sel = [m for m in models if m.get("selectable") is not False]
            default = next((m["model"] for m in sel if "model" in (m.get("default_for") or [])), None)
            print(f"key ok; {len(sel)} selectable models; platform default for 'model': {default}")
            try:
                b = await zw.get_balance()
                print(f"organization balance: {b.get('available_amount')} {b.get('currency')}")
            except ZooworkError as e:
                print("balance unavailable:", e.status, e)
            print("agent:", st.agent_id or "(none yet)")
            return 0

        if a.cmd == "models":
            for m in await zw.list_models():
                if m.get("selectable") is False:
                    continue
                print(f"{m['model']:<45} input={m.get('input')} ctx={m.get('context_window_tokens')} "
                      f"thinking={(m.get('thinking') or {}).get('levels')} default_for={m.get('default_for')}")
            return 0

        if a.cmd == "create":
            if st.agent_id:
                print("agent already exists:", st.agent_id, "(use update, or teardown first)")
                return 1
            if not a.model:
                print("pass --model ALIAS (see `models`) or set LEDGERLY_MODEL in .env")
                return 1
            models = {m["model"]: m for m in await zw.list_models()}
            if a.model not in models or models[a.model].get("selectable") is False:
                print(f"model {a.model!r} is not selectable; run `models`")
                return 1
            key = st.data.get("create_key") or f"ledgerly-create-{secrets.token_hex(6)}"
            st.data["create_key"] = key
            st.save()
            created = await zw.create_agent(_resource(a.model), idempotency_key=key)
            st.data["agent_id"] = created["agent_id"]
            st.data["model"] = a.model
            st.save()
            print("created", created["agent_id"], "config_version", created.get("config_version"))
            await zw.start_agent(created["agent_id"])
            await zw.wait_until_running(created["agent_id"], timeout=90)
            print("running. mcp outbox:", "on (" + config.PUBLIC_BASE_URL + ")" if config.PUBLIC_BASE_URL else "off (send_email fallback)")
            return 0

        aid = st.agent_id
        if not aid:
            print("no agent yet; run `create`")
            return 1

        if a.cmd == "update":
            res = _resource(a.model or st.data.get("model"))
            res.pop("name", None)
            upd = await zw.update_agent(aid, res)
            st.data["mcp_url"] = config.PUBLIC_BASE_URL or None
            st.save()
            print("updated; config_version", (upd.get("status") or {}).get("config_version"),
                  "| mcp:", config.PUBLIC_BASE_URL or "off")
            return 0

        if a.cmd == "status":
            ag = await zw.get_agent(aid)
            d, s = ag.get("declared", {}), ag.get("status", {})
            print(json.dumps({"agent_id": aid, "name": d.get("name"), "model": d.get("model"), "desired_state": s.get("desired_state"),
                              "config_version": s.get("config_version"), "custom_tools": [t["name"] for t in d.get("custom_tools", [])],
                              "mcp": [m.get("url") for m in d.get("mcp") or []], "tool_policy": d.get("tool_policy")}, indent=1))
            return 0

        if a.cmd == "schedule":
            sid = agent_spec.WEEKLY_AR_SCHEDULE["schedule_id"]
            if a.sub == "create":
                await zw.create_schedule(aid, agent_spec.WEEKLY_AR_SCHEDULE, idempotency_key=f"{aid}-{sid}")
                print(json.dumps(await zw.get_schedule(aid, sid), indent=1)[:1200])
            elif a.sub == "trigger":
                print(await zw.trigger_schedule(aid, sid))
            elif a.sub == "pause":
                print(await zw.update_schedule(aid, sid, {"enabled": False}))
            elif a.sub == "runs":
                print(json.dumps(await zw.list_schedule_runs(aid, sid, limit=10), indent=1)[:3000])
            elif a.sub == "delete":
                await zw.delete_schedule(aid, sid)
                print("deleted", sid)
            return 0

        if a.cmd == "webhook":
            if a.sub == "create":
                if not config.PUBLIC_BASE_URL:
                    print("set LEDGERLY_PUBLIC_URL (the tunnel URL) first")
                    return 1
                created = await zw.create_agent_webhook(aid, {
                    "url": f"{config.PUBLIC_BASE_URL}/webhooks/zoowork",
                    "event_types": ["run.finished", "session.created", "approval.requested", "schedule.dispatched"],
                    "description": "Ledgerly UI notifications"}, idempotency_key=f"{aid}-hook-{config.PUBLIC_BASE_URL[-24:]}")
                if created.get("signing_secret"):
                    st.data["webhook_secret"] = created["signing_secret"]  # stored server-side, gitignored
                st.data["webhook_id"] = created["endpoint"]["id"]
                st.save()
                print("webhook", created["endpoint"]["id"], "secret saved:", bool(created.get("signing_secret")))
            elif a.sub == "test":
                print(await zw.test_agent_webhook(aid, st.data["webhook_id"], idempotency_key=f"test-{secrets.token_hex(4)}"))
            elif a.sub == "delete":
                await zw.delete_agent_webhook(aid, st.data["webhook_id"])
                st.data.pop("webhook_id", None)
                st.data.pop("webhook_secret", None)
                st.save()
                print("deleted")
            return 0

        if a.cmd == "teardown":
            for s in await zw.list_schedules(aid):
                sid = s.get("schedule_id")
                if sid:
                    await zw.delete_schedule(aid, sid)
                    print("deleted schedule", sid)
            page = await zw.list_agent_webhooks(aid)
            for w in page.get("webhooks", []):
                await zw.delete_agent_webhook(aid, w["id"])
                print("deleted webhook", w["id"])
            print("stop:", await zw.stop_agent(aid))
            await zw.delete_agent(aid)
            print("deleted agent", aid)
            for k in ("agent_id", "create_key", "webhook_id", "webhook_secret", "current_session"):
                st.data.pop(k, None)
            st.data["sessions"] = {}
            st.save()
            return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
