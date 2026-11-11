import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_judge_boundaries import raw_final
from test_runtime import model_settings, tool

from app.api import create_app
from app.config import Settings
from app.errors import AppError
from app.identity import Identity, create_session
from app.model import HttpModel
from app.model_connection import ConnectionInput, ModelConnection
from app.runtime import Runtime
from app.turns import accept_turn, read_turn
from app.values import uid

LOCAL = "http://127.0.0.1"
URL = "/api/v1/model-connection"
FORM = {
    "base_url": "https://api.example.test/v1",
    "model_id": "page-model",
    "api_key": "fixture-key",
}


def make_app(*, configured=True):
    settings = Settings(
        _env_file=None,
        model_base_url="https://env.example.test/v1" if configured else "",
        model_id="env-model" if configured else "",
        model_api_key="env-fixture-key" if configured else "",
        model_context_limit=32000 if configured else 0,
        model_extra_body={"temperature": 0.1},
        database_url="dbname=local_demo",
        ingest_database_url="",
    )
    # A connection probe must never touch the application database.
    return create_app(settings, db=SimpleNamespace(ready=lambda: False), start_workers=False)


def browser(app, client_host="127.0.0.1", origin=LOCAL):
    return TestClient(app, base_url=origin, client=(client_host, 12345))


def probe_reply():
    return {
        "message": {
            "tool_calls": [
                {"function": {"name": "connection_probe", "arguments": '{"value":"ok"}'}}
            ]
        }
    }


def test_configured_env_can_be_tested_without_sending_its_key(monkeypatch):
    async def complete(self, payload):
        assert self.settings.model_api_key == "env-fixture-key"
        assert payload["model"] == "env-model" and payload["max_tokens"] == 128
        assert payload["temperature"] == 0.1
        assert [tool["function"]["name"] for tool in payload["tools"]] == ["connection_probe"]
        return probe_reply()

    monkeypatch.setattr(HttpModel, "complete", complete)
    with browser(make_app()) as client:
        assert client.get(URL).json()["test_status"] == "untested"
        result = client.post(URL + "/test", json={}, headers={"Origin": LOCAL})
        assert result.status_code == 200
        assert result.json() == {
            "configured": True,
            "source": "env",
            "model_id": "env-model",
            "test_status": "success",
        }
        assert "env-fixture-key" not in result.text


def test_successful_page_probe_switches_ready_and_retains_prior_snapshot(monkeypatch):
    async def complete(self, payload):
        assert self.settings.model_api_key == FORM["api_key"]
        assert payload["model"] == "page-model"
        assert "temperature" not in payload  # Do not forward another provider's settings.
        return probe_reply()

    monkeypatch.setattr(HttpModel, "complete", complete)
    app = make_app(configured=False)
    with browser(app) as client:
        runtime = app.state.runtime
        running = SimpleNamespace(model_configuration=runtime._model_for())
        assert client.get("/api/v1/ready").json()["model"] is False
        result = client.post(URL + "/test", json=FORM, headers={"Origin": LOCAL})
        assert result.status_code == 200
        assert result.json()["source"] == "page"
        assert FORM["api_key"] not in result.text and FORM["api_key"] not in client.get(URL).text
        assert client.get("/api/v1/ready").json()["model"] is True
        active, adapter = runtime._model_for()
        assert active.model_id == "page-model" and adapter.settings is active
        assert active.context_budget <= 16000
        assert active.context_limits["window_status"] == "unknown"
        assert runtime._model_for(running)[0].model_id == ""
        assert app.state.settings.model_api_key == ""

    with browser(make_app(configured=False)) as restarted:
        assert restarted.get(URL).json()["test_status"] == "unconfigured"


@pytest.mark.parametrize(
    "reason,wanted",
    [
        ("auth", "MODEL_AUTH_FAILED"),
        ("missing", "MODEL_NOT_FOUND"),
        ("no_tool", "MODEL_TOOL_UNSUPPORTED"),
    ],
)
def test_failed_probe_does_not_replace_active_configuration(monkeypatch, reason, wanted):
    async def complete(self, payload):
        if reason in ("auth", "missing"):
            raise AppError(
                "MODEL_UNAVAILABLE",
                "provider failed",
                503,
                details={"http_status": 401 if reason == "auth" else 404},
            )
        return {"message": {"content": "OK"}}

    monkeypatch.setattr(HttpModel, "complete", complete)
    app = make_app()
    with browser(app) as client:
        original = app.state.runtime._model_for()
        result = client.post(URL + "/test", json=FORM, headers={"Origin": LOCAL})
        assert result.status_code == 422 and result.json()["code"] == wanted
        assert client.get(URL).json()["test_status"] == "failed"
        assert app.state.runtime._model_for() is original
        assert FORM["api_key"] not in result.text


def test_probe_rejects_remote_cross_origin_forwarded_and_malformed_requests(monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("Invalid request must not contact the model")

    monkeypatch.setattr(HttpModel, "complete", unexpected)
    app = make_app()
    with browser(app) as client:
        for headers in (
            {},
            {"Origin": "https://evil.example"},
            {"Origin": LOCAL, "X-Forwarded-For": "127.0.0.1"},
        ):
            assert client.post(URL + "/test", json=FORM, headers=headers).status_code == 403
        invalid = client.post(
            URL + "/test",
            json={**FORM, "api_key": {"secret": FORM["api_key"]}},
            headers={"Origin": LOCAL},
        )
        assert invalid.status_code == 422 and FORM["api_key"] not in invalid.text
        for address in (
            "http://remote.example/v1",
            "https://user:password@example.test/v1?secret=1",
        ):
            bad = client.post(
                URL + "/test", json={**FORM, "base_url": address}, headers={"Origin": LOCAL}
            )
            assert bad.status_code == 422 and "password" not in bad.text
    with browser(app, client_host="192.0.2.10") as remote:
        assert remote.get(URL).status_code == 403
        assert remote.post(URL + "/test", json=FORM, headers={"Origin": LOCAL}).status_code == 403
    with browser(app, origin="http://dns-rebinding.example") as foreign_host:
        assert foreign_host.get(URL).status_code == 403


@pytest.mark.asyncio
async def test_running_business_turn_keeps_its_model_after_a_successful_switch(
    db, settings, monkeypatch
):
    seen = []

    async def complete(self, payload):
        if payload["tools"][0]["function"]["name"] == "connection_probe":
            return probe_reply()
        assert payload["model"] == self.settings.model_id
        seen.append(payload["model"])
        if payload["messages"][-1]["role"] == "tool":
            result = json.loads(payload["messages"][-1]["content"])
            message = raw_final(
                [{"type": "result", "result_ref": result["result_ref"], "view": "monthly_summary"}]
            )
        else:
            state = json.loads(payload["messages"][0]["content"].split("\n正式状态：\n")[1])
            scope = state["request_frame"]["available_scope_refs"][0]
            message = tool(
                "query_month", {k: scope[k] for k in ("company_id", "period", "scope_ref")}
            )
        return {"message": message, "usage": None, "finish_reason": "stop"}

    monkeypatch.setattr(HttpModel, "complete", complete)
    # Keep this test focused on snapshot ownership, not the unknown-model fallback cap.
    base = model_settings(settings).model_copy(update={"input_budget": 32000})
    connection = ModelConnection(base)
    runtime = Runtime(db, base, connection.current[1], model_connection=connection)
    user = Identity("U001")
    session = str(create_session(db, user, "C001")["session_id"])

    def queue():
        return accept_turn(db, user, session, uid(), "查询甲企业2026年9月发票总额")

    prior = queue()
    running = runtime.claim()
    assert running.turn_id == prior["turn_id"]
    await connection.test_and_enable(ConnectionInput.model_validate(FORM))
    await runtime.run(running)
    following = queue()
    await runtime.run(runtime.claim())
    for turn in (prior, following):
        actual = read_turn(db, user, turn["turn_id"])
        assert actual["status"] == "completed", actual["error"]
        assert actual["final_cards"][0]["invoice_total"] == "9622.06"
    assert seen == [base.model_id, base.model_id, FORM["model_id"], FORM["model_id"]]
