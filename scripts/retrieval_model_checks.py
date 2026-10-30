"""Actual DeepSeek agent replies, separate from retrieval-only measurements."""

import asyncio
import json

from app.identity import Identity, create_session
from app.model import HttpModel
from app.runtime import Runtime
from app.trace import read_run
from app.turns import accept_turn, read_turn
from app.values import safe_json, uid


async def check_replies(settings, db, retrieval, suite, output):
    if not settings.model_configured:
        return {"executed": False, "reason": "chat_model_not_configured"}
    runtime = Runtime(db, settings, HttpModel(settings), retrieval=retrieval)
    chosen = [q for q in suite["questions"] if q["id"] in ("H01Q2", "H03Q2", "H05Q2", "H17Q2")]
    chosen += [q for q in suite["boundary_questions"] if "query" in q]
    items = {d["id"]: d for d in suite["documents"]}
    records = []
    auth = Identity("U001")
    for q in chosen:
        session = create_session(db, auth, "C001")
        turn = accept_turn(
            db,
            auth,
            str(session["session_id"]),
            uid(),
            "请查阅甲企业知识资料回答：" + q["query"] + " 请依据原文；没有依据就明确说明。",
        )
        ex = runtime.claim()
        assert ex and ex.turn_id == turn["turn_id"]
        await runtime.run(ex)
        result = read_turn(db, auth, turn["turn_id"])
        trace = read_run(db, auth, ex.run_id)
        quotes = [c for c in result["final_cards"] if c["kind"] == "knowledge_quote"]
        excerpts = [retrieval.read(auth, c["citation_id"])["excerpt"] for c in quotes]
        served = "\n".join(excerpts)
        with db.transaction(readonly=True) as conn:
            writes = conn.execute(
                "SELECT count(*) AS n FROM operations o JOIN sources s ON o.source_id=s.source_id WHERE s.turn_id=%s",
                (turn["turn_id"],),
            ).fetchone()["n"]
        used_knowledge = any(t["name"] == "search_knowledge" for t in trace["tools"])
        if q["answerable"]:
            supported = all(items[q["document"]]["units"][u] in served for u in q["required_units"])
        else:
            supported = not quotes and used_knowledge
        row = {
            "id": q["id"],
            "answerable": q["answerable"],
            "pass": result["status"] == "completed" and supported and writes == 0,
            "used_knowledge": used_knowledge,
            "invoice_writes": writes,
            "status": result["status"],
            "semantic_human_review": "pending",
            "result": result,
            "trace": trace,
        }
        (output / (q["id"] + "-model.json")).write_text(
            json.dumps(safe_json(row), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        records.append(
            {
                k: row[k]
                for k in (
                    "id",
                    "answerable",
                    "pass",
                    "used_knowledge",
                    "invoice_writes",
                    "status",
                    "semantic_human_review",
                )
            }
        )
        print("model", q["id"], "pass" if row["pass"] else "FAIL", flush=True)
    return {
        "executed": True,
        "model_id": settings.model_id,
        "passed": sum(r["pass"] for r in records),
        "total": len(records),
        "records": records,
        "scope": "Automatic evidence/no-write checks; final natural-language semantic review is separate.",
    }


def run_model_checks(*args):
    return asyncio.run(check_replies(*args))
