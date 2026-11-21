import pytest
from test_conversation_context import settled
from test_model_connection import browser, make_app
from test_runtime import model_settings, tool

from app import context
from app.agent_authority import bind_turn
from app.agent_profiles import AgentDraft, create_draft, publish, set_enabled, update_draft
from app.agent_skills import SkillDraft
from app.agent_skills import publish as publish_skill
from app.agent_tools import set_enabled as set_tool
from app.errors import AppError
from app.identity import Identity
from app.preferences import change_preference
from app.tools import ToolExecutor
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


@pytest.mark.asyncio
async def test_ar06_agent_without_history_can_explain_the_boundary(db, settings):
    from test_runtime import ScriptedModel, answer_plan

    from app.identity import create_session
    from app.runtime import Runtime
    from app.turns import accept_turn, read_turn

    auth = Identity("U001")
    draft = create_draft(db, auth, AgentDraft(name="限制历史检索", model_profile_id="default"))
    agent = publish(db, auth, draft["agent_id"], draft["revision"])
    set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    session = str(create_session(db, auth, "C001")["session_id"])
    turn = accept_turn(db, auth, session, uid(), "回顾我昨天在其他会话说的话", agent_id=agent["agent_id"])
    model = ScriptedModel([answer_plan(blocks=[{"type": "explanation", "code": "conversation_boundary"}])])
    runtime = Runtime(db, model_settings(settings), model, agent_runtime_enabled=True)
    await runtime.run(runtime.claim())
    result = read_turn(db, auth, turn["turn_id"])
    assert result["status"] == "completed", result["error"]
    assert "当前 Agent 无权检索其他会话" in result["final_message"]


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


def test_cap01_shared_skill_is_removed_when_required_tool_is_disabled(
    db, settings, execution_factory
):
    auth = Identity("U001")
    marker = "通过票号核对的共享指引"
    skill = publish_skill(
        db,
        auth,
        SkillDraft(
            name="发票核对共享能力", instructions=marker, tool_grants={"find_invoices": ["invoke"]}
        ),
    )
    other = publish_skill(
        db,
        auth,
        SkillDraft(
            name="保留规则指引", instructions="核对适用版本", tool_grants={"get_rules": ["invoke"]}
        ),
    )
    agents = []
    for name in ("只读核对", "办理核对"):
        agent = create_draft(
            db,
            auth,
            AgentDraft(
                name=name,
                model_profile_id="default",
                skill_ids=[skill["skill_id"], other["skill_id"]],
                tool_grants={"find_invoices": ["invoke"], "get_rules": ["invoke"]},
            ),
        )
        agent = publish(db, auth, agent["agent_id"], agent["revision"])
        agents.append(set_enabled(db, auth, agent["agent_id"], agent["revision"], True))

    def bound(agent):
        ex = execution_factory("查询甲企业发票")
        with db.transaction() as conn:
            conn.execute("UPDATE turns SET status='queued' WHERE turn_id=%s", (ex.turn_id,))
            bind_turn(conn, auth, ex.turn_id, agent["agent_id"])
            conn.execute("UPDATE turns SET status='running' WHERE turn_id=%s", (ex.turn_id,))
        return ex

    config = model_settings(settings)
    old = bound(agents[0])
    prior, manifest, _ = context.assemble(db, config, old, [])
    assert marker in prior["messages"][0]["content"]
    set_tool(db, auth, "find_invoices", 0, False)
    during = []
    with browser(make_app(db)) as client:
        managed = client.get("/api/v1/agent-admin/agents").json()["agents"]
        for agent in agents:
            ex = bound(agent)
            during.append(ex)
            payload, _, _ = context.assemble(db, config, ex, [])
            names = {spec["function"]["name"] for spec in payload["tools"]}
            assert names == {"get_rules"}
            assert marker not in payload["messages"][0]["content"]
            assert "核对适用版本" in payload["messages"][0]["content"]
            preview = client.post(
                f"/api/v1/sessions/{ex.session_id}/context-preview",
                json={"agent_id": agent["agent_id"], "draft": "查询甲企业发票"},
            ).json()
            assert set(preview["tool_names"]) == names
            assert [ref["skill_id"] for ref in preview["skill_versions"]] == [other["skill_id"]]
            [inactive] = preview["inactive_skills"]
            assert inactive["skill_id"] == skill["skill_id"]
            assert inactive["missing_tools"] == {"find_invoices": ["invoke"]}
            record = next(row for row in managed if row["agent_id"] == agent["agent_id"])
            assert record["inactive_skills"] == preview["inactive_skills"]
            denied, _ = ToolExecutor(db).execute(
                ex, tool("find_invoices", {"company_id": "C001"})["tool_calls"][0]
            )
            assert denied["error"]["code"] == "AGENT_CAPABILITY_DENIED"
        set_tool(db, auth, "find_invoices", 1, True)
        managed = client.get("/api/v1/agent-admin/agents").json()["agents"]
        for agent in agents:
            payload, _, _ = context.assemble(db, config, bound(agent), [])
            assert marker in payload["messages"][0]["content"]
            assert "find_invoices" in {spec["function"]["name"] for spec in payload["tools"]}
            assert not next(row for row in managed if row["agent_id"] == agent["agent_id"])[
                "inactive_skills"
            ]
    for ex in during:
        payload, _, _ = context.assemble(db, config, ex, [])
        assert marker not in payload["messages"][0]["content"]
        assert "find_invoices" not in {spec["function"]["name"] for spec in payload["tools"]}
    with pytest.raises(AppError, match="停用"):
        context.validate(db, old, prior, manifest)


def test_cap01_skill_requires_tool_in_the_actual_request_schema(db, settings, execution_factory):
    auth = Identity("U001")
    marker = "先读取工作簿结构，再核对金额列"
    skill = publish_skill(
        db,
        auth,
        SkillDraft(
            name="工作簿指引", instructions=marker, tool_grants={"inspect_workbook": ["invoke"]}
        ),
    )
    draft = AgentDraft(
        name="工作簿助手",
        model_profile_id="default",
        skill_ids=[skill["skill_id"]],
        tool_grants={"inspect_workbook": ["invoke"]},
        knowledge_mode="authorized",
    )
    agent = create_draft(db, auth, draft)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    ex = execution_factory("你好。")
    with db.transaction() as conn:
        conn.execute("UPDATE turns SET status='queued' WHERE turn_id=%s", (ex.turn_id,))
        binding = bind_turn(conn, auth, ex.turn_id, agent["agent_id"])
        conn.execute("UPDATE turns SET status='running' WHERE turn_id=%s", (ex.turn_id,))
    assert binding["snapshot"]["skill_versions"]
    payload, _, _ = context.assemble(db, model_settings(settings), ex, [])
    assert "inspect_workbook" not in {spec["function"]["name"] for spec in payload.get("tools", [])}
    assert marker not in payload["messages"][0]["content"]
