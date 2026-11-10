import json

import psycopg
import pytest
from test_compaction import model_settings

from app import context
from app.conversation_history import evidence_in, read_page, search
from app.errors import AppError
from app.identity import Identity, create_session
from app.session_management import organize
from app.sources import add_source, assess
from app.turns import accept_turn
from app.values import uid

AUTH = Identity("U001")


def settled(db, session_id, text, answer="已记录。"):
    turn = accept_turn(db, AUTH, session_id, uid(), text)
    with db.transaction() as conn:
        user_source = conn.execute(
            "SELECT * FROM sources WHERE turn_id=%s AND role='user'", (turn["turn_id"],)
        ).fetchone()
        assess(conn, str(user_source["source_id"]), "no_change")
        source = add_source(
            conn,
            AUTH,
            answer,
            role="assistant",
            kind="assistant_final",
            turn_id=turn["turn_id"],
            source_seq=turn["source_seq"],
            companies=["C001"],
        )
        assess(conn, str(source["source_id"]), "no_change")
        conn.execute(
            "UPDATE turns SET status='completed',final_message=%s,final_response_ref=%s WHERE turn_id=%s",
            (answer, source["source_id"], turn["turn_id"]),
        )
        conn.execute(
            "UPDATE work_items SET state='done' WHERE source_id=%s", (user_source["source_id"],)
        )
    return str(user_source["source_id"]), str(source["source_id"])


def test_no_twelve_turn_cutoff(db, admin, execution_factory, settings):
    execution = execution_factory("继续")
    # Historical records must precede the current request snapshot.
    refs = []
    for n in range(30):
        refs.append(settled(db, execution.session_id, f"方案步骤编号PLAN-{n}，这里只讨论不修改。"))
    with db.transaction() as conn:
        seq = conn.execute(
            "UPDATE users SET next_source_seq=next_source_seq+1 WHERE user_id='U001' RETURNING next_source_seq"
        ).fetchone()["next_source_seq"]
        conn.execute("UPDATE turns SET source_seq=%s WHERE turn_id=%s", (seq, execution.turn_id))
    execution.source_seq = seq
    config = model_settings(settings).model_copy(
        update={"input_budget": 100000, "model_context_limit": 200000}
    )
    payload, manifest, dropped = context.assemble(db, config, execution, [])
    assert not dropped
    assert manifest["history_group_count"] == 30
    assert "PLAN-0" in json.dumps(payload, ensure_ascii=False)
    assert manifest["context_policy"]["recent_turns"] is None
    assert admin.execute("SELECT count(*) n FROM knowledge_documents").fetchone()["n"] == 0


def test_independent_history_search_exact_read_and_delete(db, execution_factory):
    old = create_session(db, AUTH, "C001")
    source_id, _ = settled(db, old["session_id"], "昨天架构升级方案：先备份再迁移，不自动改数据。")
    execution = execution_factory("找回升级方案")
    result = search(db, execution, query="架构升级")
    assert result["matches"][0]["source_id"] == source_id
    assert result["matches"][0]["session_id"] == str(old["session_id"])
    read = read_page(db, execution, source_id=source_id)
    with db.transaction(readonly=True) as conn:
        assert evidence_in(conn, AUTH, read["citation_id"])["excerpt"] == read["excerpt"]
    organize(db, AUTH, old["session_id"], "delete", 1)
    assert not search(db, execution, query="架构升级")["matches"]
    with pytest.raises(AppError, match="会话"):
        read_page(db, execution, source_id=source_id)


def test_history_never_reads_another_user(db, execution_factory):
    session = create_session(db, AUTH, "C001")
    source_id, _ = settled(db, session["session_id"], "PRIVATE-ARCHITECTURE-872")
    execution = execution_factory("查以前的方案", user="U002")
    assert not search(db, execution, query="PRIVATE-ARCHITECTURE-872")["matches"]
    with pytest.raises(AppError, match="不存在|无权"):
        read_page(db, execution, source_id=source_id)


def test_message_content_and_event_log_are_not_writable(db):
    session = create_session(db, AUTH, "C001")
    source_id, _ = settled(db, session["session_id"], "消息原文不能被覆盖")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.transaction() as conn:
            conn.execute("UPDATE sources SET content='changed' WHERE source_id=%s", (source_id,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.transaction() as conn:
            conn.execute("UPDATE conversation_events SET kind='changed'")


def test_archive_remains_searchable_and_revoke_blocks_reads(db, admin, execution_factory):
    session = create_session(db, AUTH, "C001")
    source_id, _ = settled(db, session["session_id"], "历史方案 ARCH-REVOKE-782")
    execution = execution_factory("找历史")
    organize(db, AUTH, session["session_id"], "archive", 1)
    assert search(db, execution, query="ARCH-REVOKE-782")["matches"]
    admin.execute("DELETE FROM user_company_grants WHERE user_id='U001' AND company_id='C001'")
    assert not search(db, execution, query="ARCH-REVOKE-782")["matches"]
    with pytest.raises(AppError):
        read_page(db, execution, source_id=source_id)


def test_long_history_page_has_stable_continuation(db, execution_factory):
    session = create_session(db, AUTH, "C001")
    for n in range(25):
        settled(db, session["session_id"], f"PAGED-HISTORY-762 {n}")
    execution = execution_factory("找全部旧记录")
    first = search(db, execution, query="PAGED-HISTORY-762")
    second = search(db, execution, query="PAGED-HISTORY-762", cursor=first["next_cursor"])
    assert len(first["matches"]) == 5 and len(second["matches"]) == 5
    assert not (
        {m["source_id"] for m in first["matches"]} & {m["source_id"] for m in second["matches"]}
    )


def test_original_text_can_be_read_beyond_first_excerpt(db, execution_factory):
    session = create_session(db, AUTH, "C001")
    text = "这是原始方案内容。" * 200 + "最后一步：保留回退副本。"
    source_id, _ = settled(db, session["session_id"], text)
    execution = execution_factory("回读原文")
    first = read_page(db, execution, source_id=source_id)
    second = read_page(db, execution, source_id=source_id, char_start=first["next_char_start"])
    assert first["excerpt"] + second["excerpt"] == text


@pytest.mark.asyncio
async def test_agent_can_deliver_separate_history_page(db, settings):
    from test_runtime import ScriptedModel, tool

    from app.runtime import Runtime
    from app.turns import read_turn

    old = create_session(db, AUTH, "C001")
    settled(db, old["session_id"], "方案标记-AGENT-972：先备份，再迁移。")
    session = create_session(db, AUTH, "C001")
    turn = accept_turn(db, AUTH, session["session_id"], uid(), "找之前方案标记-AGENT-972")

    def finish(payload):
        output = json.loads(
            next(m["content"] for m in reversed(payload["messages"]) if m["role"] == "tool")
        )
        return {
            "content": json.dumps(
                {
                    "status": "completed",
                    "blocks": [
                        {
                            "type": "result",
                            "result_ref": output["result_ref"],
                            "view": "history_page",
                        }
                    ],
                }
            )
        }

    runtime = Runtime(
        db,
        model_settings(settings),
        ScriptedModel([tool("search_conversations", {"query": "方案标记-AGENT-972"}), finish]),
    )
    await runtime.run(runtime.claim())
    result = read_turn(db, AUTH, turn["turn_id"])
    assert result["status"] == "completed", result["error"]
    assert result["final_cards"][0]["session_id"] == str(old["session_id"])
    assert result["final_cards"][0]["kind"] == "history_quote"


def test_http_history_routes_are_separate_from_knowledge(db, settings):
    from fastapi.testclient import TestClient

    from app.api import create_app

    session = create_session(db, AUTH, "C001")
    source_id, _ = settled(db, session["session_id"], "HTTP-CONVERSATION-872")
    with TestClient(create_app(settings, db, start_workers=False)) as client:
        assert (
            client.post(
                "/api/v1/conversations/search", json={"query": "HTTP-CONVERSATION-872"}
            ).status_code
            == 401
        )
        assert client.post("/api/v1/demo/login", json={"user_id": "U001"}).status_code == 200
        response = client.post(
            "/api/v1/conversations/search", json={"query": "HTTP-CONVERSATION-872"}
        )
        assert response.status_code == 200, response.text
        citation = response.json()["matches"][0]["citation_id"]
        assert client.get(f"/api/v1/evidence/{citation}").status_code == 200
        assert (
            client.post(
                "/api/v1/retrieval/search",
                json={"corpus": "history", "query": "HTTP-CONVERSATION-872"},
            ).status_code
            == 403
        )
        assert (
            client.post("/api/v1/conversations/read", json={"source_id": source_id}).status_code
            == 200
        )
        organize(db, AUTH, session["session_id"], "delete", 1)
        assert client.get(f"/api/v1/evidence/{citation}").status_code == 404


def test_search_excerpt_contains_match_near_end_of_long_message(db, execution_factory):
    session = create_session(db, AUTH, "C001")
    source_id, _ = settled(
        db, session["session_id"], "不相关背景。" * 300 + "最终方案升级标记-LATE-983：先备份。"
    )
    execution = execution_factory("找最终方案")
    match = search(db, execution, query="LATE-983")["matches"][0]
    assert match["source_id"] == source_id
    assert "LATE-983" in match["excerpt"]
    assert match["locator"]["char_start"] > 1000


def test_failed_turn_user_message_is_preserved_in_next_projection(db, settings):
    from test_runtime import ScriptedModel

    from app.runtime import Runtime

    session = create_session(db, AUTH, "C001")
    failed = accept_turn(
        db, AUTH, session["session_id"], uid(), "昨天方案唯一标记-FAILED-741，尚未执行。"
    )
    with db.transaction() as conn:
        conn.execute("UPDATE turns SET status='failed' WHERE turn_id=%s", (failed["turn_id"],))
        conn.execute(
            "UPDATE work_items SET state='failed' WHERE source_id IN (SELECT source_id FROM sources WHERE turn_id=%s)",
            (failed["turn_id"],),
        )
    accept_turn(db, AUTH, session["session_id"], uid(), "上次那个方案是什么")
    execution = Runtime(db, settings, ScriptedModel([])).claim()
    payload, _, dropped = context.assemble(db, model_settings(settings), execution, [])
    assert not dropped
    assert "FAILED-741" in json.dumps(payload, ensure_ascii=False)
    assert "failed" in json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_disabling_summary_restores_raw_messages_instead_of_losing_both(
    db, settings, execution_factory
):
    from test_runtime import ScriptedModel

    from app.compaction import compact
    from app.conversation_projection import groups

    execution = execution_factory("继续")
    settled(db, execution.session_id, "必须恢复的早期原文-RAW-712")
    with db.transaction() as conn:
        seq = conn.execute(
            "UPDATE users SET next_source_seq=next_source_seq+1 WHERE user_id='U001' RETURNING next_source_seq"
        ).fetchone()["next_source_seq"]
        conn.execute("UPDATE turns SET source_seq=%s WHERE turn_id=%s", (seq, execution.turn_id))
    execution.source_seq = seq

    def summarize(payload):
        data = json.loads(payload["messages"][-1]["content"])
        return {
            "content": json.dumps({"summary": "历史导航", "covered_sources": data["source_ids"]})
        }

    await compact(
        db,
        model_settings(settings),
        ScriptedModel([summarize]),
        execution,
        groups(db, execution),
        1,
    )
    config = model_settings(settings).model_copy(update={"context_use_summary": False})
    payload, manifest, _ = context.assemble(db, config, execution, [])
    assert manifest["summary_id"] is None
    assert any(m.get("content") == "必须恢复的早期原文-RAW-712" for m in payload["messages"])


def test_projection_reads_authoritative_source_not_mutable_turn_fields(
    db, admin, settings, execution_factory
):
    execution = execution_factory("继续")
    _, assistant_id = settled(
        db, execution.session_id, "原始用户方案-ORIGINAL-712", "真实助手建议-ANSWER-712"
    )
    admin.execute(
        "UPDATE turns SET text='被改写的缓存文字',final_message='错误缓存回复' WHERE final_response_ref=%s",
        (assistant_id,),
    )
    with db.transaction() as conn:
        seq = conn.execute(
            "UPDATE users SET next_source_seq=next_source_seq+1 WHERE user_id='U001' RETURNING next_source_seq"
        ).fetchone()["next_source_seq"]
        conn.execute("UPDATE turns SET source_seq=%s WHERE turn_id=%s", (seq, execution.turn_id))
    execution.source_seq = seq
    payload, _, _ = context.assemble(db, model_settings(settings), execution, [])
    text = json.dumps(payload, ensure_ascii=False)
    assert "ORIGINAL-712" in text and "ANSWER-712" in text
    assert "错误缓存回复" not in text and "被改写的缓存文字" not in text


@pytest.mark.asyncio
async def test_multiple_compactions_keep_old_sources_readable(db, settings, execution_factory):
    from test_runtime import ScriptedModel

    from app.compaction import compact
    from app.conversation_projection import groups

    execution = execution_factory("继续回忆")
    ids = [
        settled(db, execution.session_id, f"旧方案事实编号-ROUND-{n}，禁止自动保存。")
        for n in range(20)
    ]
    with db.transaction() as conn:
        seq = conn.execute(
            "UPDATE users SET next_source_seq=next_source_seq+1 WHERE user_id='U001' RETURNING next_source_seq"
        ).fetchone()["next_source_seq"]
        conn.execute("UPDATE turns SET source_seq=%s WHERE turn_id=%s", (seq, execution.turn_id))
    execution.source_seq = seq

    def summarize(payload):
        data = json.loads(payload["messages"][-1]["content"])
        return {
            "content": json.dumps(
                {
                    "summary": "旧方案未授权自动保存，细节请回读原文。",
                    "covered_sources": data["source_ids"],
                }
            )
        }

    raw = groups(db, execution)
    await compact(db, model_settings(settings), ScriptedModel([summarize]), execution, raw[:10], 1)
    first, manifest, _ = context.assemble(db, model_settings(settings), execution, [])
    assert manifest["raw_group_count"] == 10
    assert manifest["summary_id"]
    await compact(db, model_settings(settings), ScriptedModel([summarize]), execution, raw[10:], 1)
    _, manifest, _ = context.assemble(db, model_settings(settings), execution, [])
    assert manifest["raw_group_count"] == 5
    assert read_page(db, execution, source_id=ids[0][0])["excerpt"].startswith(
        "旧方案事实编号-ROUND-0"
    )


def test_historical_tool_ids_are_namespaced_across_runs(db, settings, execution_factory):
    from app.events import emit
    from app.values import canonical, jsonb

    execution = execution_factory("只回忆旧工具过程")
    for n in range(2):
        user_id, assistant_id = settled(db, execution.session_id, f"旧工具回合{n}")
        with db.transaction() as conn:
            turn = conn.execute(
                "SELECT * FROM turns WHERE final_response_ref=%s", (assistant_id,)
            ).fetchone()
            run_id = uid()
            conn.execute(
                "INSERT INTO runs(run_id,turn_id,generation,state,final_source_id) VALUES(%s,%s,1,'completed',%s)",
                (run_id, turn["turn_id"], assistant_id),
            )
            tool_source_id = uid()
            body = {
                "ok": True,
                "result_ref": tool_source_id,
                "data": {"kind": "synthetic_protocol_only"},
            }
            add_source(
                conn,
                AUTH,
                body,
                role="tool",
                kind="tool_result",
                source_id=tool_source_id,
                turn_id=turn["turn_id"],
                source_seq=turn["source_seq"],
                companies=["C001"],
            )
            conn.execute(
                "INSERT INTO tool_executions(run_id,tool_call_id,name,arguments,arguments_hash,state,result_source_id) VALUES(%s,'call_1','find_invoices',%s,'fixture','done',%s)",
                (run_id, jsonb({"company_id": "C001"}), tool_source_id),
            )
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "find_invoices",
                            "arguments": canonical({"company_id": "C001"}),
                        },
                    }
                ],
            }
            emit(conn, str(turn["turn_id"]), run_id, "model_finished", {"message": message})
    with db.transaction() as conn:
        seq = conn.execute(
            "UPDATE users SET next_source_seq=next_source_seq+1 WHERE user_id='U001' RETURNING next_source_seq"
        ).fetchone()["next_source_seq"]
        conn.execute("UPDATE turns SET source_seq=%s WHERE turn_id=%s", (seq, execution.turn_id))
    execution.source_seq = seq
    config = model_settings(settings).model_copy(
        update={"input_budget": 100000, "model_context_limit": 200000}
    )
    payload, _, _ = context.assemble(db, config, execution, [])
    ids = [c["id"] for m in payload["messages"] for c in m.get("tool_calls", [])]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert all(i.startswith("history_") for i in ids)


def test_corrupt_historical_source_is_not_silently_omitted(db, admin, settings, execution_factory):
    execution = execution_factory("继续")
    source_id, _ = settled(db, execution.session_id, "不可静默略过的原文-VERIFY-321")
    with db.transaction() as conn:
        seq = conn.execute(
            "UPDATE users SET next_source_seq=next_source_seq+1 WHERE user_id='U001' RETURNING next_source_seq"
        ).fetchone()["next_source_seq"]
        conn.execute("UPDATE turns SET source_seq=%s WHERE turn_id=%s", (seq, execution.turn_id))
    execution.source_seq = seq
    with admin.transaction():
        admin.execute("ALTER TABLE sources DISABLE TRIGGER conversation_content")
        try:
            admin.execute(
                "UPDATE sources SET content='损坏的原文' WHERE source_id=%s", (source_id,)
            )
        finally:
            admin.execute("ALTER TABLE sources ENABLE TRIGGER conversation_content")
    with pytest.raises(AppError) as error:
        context.assemble(db, model_settings(settings), execution, [])
    assert error.value.code == "STATE_SOURCE_UNAVAILABLE"


def test_history_citation_only_resolves_actual_served_evidence(db, settings, execution_factory):
    from test_answer_renderer import recorded, rendered

    old = create_session(db, AUTH, "C001")
    source_id, _ = settled(db, old["session_id"], "真实引用唯一标记-CITE-931")
    ex = execution_factory("找回CITE-931原话")
    output, payload, manifest = recorded(
        db, settings, ex, "search_conversations", {"query": "CITE-931"}
    )
    citation = output["data"]["matches"][0]["citation_id"]
    reply = rendered(
        db, ex, payload, manifest, [{"type": "history_quote", "citation_id": citation}]
    )
    assert reply.cards[0]["source_id"] == source_id
    with pytest.raises(AppError):
        rendered(db, ex, payload, manifest, [{"type": "history_quote", "citation_id": uid()}])
