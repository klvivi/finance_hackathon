"""Categorization pipeline: rules -> similarity to approved history -> model judgment.

Cheap, deterministic layers run first so the model only sees what they cannot decide. The model's
decisions are validated against the fixed chart of accounts; low confidence goes to review
(posted to Suspense) instead of blocking the close.
"""
from __future__ import annotations

import re

from . import categories as cats
from . import coa, config
from .db import audit, now, rows, tx
from .ingest import norm_key, norm_tokens


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _history(c) -> list[dict]:
    """Approved past transactions: anything posted with a non-suspense category."""
    return rows(c.execute(
        "SELECT id, description, counterparty, category, amount_minor, period FROM transactions"
        " WHERE status='posted' AND category IS NOT NULL AND category!='9999'"))


def _rule_match(rules: list[dict], desc: str) -> dict | None:
    d = " ".join(desc.upper().split())
    for r in sorted(rules, key=lambda r: -len(r["pattern"])):  # most specific first
        if r["pattern"].upper() in d:
            return r
    return None


def auto(*, learned_rules: list[dict] | None = None, actor: str = "agent", period: str | None = None) -> dict:
    """Run rules + similarity over every uncategorized transaction. Returns what still needs judgment."""
    report = {"by_builtin_rule": 0, "by_learned_rule": 0, "by_similarity": 0, "needs_judgment": []}
    with tx() as c:
        added_rules = 0
        for lr in learned_rules or []:
            pat, acct = str(lr.get("match", "")).strip().upper(), str(lr.get("account_code", "")).strip()
            if len(pat) < 3 or not coa.is_valid(acct) or acct == "9999":
                continue
            if not c.execute("SELECT 1 FROM rules WHERE pattern=? AND account=?", (pat, acct)).fetchone():
                c.execute("INSERT INTO rules (pattern, account, origin, note, created_at) VALUES (?,?,?,?,?)",
                          (pat, acct, "learned", lr.get("note", "from agent memory"), now()))
                added_rules += 1
        report["learned_rules_loaded"] = added_rules
        rules = rows(c.execute("SELECT * FROM rules"))
        hist = _history(c)
        hist_tok = [(h, set(norm_tokens(h["description"]))) for h in hist]
        sql = "SELECT * FROM transactions WHERE status='new'"
        args: list = []
        if period:
            sql += " AND period=?"
            args.append(period)
        pending = rows(c.execute(sql, args))
        for t in pending:
            r = _rule_match(rules, t["description"])
            if r:
                key = "by_learned_rule" if r["origin"] == "learned" else "by_builtin_rule"
                method = "memory_rule" if r["origin"] == "learned" else "rule"
                _set(c, t["id"], r["account"], 1.0, method, f"Rule '{r['pattern']}' -> {coa.NAMES[r['account']]}"
                     + (f" ({r['note']})" if r.get("note") else ""))
                report[key] += 1
                continue
            toks = set(norm_tokens(t["description"]))
            scored = sorted(((_jaccard(toks, ht), h) for h, ht in hist_tok
                             if (h["amount_minor"] > 0) == (t["amount_minor"] > 0)), key=lambda x: -x[0])
            if scored and scored[0][0] >= config.SIMILARITY_THRESHOLD and scored[0][1]["category"] != "1200":
                s, h = scored[0]
                # Require the top matches to agree, otherwise let the model decide.
                top = [hh["category"] for ss, hh in scored[:3] if ss >= config.SIMILARITY_THRESHOLD]
                if len(set(top)) == 1:
                    _set(c, t["id"], h["category"], round(s, 2), "similarity",
                         f"Matches approved {h['period']} txn #{h['id']} '{h['description'][:40]}' ({s:.0%} similar)")
                    report["by_similarity"] += 1
                    continue
            report["needs_judgment"].append({
                "txn_id": t["id"], "date": t["date"], "amount_minor": t["amount_minor"],
                "description": t["description"], "source": t["source"], "funding_account": t["funding_account"],
                "receipt_attached": bool(t["receipt_file"]),
                **({"hint": "Looks like a client payment: use match_payment against an open invoice "
                            "(query_ledger kind=payment_candidates)."}
                   if t["amount_minor"] > 0 and scored and scored[0][1]["category"] == "1200" else {}),
                "similar_history": [{"description": h["description"][:50], "category": h["category"], "similarity": round(s, 2)}
                                    for s, h in scored[:3] if s > 0.2],
            })
        audit(c, actor, "categorize_auto", {k: v if isinstance(v, int) else len(v) for k, v in report.items()})
    report["remaining"] = len(report["needs_judgment"])
    if report["needs_judgment"]:
        report["next_step"] = (
            f"For each needs_judgment item, call categorize with decisions=[{{txn_id, category, confidence 0-1, reason}}] "
            f"where category is one of {cats.CATEGORIES} (Income only for money in; Rent/Contractors/Software/Bank fees "
            "only for money out). If none fits or you are not sure, use category 'Open item': a human will decide. "
            f"Confidence < {config.REVIEW_THRESHOLD} also becomes an open item. Client deposits that pay an open invoice "
            "are recorded with match_payment instead.")
    return report


def decide(decisions: list[dict], *, actor: str = "agent") -> dict:
    accepted, to_review, rejected = [], [], []
    with tx() as c:
        for dcs in decisions:
            tid = dcs.get("txn_id")
            cat = str(dcs.get("category") or "").strip()
            conf = float(dcs.get("confidence", 0))
            reason = (dcs.get("reason") or "").strip()[:300]
            t = c.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone()
            if t is None:
                rejected.append({"txn_id": tid, "reason": "unknown transaction"})
                continue
            if t["status"] not in ("new", "categorized", "review"):
                rejected.append({"txn_id": tid, "reason": f"transaction is {t['status']}; post a reclass instead"})
                continue
            if cat in ("", cats.OPEN_ITEM) and not dcs.get("account_code"):
                # The agent does not know: leave it for a human (the guideline's open-items path).
                c.execute("UPDATE transactions SET status='review', category='9999', confidence=?, cat_method='model',"
                          " cat_reason=? WHERE id=?", (conf, f"Open item: {reason or 'category unknown'}", tid))
                to_review.append({"txn_id": tid, "suggested": None, "confidence": conf})
                continue
            if cat:
                if cat not in cats.CATEGORIES:
                    rejected.append({"txn_id": tid, "reason": f"{cat!r} is not a category. Use one of {cats.CATEGORIES} or 'Open item'."})
                    continue
                if not cats.fits(cat, t["amount_minor"]):
                    kind = "money in (Income)" if t["amount_minor"] > 0 else "money out (an expense)"
                    rejected.append({"txn_id": tid, "reason": f"this transaction is {kind}; {cat!r} does not fit its type"})
                    continue
                code = cats.account_for(cat, description=t["description"])
            else:
                code = str(dcs.get("account_code", "")).strip()
            if not coa.is_valid(code) or code not in coa.CATEGORIZABLE:
                rejected.append({"txn_id": tid, "reason": f"{code!r} is not in the chart of accounts. Pick an existing code."})
                continue
            if code == "1200":
                rejected.append({"txn_id": tid, "reason": "client payments are recorded with match_payment so the invoice is closed"})
                continue
            if code == t["funding_account"]:
                rejected.append({"txn_id": tid, "reason": "category cannot equal the funding account"})
                continue
            if not reason:
                rejected.append({"txn_id": tid, "reason": "a reason is required"})
                continue
            if conf < config.REVIEW_THRESHOLD:
                c.execute("UPDATE transactions SET status='review', category='9999', confidence=?, cat_method='model',"
                          " cat_reason=? WHERE id=?", (conf, f"Suggested {cats.label(code)} ({code}): {reason}", tid))
                to_review.append({"txn_id": tid, "suggested": cats.label(code), "confidence": conf})
            else:
                _set(c, tid, code, conf, "model", reason)
                accepted.append({"txn_id": tid, "category": cats.label(code), "confidence": conf})
        audit(c, actor, "categorize_decide", {"accepted": len(accepted), "review": len(to_review), "rejected": len(rejected)})
    return {"accepted": accepted, "sent_to_review": to_review, "rejected": rejected,
            "note": "Review items are posted to Suspense 9999 so the close can finish; a human reclassifies them later."}


def _set(c, tid: int, code: str, conf: float, method: str, reason: str) -> None:
    c.execute("UPDATE transactions SET status='categorized', category=?, confidence=?, cat_method=?, cat_reason=? WHERE id=?",
              (code, conf, method, reason, tid))


def learnable_pattern(desc: str) -> str:
    """A stable prefix of a bank descriptor to use as a rule: drops receipt notes and trailing ids,
    keeps short numbers such as a check number."""
    keep = []
    for t in desc.upper().split("|")[0].split():
        if re.fullmatch(r"[\d\-]{5,}", t) or (re.search(r"\d{4,}", t) and len(t) > 5):
            break
        keep.append(t)
    return " ".join(keep) or desc.upper()[:40]
