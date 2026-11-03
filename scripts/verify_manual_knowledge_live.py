"""Real model and local RAG smoke in a fresh DB; never writes the user's DB."""

import argparse
import asyncio
import json
from pathlib import Path

from advanced_common import database, write
from app.config import Settings
from app.identity import Identity, create_session
from app.knowledge import Knowledge
from app.knowledge_jobs import KnowledgeJobs
from app.model import HttpModel
from app.runtime import Runtime
from app.telemetry import code_fingerprint
from app.turns import accept_turn, read_turn
from app.values import uid


async def verify(output):
    output.mkdir(parents=True, exist_ok=False)
    settings, db = database(Settings(), output)
    runtime = Runtime(db, settings, HttpModel(settings))
    auth = Identity("U001")
    records = []
    stage = "start"
    failure = None
    marker = "KH" + uid().replace("-", "")[:10]
    content = f"青禾柜的寄存编号为{marker}；对外移交必须事先获得部门负责人书面批准。"

    async def run(session, text, name, *, require_completed=True):
        turn = accept_turn(db, auth, session, name, text)
        execution = runtime.claim()
        assert execution and execution.turn_id == turn["turn_id"]
        await runtime.run(execution)
        result = read_turn(db, auth, execution.turn_id)
        with db.transaction(readonly=True) as conn:
            contexts = conn.execute(
                "SELECT payload FROM context_snapshots WHERE run_id=%s ORDER BY model_step",
                (execution.run_id,),
            ).fetchall()
        write(output / (name + ".json"), {"turn": result, "contexts": contexts})
        assert result["status"] == "completed" or (
            not require_completed and result["status"] == "waiting_user"
        ), result.get("error")
        return result, json.dumps(contexts, ensure_ascii=False)

    try:
        a = str(create_session(db, auth, "C001")["session_id"])
        stage = "ordinary_chat"
        await run(
            a, "本轮仅提供背景，不检索、不保存偏好、不提交知识库：" + content, stage
        )
        with db.transaction(readonly=True) as conn:
            assert (
                conn.execute("SELECT count(*) n FROM knowledge_documents").fetchone()[
                    "n"
                ]
                == 0
            )
            assert (
                conn.execute("SELECT count(*) n FROM history_index").fetchone()["n"]
                == 0
            )
            assert (
                conn.execute("SELECT count(*) n FROM history_jobs").fetchone()["n"] == 0
            )
        records.append({"case": stage, "passed": True})
        stage = "same_session_context"
        same, payload = await run(
            a,
            "仅确认刚才背景的寄存编号，不查询、不修改任何资料。",
            stage,
            require_completed=False,
        )
        assert marker in payload
        records.append(
            {
                "case": stage,
                "passed": True,
                "check": "input_contains_same_session_context",
                "reply_status": same["status"],
                "semantic_reply_verified": False,
            }
        )
        b = str(create_session(db, auth, "C001")["session_id"])
        stage = "new_session_boundary"
        result, payload = await run(
            b, "我在另一个会话提过的青禾柜寄存编号是什么？", stage
        )
        assert marker not in payload
        assert "普通聊天" in result["final_message"]
        records.append({"case": stage, "passed": True})
        stage = "draft_not_published"
        result, _ = await run(
            b, "请把以下文字加入甲企业知识库，先准备待我提交的草稿：" + content, stage
        )
        draft = next(c for c in result["final_cards"] if c["kind"] == "knowledge_draft")
        with db.transaction(readonly=True) as conn:
            assert (
                conn.execute("SELECT count(*) n FROM knowledge_documents").fetchone()[
                    "n"
                ]
                == 0
            )
        records.append({"case": stage, "passed": True})
        stage = "manual_submit_and_real_index"
        submitted = Knowledge(db, settings).upload(
            auth,
            name="manual-text.txt",
            content=draft["content"].encode("utf-8"),
            title=draft["title"],
            company_id="C001",
            request_id=draft["draft_request_id"],
            submission_kind="manual_text",
        )
        jobs = KnowledgeJobs(db, settings, runtime.tools.retrieval.engine)
        await asyncio.to_thread(jobs.run, jobs.claim())
        document = Knowledge(db, settings).get(auth, submitted["document_id"])
        assert str(document["active_version_id"]) == submitted["version_id"]
        records.append({"case": stage, "passed": True})
        c = str(create_session(db, auth, "C001")["session_id"])
        stage = "new_session_manual_knowledge"
        result, payload = await run(
            c,
            "请仅根据已提交的甲企业知识资料，查青禾柜寄存编号和对外移交条件，并给出资料引用。",
            stage,
        )
        assert marker in payload
        assert any(card["kind"] == "knowledge_quote" for card in result["final_cards"])
        records.append({"case": stage, "passed": True})
        print("manual_knowledge_live PASS", flush=True)
    except Exception as exc:
        failure = {"stage": stage, "error_type": type(exc).__name__}
        raise
    finally:
        write(
            output / "report.json",
            {
                "mode": "real_model_real_local_embedding",
                "model": settings.model_id,
                "code_fingerprint": code_fingerprint(),
                "records": records,
                "passed": len(records) == 6 and failure is None,
                "failure": failure,
                "human_semantic_review": "pending",
            },
        )
        runtime.tools.retrieval.engine.close()
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    asyncio.run(verify(parser.parse_args().output.resolve()))
