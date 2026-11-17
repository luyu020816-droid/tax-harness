import pytest
from psycopg.errors import InsufficientPrivilege

from app.agent_profiles import AgentDraft, create_draft, publish, set_enabled, update_draft
from app.agent_skills import SkillDraft
from app.agent_skills import publish as publish_skill
from app.agent_skills import set_enabled as enable_skill
from app.agent_tools import catalog
from app.agent_tools import set_enabled as enable_tool
from app.cli import _seed_default_agents
from app.errors import AppError
from app.identity import Identity
from app.values import jsonb


def draft(name="资料助手", prompt="解释资料"):
    return AgentDraft(name=name, prompt=prompt, model_profile_id="default")


def test_manager_can_create_and_update_draft_without_publishing(db):
    auth = Identity("U001")
    created = create_draft(db, auth, draft())
    assert created["revision"] == 1
    assert created["enabled"] is False
    assert created["current_version"] is None
    updated = update_draft(db, auth, created["agent_id"], 1, draft(prompt="解释正式资料"))
    assert updated["revision"] == 2
    assert updated["draft"]["prompt"] == "解释正式资料"
    with db.transaction(readonly=True) as conn:
        events = conn.execute(
            "SELECT action,revision FROM agent_config_events WHERE agent_id=%s ORDER BY revision",
            (created["agent_id"],),
        ).fetchall()
        assert events == [{"action": "create", "revision": 1}, {"action": "edit", "revision": 2}]
        assert conn.execute("SELECT count(*) n FROM agent_profile_versions").fetchone()["n"] == 0


def test_stale_update_duplicate_name_and_non_manager_are_rejected(db):
    auth = Identity("U001")
    created = create_draft(db, auth, draft())
    update_draft(db, auth, created["agent_id"], 1, draft(prompt="新版"))
    with pytest.raises(AppError, match="已变化"):
        update_draft(db, auth, created["agent_id"], 1, draft(prompt="过期"))
    with pytest.raises(AppError, match="已存在"):
        create_draft(db, auth, draft(name=" 资料助手 "))
    with pytest.raises(AppError, match="没有 Agent"):
        create_draft(db, Identity("U002"), draft(name="未授权"))
    with db.transaction(readonly=True) as conn:
        row = conn.execute(
            "SELECT draft FROM agent_profiles WHERE agent_id=%s", (created["agent_id"],)
        ).fetchone()
        assert row["draft"]["prompt"] == "新版"
        assert conn.execute("SELECT count(*) n FROM agent_profiles").fetchone()["n"] == 1


def test_published_versions_are_immutable_for_runtime_role(db):
    profile = create_draft(db, Identity("U001"), draft())
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO agent_profile_versions(agent_id,version,config,published_by) "
            "VALUES(%s,1,%s,'U001')",
            (profile["agent_id"], jsonb({"name": "原版本"})),
        )
    with pytest.raises(InsufficientPrivilege), db.transaction() as conn:
        conn.execute(
            "UPDATE agent_profile_versions SET config=%s WHERE agent_id=%s",
            (jsonb({"name": "被篡改"}), profile["agent_id"]),
        )
    with pytest.raises(InsufficientPrivilege), db.transaction() as conn:
        conn.execute("DELETE FROM agent_profile_versions WHERE agent_id=%s", (profile["agent_id"],))
    with db.transaction(readonly=True) as conn:
        saved = conn.execute(
            "SELECT config FROM agent_profile_versions WHERE agent_id=%s", (profile["agent_id"],)
        ).fetchone()
    assert saved["config"] == {"name": "原版本"}


def test_runtime_role_cannot_grant_or_revoke_configuration_management(db):
    with pytest.raises(InsufficientPrivilege), db.transaction() as conn:
        conn.execute("INSERT INTO agent_config_managers(user_id) VALUES('U002')")
    with pytest.raises(InsufficientPrivilege), db.transaction() as conn:
        conn.execute("DELETE FROM agent_config_managers WHERE user_id='U001'")
    with pytest.raises(InsufficientPrivilege), db.transaction() as conn:
        conn.execute("UPDATE agent_config_managers SET user_id='U002' WHERE user_id='U001'")


def test_default_agents_share_skill_and_do_not_replace_existing_config(db, admin):
    _seed_default_agents(admin)
    _seed_default_agents(admin)
    with db.transaction(readonly=True) as conn:
        rows = conn.execute(
            "SELECT p.name,v.config FROM agent_profiles p JOIN agent_profile_versions v "
            "ON v.agent_id=p.agent_id AND v.version=p.current_version ORDER BY p.name"
        ).fetchall()
        assert len(rows) == 2
        reader = next(row["config"] for row in rows if row["name"] == "知识查阅助手")
        writer = next(row["config"] for row in rows if row["name"] == "财税办理助手")
        assert reader["skill_versions"] == writer["skill_versions"]
        assert "find_invoices" in reader["tool_grants"] and "find_invoices" in writer["tool_grants"]
        assert "update_invoice" not in reader["tool_grants"]
        assert "update_invoice" in writer["tool_grants"]
        assert conn.execute("SELECT count(*) n FROM agent_skills").fetchone()["n"] == 1


def test_default_agent_seed_preserves_existing_user_configuration(db, admin):
    created = create_draft(db, Identity("U001"), draft(name="已有配置"))
    _seed_default_agents(admin)
    with db.transaction(readonly=True) as conn:
        rows = conn.execute("SELECT agent_id,name FROM agent_profiles").fetchall()
        assert len(rows) == 1 and str(rows[0]["agent_id"]) == created["agent_id"]
        assert conn.execute("SELECT count(*) n FROM agent_skills").fetchone()["n"] == 0


def test_agents_reuse_a_versioned_skill_and_require_its_tool_grants(db):
    auth = Identity("U001")
    skill = publish_skill(
        db,
        auth,
        SkillDraft(
            name="发票核对", instructions="核对发票", tool_grants={"find_invoices": ["invoke"]}
        ),
    )
    config = AgentDraft(
        name="助手一",
        model_profile_id="default",
        tool_grants={"find_invoices": ["invoke"]},
        skill_ids=[skill["skill_id"]],
    )
    first = create_draft(db, auth, config)
    first = publish(db, auth, first["agent_id"], first["revision"])
    second = create_draft(db, auth, config.model_copy(update={"name": "助手二"}))
    second = publish(db, auth, second["agent_id"], second["revision"])
    expected = [{"skill_id": skill["skill_id"], "version": 1}]
    assert first["published_config"]["skill_versions"] == expected
    assert second["published_config"]["skill_versions"] == expected
    revised = update_draft(
        db,
        auth,
        first["agent_id"],
        first["revision"],
        config.model_copy(update={"tool_grants": {}}),
    )
    with pytest.raises(AppError, match="尚未授权"):
        publish(db, auth, revised["agent_id"], revised["revision"])
    with db.transaction(readonly=True) as conn:
        row = conn.execute(
            "SELECT current_version FROM agent_profiles WHERE agent_id=%s", (first["agent_id"],)
        ).fetchone()
    assert row["current_version"] == 1
    enable_skill(db, auth, skill["skill_id"], skill["revision"], False)
    with pytest.raises(AppError, match="停用"):
        publish(db, auth, second["agent_id"], second["revision"])
    with pytest.raises(AppError, match="停用"):
        set_enabled(db, auth, second["agent_id"], second["revision"], True)


def test_enable_requires_published_agent_and_conflicts_are_rejected(db):
    auth = Identity("U001")
    created = create_draft(db, auth, draft())
    with pytest.raises(AppError, match="先发布"):
        set_enabled(db, auth, created["agent_id"], 1, True)
    published = publish(db, auth, created["agent_id"], 1)
    enabled = set_enabled(db, auth, created["agent_id"], published["revision"], True)
    assert enabled["enabled"]
    with pytest.raises(AppError, match="已变化"):
        set_enabled(db, auth, created["agent_id"], published["revision"], False)


def test_tool_state_records_affected_agents_and_blocks_new_publication(db):
    auth = Identity("U001")
    entries = {item["name"]: item for item in catalog(db)}
    assert entries["find_invoices"]["risk_type"] == "read_only"
    assert entries["update_invoice"]["risk_type"] == "side_effect"
    assert len(entries["find_invoices"]["implementation_version"]) == 64
    assert len(entries["find_invoices"]["contract_version"]) == 64
    config = AgentDraft(
        name="查询助手", model_profile_id="default", tool_grants={"find_invoices": ["invoke"]}
    )
    profile = create_draft(db, auth, config)
    published = publish(db, auth, profile["agent_id"], profile["revision"])
    state = enable_tool(db, auth, "find_invoices", 0, False)
    assert state["affected_agents"][0]["agent_id"] == profile["agent_id"]
    assert next(x for x in catalog(db) if x["name"] == "find_invoices")["enabled"] is False
    with pytest.raises(AppError, match="停用"):
        publish(db, auth, profile["agent_id"], published["revision"])
    with pytest.raises(AppError, match="已变化"):
        enable_tool(db, auth, "find_invoices", 0, True)
    with pytest.raises(AppError, match="没有 Agent"):
        enable_tool(db, Identity("U002"), "find_invoices", 1, True)
    enable_tool(db, auth, "find_invoices", 1, True)
    with db.transaction(readonly=True) as conn:
        events = conn.execute(
            "SELECT action,revision FROM agent_capability_events WHERE kind='tool' "
            "AND capability_id='find_invoices' ORDER BY revision"
        ).fetchall()
    assert events == [{"action": "disable", "revision": 1}, {"action": "enable", "revision": 2}]
