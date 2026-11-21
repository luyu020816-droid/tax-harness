import pytest
from test_runtime import ScriptedModel, model_settings

from app.agent_profiles import AgentDraft, create_draft, publish, set_enabled
from app.agent_tools import set_enabled as set_tool_enabled
from app.identity import Identity, create_session
from app.runtime import Runtime
from app.turns import accept_turn, read_turn
from app.values import uid


@pytest.mark.asyncio
async def test_disabled_only_tool_returns_honest_blocked_checklist(db, settings):
    auth = Identity("U001")
    agent = create_draft(db, auth, AgentDraft(
        name="停用回复回归", model_profile_id="default", tool_grants={"query_month": ["invoke"]}
    ))
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    set_tool_enabled(db, auth, "query_month", 0, False)
    session = create_session(db, auth, "C001")
    turn = accept_turn(db, auth, str(session["session_id"]), uid(),
                       "查询甲企业2026年9月月度资料。", agent_id=agent["agent_id"])
    runtime = Runtime(db, model_settings(settings), ScriptedModel([]), agent_runtime_enabled=True)
    execution = runtime.claim()
    assert execution.turn_id == turn["turn_id"]
    await runtime.run(execution)
    result = read_turn(db, auth, execution.turn_id)
    assert result["status"] == "completed" and result["error"] is None
    assert "不可用或已停用" in result["final_message"]
    assert not any(c["kind"] == "monthly_summary" for c in result["final_cards"])
    progress = next(c for c in result["final_cards"] if c["kind"] == "request_progress")
    assert [i["status"] for i in progress["items"]] == ["blocked"]
