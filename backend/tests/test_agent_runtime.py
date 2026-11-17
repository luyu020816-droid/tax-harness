import pytest
from test_judge_boundaries import raw_final
from test_runtime import ScriptedModel, final, model_settings, tool

from app.agent_profiles import AgentDraft, create_draft, publish, set_enabled
from app.identity import Identity, create_session
from app.runtime import Runtime
from app.trace import read_run
from app.turns import accept_turn, read_turn
from app.values import uid


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
