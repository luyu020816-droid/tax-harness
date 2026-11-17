"""Opt-in live-model acceptance against the isolated test database only."""

import os

import pytest

from app.agent_profiles import AgentDraft, create_draft, publish, set_enabled
from app.config import Settings
from app.identity import Identity, create_session
from app.model import HttpModel
from app.runtime import Runtime
from app.trace import read_run
from app.turns import accept_turn, read_turn
from app.values import uid


@pytest.mark.skipif(
    not os.environ.get("LIVE_AGENT_MODEL_ENV"), reason="explicit live model opt-in required"
)
@pytest.mark.asyncio
async def test_two_agents_with_live_model_and_isolated_data(db, settings):
    model_settings = Settings(_env_file=os.environ["LIVE_AGENT_MODEL_ENV"]).model_copy(
        update={"database_url": settings.test_database_url, "public_web_enabled": False}
    )
    assert model_settings.model_configured, "live model settings incomplete"
    auth = Identity("U001")
    cases = [
        (
            "月度统计助手",
            "你负责查询企业月度统计。",
            {"query_month": ["invoke"], "resolve_scope": ["invoke"]},
            "查询甲企业2026年9月的月度资料",
            "query_month",
        ),
        (
            "票据查询助手",
            "你负责按票号查找发票。",
            {"find_invoices": ["invoke"]},
            "查询甲企业发票F002",
            "find_invoices",
        ),
    ]
    for name, prompt, grants, question, expected_tool in cases:
        agent = create_draft(
            db,
            auth,
            AgentDraft(name=name, prompt=prompt, model_profile_id="default", tool_grants=grants),
        )
        agent = publish(db, auth, agent["agent_id"], agent["revision"])
        agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
        session = create_session(db, auth, "C001")
        turn = accept_turn(
            db, auth, str(session["session_id"]), uid(), question, agent_id=agent["agent_id"]
        )
        runtime = Runtime(db, model_settings, HttpModel(model_settings), agent_runtime_enabled=True)
        execution = runtime.claim()
        assert execution is not None
        await runtime.run(execution)
        result = read_turn(db, auth, turn["turn_id"])
        assert result["status"] == "completed", (name, result.get("error"))
        trace = read_run(db, auth, execution.run_id)
        assert trace["agent"]["agent_id"] == agent["agent_id"]
        with db.transaction(readonly=True) as conn:
            names = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM tool_executions WHERE run_id=%s AND state='done'",
                    (execution.run_id,),
                )
            }
        assert expected_tool in names, (name, names)
