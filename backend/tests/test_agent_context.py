import pytest
from test_conversation_context import settled
from test_runtime import model_settings

from app import context
from app.agent_authority import bind_turn
from app.agent_profiles import AgentDraft, create_draft, publish, set_enabled, update_draft
from app.agent_skills import SkillDraft
from app.agent_skills import publish as publish_skill
from app.agent_tools import set_enabled as set_tool
from app.errors import AppError
from app.identity import Identity
from app.preferences import change_preference
from app.values import uid


@pytest.mark.parametrize("can_list", [False, True])
def test_mem02_agent_can_only_receive_preferences_with_list_permission(
    db, settings, execution_factory, can_list
):
    auth = Identity("U001")
    change_preference(
        db,
        auth,
        action="remember",
        scope="C001",
        key="company_format",
        value="MEM02_AGENT_SCOPED_FORMAT",
        request_id=uid(),
    )
    draft = AgentDraft(
        name="偏好授权校验",
        model_profile_id="default",
        tool_grants={"manage_preference": ["list"]} if can_list else {},
    )
    agent = create_draft(db, auth, draft)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    execution = execution_factory("请给一句问候")
    with db.transaction() as conn:
        conn.execute("UPDATE turns SET status='queued' WHERE turn_id=%s", (execution.turn_id,))
        bind_turn(conn, auth, execution.turn_id, agent["agent_id"])
        conn.execute("UPDATE turns SET status='running' WHERE turn_id=%s", (execution.turn_id,))
    payload, _, _ = context.assemble(db, model_settings(settings), execution, [])
    assert ("MEM02_AGENT_SCOPED_FORMAT" in payload["messages"][0]["content"]) is can_list


def test_global_tool_switch_filters_legacy_context_and_invalidates_old_payload(
    db, settings, execution_factory
):
    execution = execution_factory("查询甲企业发票")
    payload, manifest, _ = context.assemble(db, model_settings(settings), execution, [])
    assert "get_rules" in {item["function"]["name"] for item in payload["tools"]}
    set_tool(db, Identity("U001"), "get_rules", 0, False)
    with pytest.raises(AppError, match="停用"):
        context.validate(db, execution, payload, manifest)
    refreshed, _, _ = context.assemble(db, model_settings(settings), execution, [])
    names = {item["function"]["name"] for item in refreshed["tools"]}
    assert "get_rules" not in names and "lookup_knowledge" not in names


def test_context_uses_pinned_agent_and_skill_and_filters_tools(db, settings, execution_factory):
    auth = Identity("U001")
    skill = publish_skill(
        db,
        auth,
        SkillDraft(
            name="核对技能", instructions="按票号核对", tool_grants={"find_invoices": ["invoke"]}
        ),
    )
    draft = AgentDraft(
        name="只读 Agent",
        prompt="先解释查询范围",
        model_profile_id="default",
        tool_grants={
            "find_invoices": ["invoke"],
            "search_knowledge": ["invoke"],
            "search_conversations": ["invoke"],
        },
        skill_ids=[skill["skill_id"]],
        knowledge_mode="none",
    )
    agent = create_draft(db, auth, draft)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    execution = execution_factory("查询甲企业发票")
    with db.transaction() as conn:
        conn.execute("UPDATE turns SET status='queued' WHERE turn_id=%s", (execution.turn_id,))
        bind_turn(conn, auth, execution.turn_id, agent["agent_id"])
        conn.execute("UPDATE turns SET status='running' WHERE turn_id=%s", (execution.turn_id,))
    payload, manifest, _ = context.assemble(db, model_settings(settings), execution, [])
    assert "先解释查询范围" in payload["messages"][0]["content"]
    assert "按票号核对" in payload["messages"][0]["content"]
    names = {tool["function"]["name"] for tool in payload["tools"]}
    assert "find_invoices" in names
    assert "search_knowledge" not in names and "search_conversations" not in names
    assert manifest["current_knowledge_attachments"] == []
    reduced = draft.model_copy(
        update={
            "tool_grants": {"search_knowledge": ["invoke"], "search_conversations": ["invoke"]},
            "skill_ids": [],
        }
    )
    agent = update_draft(db, auth, agent["agent_id"], agent["revision"], reduced)
    publish(db, auth, agent["agent_id"], agent["revision"])
    with pytest.raises(AppError, match="撤销"):
        context.validate(db, execution, payload, manifest)


def test_restricted_agent_context_excludes_unbound_prior_assistant_text(
    db, settings, execution_factory
):
    auth = Identity("U001")
    draft = AgentDraft(
        name="隔离 Agent", prompt="按授权工作", model_profile_id="default", knowledge_mode="none"
    )
    agent = create_draft(db, auth, draft)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    execution = execution_factory("继续办理")
    settled(db, execution.session_id, "查询其他资料", answer="外部知识 SECRET-DOCUMENT-419")
    with db.transaction() as conn:
        seq = conn.execute(
            "UPDATE users SET next_source_seq=next_source_seq+1 WHERE user_id=%s RETURNING next_source_seq",
            (auth.user_id,),
        ).fetchone()["next_source_seq"]
        conn.execute(
            "UPDATE turns SET source_seq=%s,status='queued' WHERE turn_id=%s",
            (seq, execution.turn_id),
        )
        bind_turn(conn, auth, execution.turn_id, agent["agent_id"])
        conn.execute("UPDATE turns SET status='running' WHERE turn_id=%s", (execution.turn_id,))
    execution.source_seq = seq
    payload, manifest, _ = context.assemble(db, model_settings(settings), execution, [])
    assert "SECRET-DOCUMENT-419" not in str(payload)
    assert any(item["reason"] == "AGENT_KNOWLEDGE_SCOPE" for item in manifest["excluded_history"])
