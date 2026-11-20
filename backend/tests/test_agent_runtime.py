import pytest
from test_judge_boundaries import raw_final
from test_model_connection import FORM, LOCAL, URL, browser, make_app, probe_reply
from test_runtime import ScriptedModel, final, model_settings, tool

from app.agent_profiles import AgentDraft, create_draft, publish, set_enabled
from app.identity import Identity, create_session
from app.model import HttpModel
from app.runtime import Runtime
from app.trace import read_run
from app.turns import accept_turn, read_turn
from app.values import uid


@pytest.mark.asyncio
async def test_ag01_default_model_is_bound_when_request_is_accepted(db, admin, monkeypatch):
    seen = []

    async def complete(self, payload):
        if any(item["function"]["name"] == "connection_probe" for item in payload.get("tools", [])):
            return probe_reply()
        seen.append(payload["model"])
        return {"message": raw_final([{"type": "chat_answer", "text": "问候已送达。"}])}

    monkeypatch.setattr(HttpModel, "complete", complete)
    app = make_app(db)
    auth = Identity("U001")
    session = str(create_session(db, auth, "C001")["session_id"])
    agent = create_draft(db, auth, AgentDraft(name="默认连接 Agent", model_profile_id="default"))
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    with browser(app) as client:

        def queue():
            reply = client.post(
                f"/api/v1/sessions/{session}/turns",
                json={
                    "client_request_id": uid(),
                    "text": "请写一句简短的问候。",
                    "agent_id": agent["agent_id"],
                },
            )
            assert reply.status_code == 202, reply.text
            return reply.json()

        first = queue()
        switched = client.post(URL + "/test", json=FORM, headers={"Origin": LOCAL})
        assert switched.status_code == 200
        second = queue()
        runtime = app.state.runtime
        assert first["default_model_version"] < second["default_model_version"]
        # The first worker is claimed, but never gets to send. A reclaim after
        # activation still resolves the first request's immutable version.
        leased = runtime.claim()
        assert leased.turn_id == first["turn_id"]
        assert runtime._model_for(leased)[0].model_id == "env-model"
        admin.execute(
            "UPDATE work_items SET lease_until=now()-interval '1 second' WHERE work_id=%s",
            (leased.work_id,),
        )
        for turn, model_name in ((first, "env-model"), (second, FORM["model_id"])):
            execution = runtime.claim()
            assert execution.turn_id == turn["turn_id"]
            await runtime.run(execution)
            assert read_turn(db, auth, execution.turn_id)["status"] == "completed"
            trace = read_run(db, auth, execution.run_id)
            assert trace["model_binding"] == {
                "profile_id": "default",
                "version": turn["default_model_version"],
                "model_id": model_name,
            }
            assert FORM["api_key"] not in str(trace) and "env-fixture-key" not in str(trace)
        assert read_turn(db, auth, first["turn_id"])["active_run_generation"] == 2
    assert seen == ["env-model", FORM["model_id"]]


def test_agent_runtime_stays_disabled_by_default_until_verified(db, settings):
    auth = Identity("U001")
    draft = AgentDraft(name="待验证 Agent", model_profile_id="default")
    agent = create_draft(db, auth, draft)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    session = create_session(db, auth, "C001")
    turn = accept_turn(
        db, auth, str(session["session_id"]), uid(), "查询发票", agent_id=agent["agent_id"]
    )
    runtime = Runtime(db, model_settings(settings), ScriptedModel([]))
    assert runtime.claim() is None
    result = read_turn(db, auth, turn["turn_id"])
    assert result["status"] == "failed"
    assert result["error"]["code"] == "AGENT_RUNTIME_NOT_READY"


@pytest.mark.asyncio
async def test_agent_turn_uses_bound_instructions_and_only_allowed_tools(db, settings):
    auth = Identity("U001")
    draft = AgentDraft(
        name="账务 Agent",
        prompt="先核对范围",
        model_profile_id="default",
        knowledge_mode="none",
        tool_grants={"query_month": ["invoke"], "resolve_scope": ["invoke"]},
    )
    agent = create_draft(db, auth, draft)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    session = create_session(db, auth, "C001")
    turn = accept_turn(
        db,
        auth,
        str(session["session_id"]),
        uid(),
        "查询甲企业2026-09月度资料",
        agent_id=agent["agent_id"],
    )
    model = ScriptedModel(
        [
            tool("query_month", {"company_id": "C001", "period": "2026-09"}),
            final("查询完成，三项金额分别列在结果中。"),
        ]
    )
    runtime = Runtime(db, model_settings(settings), model, agent_runtime_enabled=True)
    execution = runtime.claim()
    assert execution is not None
    await runtime.run(execution)
    result = read_turn(db, auth, turn["turn_id"])
    assert result["status"] == "completed", result
    assert result["final_cards"][0]["invoice_total"] == "9622.06"
    assert "先核对范围" in model.requests[0]["messages"][0]["content"]
    assert {t["function"]["name"] for t in model.requests[0]["tools"]} <= {
        "query_month",
        "resolve_scope",
    }
    trace = read_run(db, auth, execution.run_id)
    assert trace["agent"]["agent_id"] == agent["agent_id"]
    assert trace["agent"]["agent_version"] == 1
    assert trace["contexts"][0]["payload"] == model.requests[0]


@pytest.mark.asyncio
async def test_agents_use_independent_pinned_model_profiles(db, settings):
    auth = Identity("U001")
    with db.transaction() as conn:
        for name in ("model_a", "model_b"):
            conn.execute(
                "INSERT INTO agent_model_profiles(profile_id,name,version,model_id,base_url) "
                "VALUES(%s,%s,1,%s,'https://example.test/v1')",
                (name, name, name),
            )
    configs = {}
    for name in ("model_a", "model_b"):
        model = ScriptedModel([raw_final([{"type": "chat_answer", "text": "你好。"}])])
        configs[name] = (model_settings(settings).model_copy(update={"model_id": name}), model)

    class Models:
        def resolve(self, profile_id, version):
            assert version == 1
            return configs[profile_id]

    runtime = Runtime(
        db,
        model_settings(settings),
        ScriptedModel([]),
        agent_models=Models(),
        agent_runtime_enabled=True,
    )
    for name in ("model_a", "model_b"):
        agent = create_draft(db, auth, AgentDraft(name=name, model_profile_id=name))
        agent = publish(db, auth, agent["agent_id"], agent["revision"])
        agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
        session = create_session(db, auth, "C001")
        turn = accept_turn(
            db, auth, str(session["session_id"]), uid(), "请讲个笑话", agent_id=agent["agent_id"]
        )
        execution = runtime.claim()
        assert execution is not None
        await runtime.run(execution)
        assert read_turn(db, auth, turn["turn_id"])["status"] == "completed"
        assert configs[name][1].requests[0]["model"] == name
        assert read_run(db, auth, execution.run_id)["agent"]["model_profile_id"] == name
