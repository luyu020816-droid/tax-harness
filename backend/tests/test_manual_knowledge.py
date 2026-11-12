"""Product boundary tests: a recorded conversation is not a knowledge submission."""

import json

import pytest
from fastapi.testclient import TestClient
from test_compaction import closed_group, summary_message
from test_knowledge import knowledge as knowledge_fixture
from test_runtime import ScriptedModel, model_settings

from app.api import create_app
from app.compaction import compact
from app.context import assemble
from app.errors import AppError
from app.history_jobs import HistoryJobs
from app.history_retrieval import HistoryRetrieval
from app.identity import Identity, create_session
from app.session_management import history, organize
from app.summary_evidence import collect
from app.tools import ToolExecutor
from app.turns import accept_turn, session_messages
from app.values import uid

AUTH = Identity("U001")


def test_prepared_draft_cannot_be_hidden_by_acknowledgement(db, settings, execution_factory):
    from test_answer_renderer import recorded, rendered

    ex = execution_factory("把以下文字加入甲企业知识库，先准备草稿：需要书面批准。")
    output, payload, manifest = recorded(
        db,
        settings,
        ex,
        "prepare_knowledge_text",
        {"company_id": "C001", "title": "审批", "content": "需要书面批准。"},
    )
    with pytest.raises(AppError) as error:
        rendered(db, ex, payload, manifest, [{"type": "explanation", "code": "acknowledged"}])
    assert error.value.code == "REPLY_EVIDENCE_MISSING"
    assert error.value.details["results"][0]["view"] == "knowledge_draft"
    reply = rendered(
        db,
        ex,
        payload,
        manifest,
        [{"type": "result", "result_ref": output["result_ref"], "view": "knowledge_draft"}],
    )
    assert reply.cards[0]["kind"] == "knowledge_draft"


@pytest.fixture
def indexed_knowledge(db, settings, admin):
    return knowledge_fixture.__wrapped__(db, settings, admin)


def test_chat_is_recorded_without_any_retrieval_index(db, admin):
    session = create_session(db, AUTH, "C001")
    turn = accept_turn(db, AUTH, session["session_id"], uid(), "未提交知识的唯一标记-KNOW-8173")
    assert (
        session_messages(db, AUTH, session["session_id"])["turns"][0]["turn_id"] == turn["turn_id"]
    )
    for table in (
        "knowledge_documents",
        "history_index",
        "history_jobs",
        "history_passages",
        "history_embeddings",
    ):
        assert admin.execute(f"SELECT count(*) n FROM {table}").fetchone()["n"] == 0


def test_legacy_flags_cannot_enable_chat_index_or_search(db, settings):
    config = settings.model_copy(update={"history_enabled": True, "context_history_search": True})
    jobs = HistoryJobs(db, config, None)
    assert jobs.claim() is None
    for action in (
        lambda: jobs.run({}),
        jobs.backfill,
        lambda: HistoryRetrieval(db, config, None).search(AUTH, query="test"),
    ):
        with pytest.raises(AppError) as error:
            action()
        assert error.value.code == "HISTORY_RETRIEVAL_DISABLED"


def test_http_history_is_denied_and_text_requires_manual_submission(db, settings, admin):
    with TestClient(
        create_app(settings, db=db, model=ScriptedModel([]), start_workers=False)
    ) as client:
        assert client.post("/api/v1/knowledge/text", json={}).status_code == 401
        client.post("/api/v1/demo/login", json={"user_id": "U001"})
        response = client.post(
            "/api/v1/retrieval/search", json={"corpus": "history", "query": "旧聊天"}
        )
        assert response.status_code == 403
        assert response.json()["code"] == "HISTORY_RETRIEVAL_DISABLED"
        payload = {
            "company_id": "C001",
            "title": "手动文字",
            "content": "手动提交正文-KNOW-2187",
            "request_id": uid(),
        }
        first = client.post("/api/v1/knowledge/text", json=payload)
        assert first.status_code == 202, first.text
        second = client.post("/api/v1/knowledge/text", json=payload)
        assert second.status_code == 202
        assert first.json()["document_id"] == second.json()["document_id"]
        assert admin.execute("SELECT count(*) n FROM knowledge_documents").fetchone()["n"] == 1
        assert (
            admin.execute("SELECT submission_kind FROM knowledge_files").fetchone()[
                "submission_kind"
            ]
            == "manual_text"
        )
        assert admin.execute("SELECT count(*) n FROM history_index").fetchone()["n"] == 0
        changed = client.post("/api/v1/knowledge/text", json={**payload, "content": "不同内容"})
        assert changed.status_code == 409
        client.post("/api/v1/demo/login", json={"user_id": "U002"})
        assert (
            client.post("/api/v1/knowledge/text", json={**payload, "request_id": uid()}).status_code
            == 403
        )


def test_draft_is_not_a_document_and_tools_do_not_expose_history(
    db, settings, admin, execution_factory
):
    execution = execution_factory("把下面这段加入知识库：特殊审批需要书面记录")
    result, _ = ToolExecutor(db).execute(
        execution,
        {
            "id": "draft-1",
            "function": {
                "name": "prepare_knowledge_text",
                "arguments": json.dumps(
                    {"company_id": "C001", "title": "审批", "content": "特殊审批需要书面记录"}
                ),
            },
        },
    )
    assert result["ok"] and not result["data"]["submitted"]
    assert admin.execute("SELECT count(*) n FROM knowledge_documents").fetchone()["n"] == 0
    payload, _, _ = assemble(db, model_settings(settings), execution, [])
    names = {t["function"]["name"] for t in payload["tools"]}
    assert "prepare_knowledge_text" in names and "search_knowledge" in names
    assert not {"search_history", "read_history"} & names


@pytest.mark.asyncio
async def test_summary_is_only_loaded_in_its_own_session(db, settings, admin, execution_factory):
    ex = execution_factory("本会话摘要唯一事实-SUM-7159。" + "背景信息不授权任何修改。" * 50)
    source = admin.execute("SELECT * FROM sources WHERE source_id=%s", (ex.source_id,)).fetchone()
    summary = summary_message(source)
    summary["content"] = json.dumps(
        {"summary": "本会话摘要唯一事实-SUM-7159。", "covered_sources": [str(source["source_id"])]},
        ensure_ascii=False,
    )
    assert await compact(
        db,
        model_settings(settings),
        ScriptedModel([summary]),
        ex,
        [closed_group(db, source)],
        1,
    )
    a, manifest, _ = assemble(db, model_settings(settings), ex, [])
    assert manifest["summary_id"]
    assert "SUM-7159" in json.dumps(a, ensure_ascii=False)
    other = execution_factory("新会话不应知道旧内容")
    b, manifest_b, _ = assemble(db, model_settings(settings), other, [])
    assert not manifest_b["summary_id"]
    assert "SUM-7159" not in json.dumps(b, ensure_ascii=False)
    with db.transaction(readonly=True) as conn, pytest.raises(AppError) as error:
        collect(conn, other, [{"source_id": ex.source_id, "revision": 1}], 3000)
    assert error.value.code == "SUMMARY_INVALID"


def test_session_organization_is_owned_versioned_and_preserves_documents(
    db, admin, indexed_knowledge
):
    from test_knowledge import uploaded

    document = uploaded(indexed_knowledge)
    a = create_session(db, AUTH, "C001")
    b = create_session(db, AUTH, "C001")
    a_id = str(a["session_id"])
    organize(db, AUTH, a_id, "pin", 1)
    assert str(history(db, AUTH)["sessions"][0]["session_id"]) == a_id
    with pytest.raises(AppError) as error:
        organize(db, AUTH, a_id, "archive", 1)
    assert error.value.code == "VERSION_CONFLICT"
    organize(db, AUTH, a_id, "archive", 2)
    assert a_id not in [str(s["session_id"]) for s in history(db, AUTH)["sessions"]]
    assert len(history(db, AUTH, True)["sessions"]) == 1
    with pytest.raises(AppError):
        accept_turn(db, AUTH, a_id, uid(), "归档不能发送")
    organize(db, AUTH, a_id, "unarchive", 3)
    with pytest.raises(AppError):
        organize(db, Identity("U002"), a_id, "delete", 4)
    organize(db, AUTH, a_id, "delete", 4)
    with pytest.raises(AppError):
        session_messages(db, AUTH, a_id)
    assert admin.execute(
        "SELECT active_version_id FROM knowledge_documents WHERE document_id=%s",
        (document["document_id"],),
    ).fetchone()["active_version_id"]
    assert len(history(db, AUTH)["sessions"]) == 1
    assert str(history(db, AUTH)["sessions"][0]["session_id"]) == str(b["session_id"])


def test_running_session_cannot_be_archived_or_deleted(db, execution_factory):
    ex = execution_factory("任务运行中")
    for action in ("archive", "delete"):
        with pytest.raises(AppError) as error:
            organize(db, AUTH, ex.session_id, action, 1)
        assert error.value.code == "SESSION_BUSY"


def test_history_list_is_paged_beyond_recent_limit(db):
    for _ in range(53):
        create_session(db, AUTH, "C001")
    first = history(db, AUTH)
    second = history(db, AUTH, offset=first["next_offset"])
    assert len(first["sessions"]) == 50 and len(second["sessions"]) == 3
    assert not {s["session_id"] for s in first["sessions"]} & {
        s["session_id"] for s in second["sessions"]
    }


def test_manually_submitted_text_is_indexed_and_available_in_new_session(
    db, settings, indexed_knowledge, admin
):
    store, jobs, retrieval = indexed_knowledge
    with TestClient(
        create_app(settings, db=db, model=ScriptedModel([]), start_workers=False)
    ) as client:
        client.post("/api/v1/demo/login", json={"user_id": "U001"})
        response = client.post(
            "/api/v1/knowledge/text",
            json={
                "company_id": "C001",
                "title": "住宿审批",
                "content": "普通住宿每晚480元，会展期间650元但需财务负责人书面批准。",
                "request_id": uid(),
            },
        )
        assert response.status_code == 202
        document = response.json()
        jobs.run(jobs.claim())
        a = create_session(db, AUTH, "C001")
        b = create_session(db, AUTH, "C001")
        for session in (a, b):
            bundle = retrieval.search(
                AUTH, company_id="C001", query="住宿审批", session_id=str(session["session_id"])
            )
            assert bundle["status"] == "ok"
            assert bundle["evidence"][0]["document_id"] == document["document_id"]
            assert "书面批准" in bundle["evidence"][0]["excerpt"]
        version = store.get(AUTH, document["document_id"])
        assert (
            client.post(
                f"/api/v1/knowledge/documents/{document['document_id']}/disable",
                json={"request_id": uid(), "expected_document_revision": version["revision"]},
            ).status_code
            == 200
        )
        assert not retrieval.search(
            AUTH, company_id="C001", query="住宿审批", session_id=str(b["session_id"])
        )["evidence"]
        assert admin.execute("SELECT count(*) n FROM history_index").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_draft_card_is_delivered_by_runtime_without_publishing(db, settings, admin):
    from test_runtime import tool

    from app.runtime import Runtime
    from app.turns import read_turn

    def final_draft(payload):
        output = json.loads(
            next(m["content"] for m in reversed(payload["messages"]) if m["role"] == "tool")
        )
        return {
            "content": json.dumps(
                {
                    "blocks": [
                        {
                            "type": "result",
                            "result_ref": output["result_ref"],
                            "view": "knowledge_draft",
                        }
                    ],
                    "status": "completed",
                }
            )
        }

    model = ScriptedModel(
        [
            tool(
                "prepare_knowledge_text",
                {
                    "company_id": "C001",
                    "title": "测试入库",
                    "content": "这是等待本人提交的知识正文。",
                },
            ),
            final_draft,
        ]
    )
    runtime = Runtime(db, model_settings(settings), model)
    session = create_session(db, AUTH, "C001")
    turn = accept_turn(
        db,
        AUTH,
        str(session["session_id"]),
        uid(),
        "把这段加入知识库：这是等待本人提交的知识正文。",
    )
    await runtime.run(runtime.claim())
    result = read_turn(db, AUTH, turn["turn_id"])
    assert result["status"] == "completed", result["error"]
    assert result["final_cards"][0]["kind"] == "knowledge_draft"
    assert result["final_cards"][0]["draft_request_id"].startswith("draft:")
    assert admin.execute("SELECT count(*) n FROM knowledge_documents").fetchone()["n"] == 0
