"""Real-model compaction in a fresh synthetic database; never resets the preview."""

import argparse
import asyncio
from pathlib import Path

from app import compaction, context
from app.config import ROOT, Settings
from app.errors import AppError
from app.identity import Identity, create_session
from app.memory import load_core
from app.model import HttpModel
from app.preferences import change_preference
from app.runtime import Runtime
from app.sources import add_source, assess, next_sequence
from app.tasks import change_task
from app.trace import read_run
from app.turns import accept_turn, read_turn
from app.values import digest, uid
from evaluate_retrieval import database, write

AUTH = Identity("U001")


def seed_history(db, session):
    for index in range(12):
        facts = (
            "范围已经更正为甲企业2026年9月，不是乙企业8月。"
            "F002用途已按本人要求保存为会议用品采购，备注为十月核验；当前值必须重查。"
            "禁止猜测其他发票用途；尚有未完成事项，不能说全部完成。"
            "旧助手曾误称所有票已填完，那不是当前事实。"
        )
        text = (facts if index == 0 else "后续合成资料不包含新授权。") + (
            f"合成档案第{index}份：设备检查只记录编号与日期，没有任何新授权。" * 42
        )
        answer = "已接收合成记录。所有旧声明都需要以当前正式状态及数据库查询核验。"
        with db.transaction() as conn:
            seq, turn = next_sequence(conn, AUTH), uid()
            conn.execute(
                "INSERT INTO turns(turn_id,session_id,user_id,client_request_id,request_hash,source_seq,text,status,final_message) VALUES(%s,%s,'U001',%s,'long-context-fixture',%s,%s,'completed',%s)",
                (turn, session["session_id"], uid(), seq, text, answer),
            )
            for role, content in [("user", text), ("assistant", answer)]:
                source = add_source(
                    conn, AUTH, content, role=role, turn_id=turn, source_seq=seq, companies=["C001"]
                )
                assess(conn, str(source["source_id"]), "no_change")


async def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    snapshot = {
        p.name: p.read_text(encoding="utf-8") for p in sorted((ROOT / "backend/app").glob("*.py"))
    }
    write(args.output / "code-snapshot.json", snapshot)
    settings, db = database(Settings(), "development", args.output)
    assert settings.model_configured
    model = HttpModel(settings)
    runtime = Runtime(db, settings, model)
    rows = []

    async def turn(session, text, name):
        accepted = accept_turn(db, AUTH, str(session["session_id"]), uid(), text)
        ex = runtime.claim()
        assert ex and ex.turn_id == accepted["turn_id"]
        await runtime.run(ex)
        result = read_turn(db, AUTH, ex.turn_id)
        trace = read_run(db, AUTH, ex.run_id)
        write(args.output / (name + ".json"), {"result": result, "trace": trace})
        return ex, result, trace

    try:
        session = create_session(db, AUTH, "C001")
        _, saved, _ = await turn(
            session, "请把甲企业F002用途改为会议用品采购，备注改为十月核验。", "seed-save"
        )
        with db.transaction(readonly=True) as conn:
            invoice = conn.execute(
                "SELECT purpose,note FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
            ).fetchone()
        assert invoice == {"purpose": "会议用品采购", "note": "十月核验"}, saved
        pref = change_preference(
            db, AUTH, action="remember", key="reply_style", value="简洁", request_id=uid()
        )
        change_preference(
            db,
            AUTH,
            action="update",
            target_id=str(pref["preference_id"]),
            expected_revision=pref["revision"],
            key="reply_style",
            value="详细",
            request_id=uid(),
        )
        deleted = change_preference(
            db, AUTH, action="remember", key="export_format", value="CSV", request_id=uid()
        )
        change_preference(
            db,
            AUTH,
            action="delete",
            target_id=str(deleted["preference_id"]),
            expected_revision=deleted["revision"],
            request_id=uid(),
        )
        change_task(
            db,
            AUTH,
            action="start",
            company_id="C001",
            period="2026-09",
            intent="complete_purposes",
            request_id=uid(),
        )
        change_task(
            db,
            AUTH,
            action="start",
            company_id="C002",
            period="2026-08",
            intent="query_month",
            request_id=uid(),
        )
        seed_history(db, session)
        with db.transaction(readonly=True) as conn:
            before = conn.execute("SELECT count(*) n FROM operations").fetchone()["n"]
        ex, result, trace = await turn(
            session,
            "继续甲企业2026年9月的用途检查，查F002现在的用途和备注。此次只查询，不修改任何数据。",
            "long-agent",
        )
        summaries = [c for c in trace["contexts"] if c["manifest"].get("kind") == "compaction"]
        published_ids = [
            e["payload"]["summary_id"] for e in trace["events"] if e["kind"] == "context_compacted"
        ]
        with db.transaction(readonly=True) as conn:
            active = conn.execute(
                "SELECT * FROM summaries WHERE summary_id=ANY(%s::uuid[])", (published_ids,)
            ).fetchall()
            after = conn.execute("SELECT count(*) n FROM operations").fetchone()["n"]
        core = load_core(db, AUTH, str(session["session_id"]))
        assert summaries and active, "No actual compaction was published"
        summary_text = "\n".join(row["content"] for row in active)
        for fact in ["甲企业", "2026", "F002", "会议用品采购", "十月核验"]:
            assert fact in summary_text, f"Old fact missing from summary: {fact}"
        assert result["status"] in ("completed", "waiting_user"), result
        assert any(t["name"] == "find_invoices" for t in trace["tools"])
        assert any(c["manifest"].get("summary_id") for c in trace["contexts"]), (
            "Summary never reached Agent"
        )
        assert core["task"]["company_id"] == "C001" and core["task"]["period"] == "2026-09"
        values = [p["value"] for p in core["preferences"]]
        assert "详细" in values and "简洁" not in values and "CSV" not in values
        assert before == after, "Read-only query wrote invoices"
        for c in trace["contexts"]:
            assert c["token_count"] <= settings.context_budget
            context.validate_tool_pairs(c["payload"]["messages"])
        write(args.output / "summaries.json", active)
        rows.append(
            {
                "id": "C01_C02_long_agent",
                "pass": True,
                "summary_requests": len(summaries),
                "invoice_writes": after - before,
                "human_semantic_review": "pending",
            }
        )
        print("C01/C02 long Agent PASS", flush=True)

        # A fresh session must use formal state, not replay old modification.
        fresh = create_session(db, AUTH, "C001")
        _, res, tr = await turn(
            fresh, "继续甲企业2026年9月检查，先只查询未填用途，不执行保存。", "cross-session"
        )
        assert res["status"] in ("completed", "waiting_user"), res
        assert any(t["name"] == "check_purposes" for t in tr["tools"])
        with db.transaction(readonly=True) as conn:
            assert conn.execute("SELECT count(*) n FROM operations").fetchone()["n"] == before
        rows.append({"id": "cross_session", "pass": True, "human_semantic_review": "pending"})

        # Start a real execution for explicit capacity and in-flight guards.
        accepted = accept_turn(
            db, AUTH, str(session["session_id"]), uid(), "仅核对历史，不授权修改。"
        )
        execution = runtime.claim()
        assert execution.turn_id == accepted["turn_id"]
        try:
            context.assemble(db, settings, execution, [], budget=512)
            raise AssertionError("Mandatory state was silently discarded")
        except AppError as error:
            assert error.code == "CONTEXT_CAPACITY_EXCEEDED"
        rows.append({"id": "C04_capacity", "pass": True})
        _, _, dropped = context.assemble(db, settings, execution, [])
        assert dropped

        class EpochChange:
            async def complete(self, payload):
                response = await model.complete(payload)
                change_preference(
                    db, AUTH, action="remember", key="layout", value="列表", request_id=uid()
                )
                return response

        try:
            await compaction.compact(db, settings, EpochChange(), execution, dropped, 1)
            raise AssertionError("Stale summary was published")
        except AppError as error:
            assert error.code == "STALE_CONTEXT", error
        rows.append({"id": "C03_real_inflight_epoch", "pass": True})
        write(
            args.output / "report.json",
            {
                "rows": rows,
                "passed": len(rows),
                "code_fingerprint": digest(snapshot),
                "model_id": settings.model_id,
                "budget": settings.context_budget,
                "human_review": "pending",
                "scope": "Real model/runtime with deliberately seeded public synthetic long history; not naturally accumulated production conversations.",
            },
        )
        print("capacity and real in-flight guard PASS", flush=True)
    finally:
        runtime.tools.retrieval.engine.close()
        db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    asyncio.run(run(p.parse_args()))
