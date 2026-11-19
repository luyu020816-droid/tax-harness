"""Persistent memory, versioned knowledge policy, and scoped context integration."""

import pytest
from psycopg.errors import InsufficientPrivilege
from test_knowledge import knowledge as knowledge
from test_knowledge import uploaded
from test_runtime import ScriptedModel, final, model_settings

from app import compaction_requests, preferences, turns
from app.agent_preview import configuration
from app.agent_profiles import AgentDraft, create_draft, publish, set_enabled
from app.context_preview import preview
from app.errors import AppError
from app.identity import Identity, create_session
from app.knowledge_strategy import Strategy, read_in
from app.knowledge_strategy import publish as publish_strategy
from app.memory import load_core
from app.runtime import Runtime
from app.values import uid

AUTH = Identity("U001")


def agent(db, name):
    draft = create_draft(
        db, AUTH, AgentDraft(name=name, prompt="核对当前授权", model_profile_id="default")
    )
    draft = publish(db, AUTH, draft["agent_id"], draft["revision"])
    return set_enabled(db, AUTH, draft["agent_id"], draft["revision"], True)


def test_memory_history_keeps_tombstones_but_never_recalls_old_values(db, settings):
    import json
    import os
    import subprocess
    import sys

    from app.config import ROOT

    first = preferences.change_preference(
        db, AUTH, action="remember", key="reply_style", value="请简洁", request_id=uid()
    )
    changed = preferences.change_preference(
        db,
        AUTH,
        action="update",
        target_id=str(first["preference_id"]),
        expected_revision=1,
        value="请详细",
        request_id=uid(),
    )
    # A fresh interpreter must read the committed state, not a process cache.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,json; from app.db import Database; from app.identity import Identity; from app.preferences import list_preferences; d=Database(os.environ['MEMORY_TEST_URL']); d.open(); print(json.dumps([p['value'] for p in list_preferences(d,Identity('U001'))],ensure_ascii=True)); d.close()",
        ],
        env={
            **os.environ,
            "MEMORY_TEST_URL": settings.test_database_url,
            "PYTHONPATH": str(ROOT / "backend"),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert json.loads(result.stdout) == ["请详细"]
    preferences.change_preference(
        db,
        AUTH,
        action="delete",
        target_id=str(first["preference_id"]),
        expected_revision=changed["revision"],
        request_id=uid(),
    )
    page = preferences.history(db, AUTH, str(first["preference_id"]), limit=1)
    assert page["current_status"] == "deleted" and page["revisions"][0]["value"] is None
    assert page["next_revision"] == 3
    older = preferences.history(db, AUTH, str(first["preference_id"]), before_revision=3)
    assert [r["value"] for r in older["revisions"]] == ["请详细", "请简洁"]
    session = create_session(db, AUTH, "C001")
    assert load_core(db, AUTH, str(session["session_id"]))["preferences"] == []
    with pytest.raises(AppError):
        preferences.history(db, Identity("U002"), str(first["preference_id"]))


def test_strategy_version_conflict_permissions_and_immutable_history(db):
    assert db.ready()
    with db.transaction(readonly=True) as conn:
        assert read_in(conn, AUTH, "C001")["revision"] == 0
    first = publish_strategy(db, AUTH, "C001", 0, Strategy(evidence_budget=512, max_groups=1))
    assert first["revision"] == 1
    with pytest.raises(AppError, match="已变化"):
        publish_strategy(db, AUTH, "C001", 0, Strategy())
    with pytest.raises(AppError):
        publish_strategy(db, Identity("U002"), "C001", 1, Strategy())
    second = publish_strategy(db, AUTH, "C001", 1, Strategy())
    assert second["revision"] == 2
    with db.transaction() as conn, pytest.raises(InsufficientPrivilege):
        conn.execute("UPDATE knowledge_strategy_versions SET config='{}' WHERE company_id='C001'")


def test_missing_strategy_version_never_silently_enables_defaults(db):
    with db.transaction() as conn:
        conn.execute("INSERT INTO knowledge_strategies(company_id,revision) VALUES('C001',1)")
    with db.transaction(readonly=True) as conn, pytest.raises(AppError, match="版本缺失"):
        read_in(conn, AUTH, "C001")


def test_strategy_disable_revokes_old_citations_and_strict_mode_fails_closed(knowledge, db):
    uploaded(knowledge)
    _, _, retrieval = knowledge
    result = retrieval.search(AUTH, company_id="C001", query="发票用途")
    assert result["evidence"]
    publish_strategy(db, AUTH, "C001", 0, Strategy(enabled=False))
    with pytest.raises(AppError, match="策略已变化"):
        retrieval.read(AUTH, result["evidence"][0]["citation_id"])
    blocked = retrieval.search(AUTH, company_id="C001", query="发票用途")
    assert blocked["status"] == "unavailable" and blocked["evidence"] == []
    publish_strategy(db, AUTH, "C001", 1, Strategy(rerank=False, require_calibrated=True))
    blocked = retrieval.search(AUTH, company_id="C001", query="发票用途")
    assert blocked["status"] == "unavailable" and blocked["evidence"] == []
    assert blocked["scope_snapshot"]["strategy"]["revision"] == 2


@pytest.mark.asyncio
async def test_restricted_agent_compacts_and_other_agents_cannot_reuse_summary(db, settings):
    settings = model_settings(settings).model_copy(update={"input_budget": None})
    first, second = agent(db, "私有摘要甲"), agent(db, "私有摘要乙")
    session = str(create_session(db, AUTH, "C001")["session_id"])
    model = ScriptedModel([final("已记录。") for _ in range(7)], auto_summary=True)
    runtime = Runtime(db, settings, model, agent_runtime_enabled=True)
    for n in range(7):
        turn = turns.accept_turn(
            db,
            AUTH,
            session,
            uid(),
            f"第{n}轮：" + "保留项目背景。" * 100,
            agent_id=first["agent_id"],
        )
        await runtime.run(runtime.claim())
        assert turns.read_turn(db, AUTH, turn["turn_id"])["status"] == "completed"
    request = compaction_requests.accept(
        db, AUTH, session, uid(), settings, agent_id=first["agent_id"]
    )
    await runtime.run(runtime.claim())
    result = compaction_requests.read(db, AUTH, session, request["turn_id"])
    assert result["result"]["outcome"] == "succeeded", result
    with db.transaction(readonly=True) as conn:
        one, two = configuration(conn, first["agent_id"]), configuration(conn, second["agent_id"])
    own = preview(db, settings, AUTH, session, "继续", binding=one)
    other = preview(db, settings, AUTH, session, "继续", binding=two)
    legacy = preview(db, settings, AUTH, session, "继续")
    assert own["summary_id"] == result["result"]["summary_id"]
    assert other["summary_id"] is None and legacy["summary_id"] is None
    assert own["tool_count"] == 0
    with db.transaction(readonly=True) as conn:
        assert conn.execute(
            "SELECT agent_scope FROM summaries WHERE summary_id=%s", (own["summary_id"],)
        ).fetchone()["agent_scope"]
    assert len(turns.session_messages(db, AUTH, session)["turns"]) == 7

    # Guessing a protected source from another Agent's summary must not grant access.
    with db.transaction(readonly=True) as conn:
        from app.summary_evidence import unpack

        summary = conn.execute(
            "SELECT content FROM summaries WHERE summary_id=%s", (own["summary_id"],)
        ).fetchone()
        excerpt = unpack(summary["content"])["protected_excerpts"][0]
    turns.accept_turn(db, AUTH, session, uid(), "核对原文", agent_id=second["agent_id"])
    other_execution = runtime.claim()
    with pytest.raises(AppError, match="没有可用摘要"):
        runtime.tools.history.read_summary(other_execution, excerpt["source_id"], excerpt["quote"])


def test_strategy_and_memory_api_enforce_origin_owner_and_revision(db, settings):
    from fastapi.testclient import TestClient

    from app.api import create_app

    app = create_app(model_settings(settings), db=db, model=ScriptedModel([]), start_workers=False)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as client:
        client.post("/api/v1/demo/login", json={"user_id": "U001"})
        profile = client.get("/api/v1/knowledge/strategies/C001").json()
        assert profile["can_manage"] and profile["revision"] == 0
        payload = {"expected_revision": 0, "config": Strategy().model_dump()}
        assert client.put("/api/v1/knowledge/strategies/C001", json=payload).status_code == 403
        assert (
            client.put(
                "/api/v1/knowledge/strategies/C001",
                json=payload,
                headers={"Origin": "http://127.0.0.1"},
            ).status_code
            == 200
        )
        assert (
            client.put(
                "/api/v1/knowledge/strategies/C001",
                json=payload,
                headers={"Origin": "http://127.0.0.1"},
            ).status_code
            == 409
        )
        pref = client.post(
            "/api/v1/preferences", json={"key": "audit_test", "value": "简洁", "request_id": uid()}
        ).json()
        history_url = f"/api/v1/preferences/{pref['preference_id']}/history"
        assert client.get(history_url).json()["revisions"][0]["value"] == "简洁"
        assert client.get(history_url + "?before_revision=0").status_code == 422
        assert client.get(history_url + "?before_revision=999999999999999").status_code == 422
        client.post("/api/v1/demo/login", json={"user_id": "U002"})
        assert not client.get("/api/v1/knowledge/strategies/C001").json()["can_manage"]
        payload["expected_revision"] = 1
        assert (
            client.put(
                "/api/v1/knowledge/strategies/C001",
                json=payload,
                headers={"Origin": "http://127.0.0.1"},
            ).status_code
            == 403
        )
        assert client.get(history_url).status_code == 404
