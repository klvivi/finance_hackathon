"""ZooWork runtime glue: sessions, live event fan-out, custom-tool execution, approvals, artifacts.

The API key stays in this process. The browser only talks to our FastAPI routes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from contextlib import aclosing
from pathlib import Path
from typing import Any

import httpx
from zoowork import (ZooworkError, assistant_text, create_zoowork_client, custom_tool_use, is_run_finished,
                     run_outcome, thinking_text, tool_call)

from . import config, db, tools
from .journal import LedgerError

log = logging.getLogger("ledgerly")


# ------------------------------------------------------------------------------------------- state
class State:
    """Small JSON file: agent id, per-session cursors, run history. Survives a ledger reset."""

    def __init__(self, path: Path = config.STATE_PATH):
        self.path = path
        self.data: dict[str, Any] = {"agent_id": None, "sessions": {}, "runs": []}
        if path.exists():
            self.data.update(json.loads(path.read_text()))

    def save(self) -> None:
        config.ensure_dirs()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)

    @property
    def agent_id(self) -> str | None:
        return self.data.get("agent_id")


# --------------------------------------------------------------------------------------------- hub
class Hub:
    """Fan-out of feed items to every open browser (SSE). Keeps a replay buffer for late joiners."""

    def __init__(self, keep: int = 800):
        self.subs: set[asyncio.Queue] = set()
        self.buffer: list[dict] = []
        self.keep = keep
        self.seq = 0

    def publish(self, kind: str, **data) -> None:
        self.seq += 1
        item = {"id": self.seq, "ts": time.time(), "kind": kind, **data}
        self.buffer = (self.buffer + [item])[-self.keep:]
        for q in list(self.subs):
            q.put_nowait(item)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for item in self.buffer:
            q.put_nowait(item)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)


STEPS = [("preparing", "Preparing"), ("importing", "Pulling transactions and receipts"),
         ("payments", "Matching income to invoices"), ("invoicing", "Drafting invoices for delivered work"),
         ("categorizing", "Categorizing transactions"), ("posting", "Posting to the ledger"),
         ("reports", "Reconciling with the bank statement"), ("summary", "Preparing the books for your review")]
TOOL_STEP = {"memory_search": "preparing", "ingest_file": "importing", "image": "importing", "match_payment": "payments",
             "create_invoice": "invoicing", "categorize": "categorizing", "post_entry": "posting", "run_report": "reports",
             "save_close_summary": "summary", "artifact_publish": "summary"}


def _short(v: Any, n: int = 220) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------------------------------------------ runtime
class Runtime:
    def __init__(self) -> None:
        self.state = State()
        self.hub = Hub()
        self.client = None
        self.inflight: set[str] = set()
        self.following: set[str] = set()
        self.tasks: set[asyncio.Task] = set()
        self.key_present = bool(os.getenv("ZOOWORK_API_KEY"))

    # ---- lifecycle
    async def start(self) -> None:
        if self.key_present:
            self.client = create_zoowork_client(timeout=httpx.Timeout(30.0, read=75.0))
        if self.client and self.state.agent_id:
            self._spawn(self._poll_custom_tools())
            # Resume any session that was mid-turn when the server stopped.
            for sid, s in self.state.data["sessions"].items():
                if s.get("active"):
                    self.follow(sid)

    async def stop(self) -> None:
        for t in list(self.tasks):
            t.cancel()
        if self.client:
            await self.client.aclose()

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    def active_run(self) -> dict | None:
        r = next((r for r in reversed(self.state.data["runs"]) if r.get("status") == "running"), None)
        if r:
            r = {**r, "progress": self.state.data["sessions"].get(r["session_id"], {}).get("progress")}
        return r

    def ready(self) -> tuple[bool, str]:
        if not self.key_present:
            return False, "ZOOWORK_API_KEY is not set (add it to .env and restart the server)"
        if not self.state.agent_id:
            return False, "No agent provisioned yet: run `python -m backend.provision create`"
        return True, "ok"

    # ---- files into the agent workspace
    async def upload(self, path: Path, ws_rel: str) -> str | None:
        if not (self.client and self.state.agent_id):
            return None
        try:
            r = await self.client.upload_file(self.state.agent_id, ws_rel, path.read_bytes())
            return r["path"]
        except Exception as e:  # upload failure must not fail a bookkeeping step
            self.hub.publish("warn", text=f"Upload of {path.name} to workspace failed: {e}")
            return None

    # ---- the close
    async def start_close(self, period: str, label: str) -> str:
        ok, why = self.ready()
        if not ok:
            raise RuntimeError(why)
        if self.active_run():
            raise RuntimeError("A close is already in progress. Finish or stop it before starting another.")
        aid = self.state.agent_id
        files = db.rows(db.q("SELECT name, kind FROM files ORDER BY name"))
        if not files:
            raise RuntimeError("No files uploaded. Upload statements or click 'Load demo files'.")
        self.hub.publish("system", text=f"Uploading {len(files)} files to the agent workspace…")
        for f in files:
            ws = await self.upload(config.INBOX / f["name"], f"inbox/{f['name']}")
            with db.tx() as c:
                c.execute("UPDATE files SET workspace_path=? WHERE name=?", (ws, f["name"]))
        session = await self.client.create_session(aid, {"metadata": {"app": "ledgerly", "kind": "close", "period": period}})
        sid = session["session_id"]
        run_no = len(self.state.data["runs"]) + 1
        self.state.data["sessions"][sid] = {"kind": "close", "period": period, "cursor": None, "active": True,
                                            "run_no": run_no, "started": time.time()}
        self.state.data["runs"].append({"run_no": run_no, "session_id": sid, "period": period, "started": time.time(),
                                        "status": "running"})
        self.state.data["current_session"] = sid
        self.state.save()
        manifest = "\n".join(f"- /workspace/inbox/{f['name']} ({f['kind']})" for f in files)
        try:
            # One user.message carries the file manifest. (system.message posts were stored but answered with 502
            # on this deployment, so they are not used: a retry would duplicate them.)
            content = (f"{label}\n\nPeriod: {period}. Today is {time.strftime('%Y-%m-%d')}.\n"
                       f"Files uploaded for this close (ledger inbox name = workspace file name):\n{manifest}")
            await self._post(sid, {"type": "user.message", "content": content, "idempotency_key": f"close-{sid}"})
        except Exception:
            self.state.data["runs"][-1]["status"] = "failed_to_start"
            self.state.data["sessions"][sid]["active"] = False
            self.state.save()
            raise
        self.hub.publish("run", session_id=sid, text=f"Run #{run_no}: “{label}” sent to the agent", run_no=run_no)
        self.follow(sid)
        return sid

    async def _post(self, sid: str, event: dict, attempts: int = 4) -> dict:
        """Post one event; retry gateway errors with the same idempotency key (safe: the server dedupes)."""
        for i in range(attempts):
            try:
                r = await self.client.post_events(self.state.agent_id, sid, [event])
                if not r or r[0].get("accepted") is not True:
                    raise RuntimeError(f"event not accepted: {r}")
                return r[0]
            except ZooworkError as e:
                if not e.retryable or i == attempts - 1:
                    raise
            except httpx.TransportError:
                if i == attempts - 1:
                    raise
            await asyncio.sleep(1.5 * (i + 1))
        raise RuntimeError("unreachable")

    async def ask(self, question: str) -> str:
        """'Ask your books': a separate, read-only conversation that answers from the ledger tools."""
        ok, why = self.ready()
        if not ok:
            raise RuntimeError(why)
        sid = self.state.data.get("ask_session")
        if not sid:
            sess = await self.client.create_session(self.state.agent_id, {"metadata": {"app": "ledgerly", "kind": "ask"}})
            sid = sess["session_id"]
            self.state.data["ask_session"] = sid
            self.state.data["sessions"][sid] = {"kind": "ask", "cursor": None, "active": True}
            self.state.save()
        text = ("Question from the user about the books (answer from the ledger tools only, briefly, in plain English; "
                f"read-only: do not ingest, categorize, post or send anything). Period 2026-10.\n\n{question}")
        await self._post(sid, {"type": "user.message", "content": text, "idempotency_key": f"ask-{secrets.token_hex(8)}"})
        self.state.data["sessions"][sid]["active"] = True
        self.state.save()
        self.follow(sid)
        return sid

    async def send_message(self, text: str, sid: str | None = None) -> str:
        ok, why = self.ready()
        if not ok:
            raise RuntimeError(why)
        sid = sid or self.state.data.get("current_session")
        if not sid:
            raise RuntimeError("no session yet; run a close first")
        await self._post(sid, {"type": "user.message", "content": text, "idempotency_key": f"msg-{secrets.token_hex(8)}"})
        self.state.data["sessions"].setdefault(sid, {"kind": "other", "cursor": None})["active"] = True
        self.state.save()
        self.hub.publish("user", session_id=sid, text=text)
        self.follow(sid)
        return sid

    # ---- event stream -> feed + tool execution
    def follow(self, sid: str) -> None:
        if sid not in self.following:
            self.following.add(sid)
            self._spawn(self._follow(sid))

    async def _follow(self, sid: str) -> None:
        aid = self.state.agent_id
        sess = self.state.data["sessions"].setdefault(sid, {"kind": "scheduled", "cursor": None, "active": True})
        attempts, last_event = 0, time.time()
        try:
            # Keep following while the run may still be working (e.g. waiting on a human approval).
            while time.time() - last_event < 1800:
                try:
                    async with aclosing(self.client.stream_events(aid, sid, cursor=sess.get("cursor"))) as stream:
                        async for ev in stream:
                            attempts, last_event = 0, time.time()
                            finished = await self._on_event(sid, ev)
                            if ev.cursor:
                                sess["cursor"] = ev.cursor
                                self.state.save()
                            if finished:
                                sess["active"] = False
                                self.state.save()
                                return
                except ZooworkError as e:
                    if 400 <= e.status < 500:
                        self.hub.publish("error", session_id=sid, text=f"Stream error {e.status}: {e}")
                        return
                except (httpx.TransportError, httpx.TimeoutException):
                    pass  # reconnect from the saved cursor; nothing is lost (server-side resume)
                attempts += 1
                await asyncio.sleep(min(2 ** attempts, 15))
            sess["active"] = False
            for r in self.state.data["runs"]:  # an abandoned run must not block the next close
                if r["session_id"] == sid and r.get("status") == "running":
                    r["status"] = "stalled"
            self.state.save()
        finally:
            self.following.discard(sid)

    def _progress(self, sid: str, step: str | None = None, note: str | None = None) -> None:
        """Plain-language close progress for end users (the raw agent feed stays an admin view)."""
        sess = self.state.data["sessions"].get(sid)
        if not sess or sess.get("kind") != "close":
            return
        pr = sess.setdefault("progress", {"step": "preparing", "done": [], "note": ""})
        if step and step != pr["step"]:
            order = [s for s, _ in STEPS]
            cur = pr["step"]
            forward = (step not in order or cur not in order or order.index(step) > order.index(cur))
            if forward:  # never move the tracker backwards (e.g. a retry of an earlier tool)
                if cur in order and cur not in pr["done"]:
                    pr["done"].append(cur)
                if step in ("done", "stopped"):
                    pr["done"] = [s for s in order] if step == "done" else pr["done"]
                pr["step"] = step
        if note:
            pr["note"] = note
        self.state.save()
        self.hub.publish("progress", session_id=sid, **pr)

    async def _on_event(self, sid: str, ev) -> bool:
        t, p = ev.event_type, ev.payload
        base = {"session_id": sid, "run_id": ev.run_id}
        if t == "agent.tool":
            c = tool_call(ev)
            if c and c.phase == "start" and c.tool_name in TOOL_STEP:
                self._progress(sid, TOOL_STEP[c.tool_name])
        elif t == "agent.custom_tool_use":
            c = custom_tool_use(ev)
            if c and c.phase == "requested" and c.name in TOOL_STEP:
                self._progress(sid, TOOL_STEP[c.name])
        elif t == "agent.approval" and p.get("phase") == "requested":
            self._progress(sid, "approval")
        if t == "run.started":
            self.hub.publish("run", text="Agent run started", **base)
        elif t == "agent.assistant":
            txt = assistant_text(ev).strip()
            if txt:
                self.hub.publish("assistant", text=txt, **base)
                first = txt.split("\n")[0].strip("# ").strip()
                if len(first) < 220:
                    self._progress(sid, note=first)
        elif t == "agent.thinking":
            txt = thinking_text(ev).strip()
            if txt:
                self.hub.publish("thinking", text=_short(txt, 600), **base)
        elif t == "agent.tool":
            call = tool_call(ev)
            if call is None:
                return False
            if call.phase == "start":
                self.hub.publish("tool", phase="start", tool=call.tool_name, call_id=call.tool_call_id,
                                 args=_short(call.args or {}, 300), **base)
            elif call.phase == "end":
                self.hub.publish("tool", phase="end", tool=call.tool_name, call_id=call.tool_call_id,
                                 ok=not call.is_error, preview=_short(call.result_preview or "", 300), **base)
            else:
                self.hub.publish("tool", phase="blocked", tool=call.tool_name, call_id=call.tool_call_id,
                                 reason=p.get("deniedReason"), **base)
        elif t == "agent.custom_tool_use":
            cu = custom_tool_use(ev)
            if cu and cu.phase == "requested":
                self._spawn(self.execute_call(cu.call_id, cu.name or "", cu.input or {}, sid))
        elif t == "agent.approval":
            if p.get("phase") == "requested":
                self.hub.publish("approval", phase="requested", approval_id=p.get("approvalId"), tool=p.get("toolName"),
                                 arguments=p.get("arguments"), **base)
            else:
                self.hub.publish("approval", phase="resolved", approval_id=p.get("approvalId"), tool=p.get("toolName"),
                                 resolution=p.get("resolution"), **base)
        elif t == "agent.error":
            self.hub.publish("error", text=_short(p.get("errorMessage") or p, 400), detail=p.get("kind"), **base)
        elif t == "attachment.created":
            self.hub.publish("artifact", text="Agent produced a file", detail=_short(p, 300), **base)
        elif t == "agent.compaction":
            self.hub.publish("system", text="Context compacted", **base)
        elif is_run_finished(ev):
            outcome = run_outcome(ev)
            yielded = p.get("turnEndReason") == "yielded"
            self.hub.publish("run", text=f"Run finished: {outcome}" + (" (yielded)" if yielded else ""), outcome=outcome, **base)
            await self._record_finish(sid, outcome)
            await self.refresh_artifacts(sid)
            return not yielded
        return False

    async def _record_finish(self, sid: str, outcome: str | None) -> None:
        s = self.state.data["sessions"].get(sid, {})
        period = s.get("period", "2026-10")
        by_method = {r["cat_method"] or "none": r["n"] for r in db.q(
            "SELECT cat_method, COUNT(*) AS n FROM transactions WHERE period=? AND source!='history' GROUP BY cat_method", period)}
        review = db.q1("SELECT COUNT(*) AS n FROM transactions WHERE period=? AND category='9999'", period)["n"]
        from . import books
        with db.tx() as c:
            oi = books.open_items(c, period)
            rec = books.reconciliation(c, period)
        unknown = sum(1 for i in oi["items"] if i["kind"] == "category" and i["status"] == "Open")
        if s.get("kind") == "close":
            self._progress(sid, "done" if outcome == "succeeded" else "stopped")
        for r in self.state.data["runs"]:
            if r["session_id"] == sid and r.get("status") == "running":
                r.update({"status": outcome, "finished": time.time(), "review_items": review, "by_method": by_method,
                          "model_judgments": by_method.get("model", 0), "unknown_categories": unknown,
                          "open_items": oi["open_count"], "learned_rules_used": by_method.get("memory_rule", 0),
                          "reconciliation": rec["status"]})
        self.state.save()
        self.hub.publish("ledger", text="Books updated")

    # ---- custom tools
    async def execute_call(self, call_id: str, name: str, args: dict, sid: str | None) -> None:
        if call_id in self.inflight:
            return
        self.inflight.add(call_id)
        try:
            prior = db.q1("SELECT result, is_error FROM tool_calls WHERE call_id=?", call_id)
            if prior:
                result, is_err = json.loads(prior["result"]), bool(prior["is_error"])
            else:
                self.hub.publish("ledger_tool", phase="start", tool=name, args=_short(args, 300), session_id=sid, call_id=call_id)
                t0 = time.time()
                try:
                    result, is_err = tools.humanize(await tools.execute(name, args, upload=self.upload)), False
                except (LedgerError, KeyError, TypeError, ValueError) as e:
                    result, is_err = {"error": str(e), "hint": "Fix the input and call the tool again."}, True
                except Exception as e:  # unexpected: still answer the model so the run continues
                    log.exception("tool %s failed", name)
                    result, is_err = {"error": f"internal error: {e}"}, True
                with db.tx() as c:
                    c.execute("INSERT OR REPLACE INTO tool_calls (call_id, name, input, result, is_error, session_id, created_at)"
                              " VALUES (?,?,?,?,?,?,?)", (call_id, name, json.dumps(args), json.dumps(result, default=str),
                                                          int(is_err), sid, db.now()))
                self.hub.publish("ledger_tool", phase="end", tool=name, ok=not is_err, ms=int((time.time() - t0) * 1000),
                                 preview=_short(result, 400), session_id=sid, call_id=call_id)
                self.hub.publish("ledger", text="Books updated")
            await self.client.resolve_custom_tool_call(
                self.state.agent_id, call_id, content=[{"type": "json", "value": result}],
                is_error=is_err or None, resolved_by="ledgerly-backend")
        except ZooworkError as e:
            self.hub.publish("error", text=f"Could not return {name} result: {e.status} {e}", session_id=sid)
        finally:
            self.inflight.discard(call_id)

    async def _poll_custom_tools(self) -> None:
        """Safety net + scheduled runs: any pending call on this Agent gets executed, even with no browser open."""
        while True:
            try:
                for call in await self.client.list_custom_tool_calls(self.state.agent_id, status="pending"):
                    sid = call.get("session_id")
                    if sid and sid not in self.state.data["sessions"]:
                        self.hub.publish("system", session_id=sid, text="Picked up a session started by a schedule")
                        self.state.data["sessions"][sid] = {"kind": "scheduled", "cursor": None, "active": True}
                        self.state.save()
                        self.follow(sid)
                    if call["call_id"] not in self.inflight:
                        self._spawn(self.execute_call(call["call_id"], call["name"], call.get("input") or {}, sid))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("poll failed: %s", e)
            await asyncio.sleep(2.0)

    # ---- approvals
    async def approvals(self) -> list[dict]:
        if not (self.client and self.state.agent_id):
            return []
        return await self.client.list_approvals(self.state.agent_id, status="pending")

    async def resolve_approval(self, approval_id: str, decision: str) -> dict:
        r = await self.client.resolve_approval(self.state.agent_id, approval_id, decision=decision, resolved_by="ops-user")
        self.hub.publish("approval", phase="decided", approval_id=approval_id, resolution=decision)
        return r

    # ---- artifacts
    async def refresh_artifacts(self, sid: str) -> list[dict]:
        if not (self.client and self.state.agent_id):
            return []
        try:
            page = await self.client.list_artifacts(self.state.agent_id, session_id=sid, limit=100)
        except ZooworkError as e:
            self.hub.publish("warn", text=f"Could not list artifacts: {e}")
            return []
        arts = [{"artifact_id": a.get("artifact_id"), "name": Path(a.get("source_path") or a.get("name") or "").name,
                 "status": a.get("status"), "source_path": a.get("source_path")} for a in page.get("artifacts", [])]
        self.state.data.setdefault("artifacts", {})[sid] = arts
        self.state.save()
        if arts:
            self.hub.publish("artifacts", session_id=sid, count=len(arts))
        return arts

    async def artifact_url(self, artifact_id: str) -> str:
        r = await self.client.download_artifact(self.state.agent_id, artifact_id)
        return r["url"]


runtime = Runtime()
