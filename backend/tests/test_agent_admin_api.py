import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_runtime import ScriptedModel, final, model_settings, tool

from app.api import create_app
from app.model_connection import ModelConnection

BASE = "http://127.0.0.1"
PREFIX = "/api/v1/agent-admin"


def client_for(db, settings):
    app = create_app(settings, db=db, model=ScriptedModel([]), start_workers=False)
    return TestClient(app, base_url=BASE, client=("127.0.0.1", 1234))


def test_management_requires_local_origin_and_manager(db, settings):
    with client_for(db, settings) as client:
        assert client.get(PREFIX + "/agents").status_code == 401
        assert client.post("/api/v1/demo/login", json={"user_id": "U002"}).status_code == 200
        assert client.get(PREFIX + "/agents").status_code == 403
        assert client.post("/api/v1/demo/login", json={"user_id": "U001"}).status_code == 200
        result = client.get(PREFIX + "/agents")
        assert result.status_code == 200 and result.json()["runtime_ready"] is True
        draft = {"name": "测试助手", "model_profile_id": "default"}
        assert client.post(PREFIX + "/agents", json=draft).status_code == 403
        assert (
            client.post(
                PREFIX + "/agents", json=draft, headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        created = client.post(PREFIX + "/agents", json=draft, headers={"Origin": BASE})
        assert created.status_code == 201
        agent_id = created.json()["agent_id"]
        published = client.post(
            PREFIX + f"/agents/{agent_id}/publish",
            json={"expected_revision": 1},
            headers={"Origin": BASE},
        )
        assert published.status_code == 200 and published.json()["current_version"] == 1


def test_model_endpoint_never_echoes_credentials(db, settings, monkeypatch):
    async def probe(self, form):
        self.current = (
            SimpleNamespace(
                model_configured=True,
                model_id=form.model_id,
                model_base_url=form.base_url,
                model_api_key=form.api_key.get_secret_value(),
            ),
            object(),
        )

    monkeypatch.setattr(ModelConnection, "test_and_enable", probe)
    monkeypatch.setattr(
        ModelConnection,
        "status",
        lambda self: {"configured": True, "model_id": self.current[0].model_id},
    )
    with client_for(db, settings) as client:
        client.post("/api/v1/demo/login", json={"user_id": "U001"})
        body = {
            "name": "模型 A",
            "expected_version": 0,
            "connection": {
                "base_url": "https://provider.example/v1",
                "model_id": "model-a",
                "api_key": "fixture-secret",
            },
        }
        response = client.post(PREFIX + "/models/a/test", json=body, headers={"Origin": BASE})
        assert response.status_code == 200 and "fixture-secret" not in response.text
        listed = client.get(PREFIX + "/models")
        assert listed.status_code == 200 and "fixture-secret" not in listed.text
        body["connection"]["api_key"] = {"wrong": "fixture-secret"}
        bad = client.post(PREFIX + "/models/a/test", json=body, headers={"Origin": BASE})
        assert bad.status_code == 422 and "fixture-secret" not in bad.text


def test_original_conversation_api_runs_selected_agent(db, settings):
    model = ScriptedModel(
        [
            tool("query_month", {"company_id": "C001", "period": "2026-09"}),
            final("查询完成，三项金额分别列在结果中。"),
        ]
    )
    app = create_app(model_settings(settings), db=db, model=model, start_workers=False)
    with TestClient(app, base_url=BASE, client=("127.0.0.1", 1234)) as client:
        client.post("/api/v1/demo/login", json={"user_id": "U001"})
        draft = {
            "name": "账务助手",
            "prompt": "核对目标",
            "model_profile_id": "default",
            "knowledge_mode": "none",
            "tool_grants": {"query_month": ["invoke"], "resolve_scope": ["invoke"]},
        }
        created = client.post(PREFIX + "/agents", json=draft, headers={"Origin": BASE}).json()
        agent_id = created["agent_id"]
        published = client.post(
            PREFIX + f"/agents/{agent_id}/publish",
            json={"expected_revision": 1},
            headers={"Origin": BASE},
        ).json()
        enabled = client.post(
            PREFIX + f"/agents/{agent_id}/enabled",
            json={"expected_revision": published["revision"], "enabled": True},
            headers={"Origin": BASE},
        )
        assert enabled.status_code == 200
        assert any(a["agent_id"] == agent_id for a in client.get("/api/v1/agents").json())
        session = client.post("/api/v1/sessions", json={"selected_company_id": "C001"}).json()
        accepted = client.post(
            f"/api/v1/sessions/{session['session_id']}/turns",
            json={
                "client_request_id": "agent-api-1",
                "text": "查询甲企业2026-09月度资料",
                "agent_id": agent_id,
            },
        )
        assert accepted.status_code == 202
        execution = app.state.runtime.claim()
        assert execution is not None
        asyncio.run(app.state.runtime.run(execution))
        result = client.get(f"/api/v1/turns/{accepted.json()['turn_id']}").json()
        assert (
            result["status"] == "completed"
            and result["final_cards"][0]["invoice_total"] == "9622.06"
        )
        assert "核对目标" in model.requests[0]["messages"][0]["content"]
