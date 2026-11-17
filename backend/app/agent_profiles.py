"""Draft storage for agent configuration; publication is a separate operation."""

from pydantic import Field

from app.agent_configuration import SkillRequirements, validate_grants, validate_skill_dependencies
from app.errors import AppError
from app.identity import Identity
from app.tool_schemas import TOOLS, StrictModel
from app.values import jsonb, safe_json, uid


class AgentDraft(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    prompt: str = Field(default="", max_length=12000)
    model_profile_id: str = Field(min_length=1, max_length=64)
    tool_grants: dict[str, list[str]] = Field(default_factory=dict, max_length=100)
    skill_ids: list[str] = Field(default_factory=list, max_length=50)
    knowledge_mode: str = Field(default="none", pattern="^(none|authorized|selected)$")
    knowledge_document_ids: list[str] = Field(default_factory=list, max_length=20)


def _require_manager(conn, auth: Identity, *, lock=False):
    if not conn.execute(
        "SELECT 1 FROM agent_config_managers m JOIN users u USING(user_id) "
        "WHERE m.user_id=%s AND u.is_active" + (" FOR SHARE OF m,u" if lock else ""),
        (auth.user_id,),
    ).fetchone():
        raise AppError("ACCESS_DENIED", "当前账号没有 Agent 配置管理权限。", 403)


def _event(conn, agent_id, auth, action, revision):
    conn.execute(
        "INSERT INTO agent_config_events(event_id,agent_id,actor_id,action,revision) "
        "VALUES(%s,%s,%s,%s,%s)",
        (uid(), agent_id, auth.user_id, action, revision),
    )


def create_draft(db, auth: Identity, draft: AgentDraft):
    name = draft.name.strip()
    if not name:
        raise AppError("AGENT_CONFIG_INVALID", "Agent 名称不能为空。", 422)
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(8123419417)")
        _require_manager(conn, auth, lock=True)
        if conn.execute(
            "SELECT 1 FROM agent_profiles WHERE lower(btrim(name))=lower(%s)", (name,)
        ).fetchone():
            raise AppError("AGENT_NAME_EXISTS", "Agent 名称已存在。", 409)
        agent_id = uid()
        row = conn.execute(
            "INSERT INTO agent_profiles(agent_id,name,draft) VALUES(%s,%s,%s) RETURNING *",
            (agent_id, name, jsonb(draft.model_copy(update={"name": name}).model_dump())),
        ).fetchone()
        _event(conn, agent_id, auth, "create", row["revision"])
        return safe_json(row)


def update_draft(db, auth: Identity, agent_id: str, expected_revision: int, draft: AgentDraft):
    name = draft.name.strip()
    if not name:
        raise AppError("AGENT_CONFIG_INVALID", "Agent 名称不能为空。", 422)
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(8123419417)")
        _require_manager(conn, auth, lock=True)
        existing = conn.execute(
            "SELECT * FROM agent_profiles WHERE agent_id=%s FOR UPDATE", (agent_id,)
        ).fetchone()
        if not existing:
            raise AppError("RESOURCE_NOT_FOUND", "Agent 不存在。", 404)
        if existing["revision"] != expected_revision:
            raise AppError("AGENT_CONFIG_CONFLICT", "Agent 配置已变化，请刷新后重试。", 409)
        if conn.execute(
            "SELECT 1 FROM agent_profiles WHERE lower(btrim(name))=lower(%s) AND agent_id<>%s",
            (name, agent_id),
        ).fetchone():
            raise AppError("AGENT_NAME_EXISTS", "Agent 名称已存在。", 409)
        row = conn.execute(
            "UPDATE agent_profiles SET name=%s,draft=%s,revision=revision+1,"
            "updated_at=clock_timestamp() WHERE agent_id=%s RETURNING *",
            (name, jsonb(draft.model_copy(update={"name": name}).model_dump()), agent_id),
        ).fetchone()
        _event(conn, agent_id, auth, "edit", row["revision"])
        return safe_json(row)


def publish(db, auth: Identity, agent_id: str, expected_revision: int):
    from uuid import UUID

    from app.knowledge import document_in

    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(8123419417)")
        _require_manager(conn, auth, lock=True)
        row = conn.execute(
            "SELECT * FROM agent_profiles WHERE agent_id=%s FOR UPDATE", (agent_id,)
        ).fetchone()
        if not row:
            raise AppError("RESOURCE_NOT_FOUND", "Agent 不存在。", 404)
        if row["revision"] != expected_revision:
            raise AppError("AGENT_CONFIG_CONFLICT", "Agent 配置已变化，请刷新后重试。", 409)
        draft = AgentDraft.model_validate(row["draft"])
        grants = {name: frozenset(actions) for name, actions in draft.tool_grants.items()}
        validate_grants(grants, frozenset(TOOLS))
        disabled = conn.execute(
            "SELECT tool_name FROM agent_tool_states WHERE NOT enabled AND tool_name=ANY(%s)",
            (list(grants),),
        ).fetchall()
        if disabled:
            raise AppError("AGENT_CONFIG_INVALID", "配置引用了已停用的工具。", 422)
        model_version = None
        if draft.model_profile_id != "default":
            model = conn.execute(
                "SELECT version,enabled FROM agent_model_profiles WHERE profile_id=%s",
                (draft.model_profile_id,),
            ).fetchone()
            if not model or not model["enabled"]:
                raise AppError("AGENT_CONFIG_INVALID", "模型配置不存在或已停用。", 422)
            model_version = model["version"]
        try:
            skill_ids = [str(UUID(value)) for value in draft.skill_ids]
            document_ids = [str(UUID(value)) for value in draft.knowledge_document_ids]
        except ValueError as exc:
            raise AppError("AGENT_CONFIG_INVALID", "Skill 或资料编号无效。", 422) from exc
        if len(set(skill_ids)) != len(skill_ids) or len(set(document_ids)) != len(document_ids):
            raise AppError("AGENT_CONFIG_INVALID", "Skill 或资料编号重复。", 422)
        if draft.knowledge_mode == "selected":
            if not document_ids:
                raise AppError("AGENT_CONFIG_INVALID", "请至少选择一份知识资料。", 422)
            for document_id in document_ids:
                document = document_in(conn, auth, document_id)
                if document["status"] != "active" or not document["active_version_id"]:
                    raise AppError("AGENT_CONFIG_INVALID", "所选知识资料尚不可用。", 422)
        elif document_ids:
            raise AppError("AGENT_CONFIG_INVALID", "当前知识范围不应指定资料。", 422)
        skills = {}
        pinned = []
        for skill_id in skill_ids:
            skill = conn.execute(
                "SELECT s.enabled,s.current_version,v.config FROM agent_skills s "
                "JOIN agent_skill_versions v ON v.skill_id=s.skill_id AND v.version=s.current_version "
                "WHERE s.skill_id=%s",
                (skill_id,),
            ).fetchone()
            if not skill:
                raise AppError("AGENT_CONFIG_INVALID", "Skill 不存在或没有有效版本。", 422)
            config = skill["config"]
            skills[skill_id] = SkillRequirements(
                enabled=skill["enabled"],
                tools={key: frozenset(value) for key, value in config["tool_grants"].items()},
                requires_knowledge=config["requires_knowledge"],
            )
            pinned.append({"skill_id": skill_id, "version": skill["current_version"]})
        validate_skill_dependencies(grants, draft.knowledge_mode, skills)
        version = (row["current_version"] or 0) + 1
        config = {
            **draft.model_dump(),
            "skill_ids": skill_ids,
            "knowledge_document_ids": document_ids,
            "skill_versions": pinned,
            "model_profile_version": model_version,
        }
        conn.execute(
            "INSERT INTO agent_profile_versions(agent_id,version,config,published_by) "
            "VALUES(%s,%s,%s,%s)",
            (agent_id, version, jsonb(config), auth.user_id),
        )
        row = conn.execute(
            "UPDATE agent_profiles SET current_version=%s,revision=revision+1,"
            "updated_at=clock_timestamp() WHERE agent_id=%s RETURNING *",
            (version, agent_id),
        ).fetchone()
        _event(conn, agent_id, auth, "publish", row["revision"])
        return safe_json({**row, "published_config": config})


def set_enabled(db, auth: Identity, agent_id: str, expected_revision: int, enabled: bool):
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(8123419417)")
        _require_manager(conn, auth, lock=True)
        row = conn.execute(
            "SELECT * FROM agent_profiles WHERE agent_id=%s FOR UPDATE", (agent_id,)
        ).fetchone()
        if not row:
            raise AppError("RESOURCE_NOT_FOUND", "Agent 不存在。", 404)
        if row["revision"] != expected_revision:
            raise AppError("AGENT_CONFIG_CONFLICT", "Agent 配置已变化，请刷新后重试。", 409)
        if enabled and not row["current_version"]:
            raise AppError("AGENT_CONFIG_INVALID", "请先发布 Agent 配置。", 422)
        if row["enabled"] == enabled:
            return safe_json(row)
        if enabled:
            config = conn.execute(
                "SELECT config FROM agent_profile_versions WHERE agent_id=%s AND version=%s",
                (agent_id, row["current_version"]),
            ).fetchone()["config"]
            if conn.execute(
                "SELECT 1 FROM agent_tool_states WHERE NOT enabled AND tool_name=ANY(%s) LIMIT 1",
                (list(config["tool_grants"]),),
            ).fetchone():
                raise AppError("AGENT_CONFIG_INVALID", "Agent 引用了已停用的工具。", 422)
            for skill in config["skill_versions"]:
                state = conn.execute(
                    "SELECT enabled FROM agent_skills WHERE skill_id=%s", (skill["skill_id"],)
                ).fetchone()
                if not state or not state["enabled"]:
                    raise AppError("AGENT_CONFIG_INVALID", "Agent 引用了已停用的 Skill。", 422)
            if config["knowledge_mode"] == "selected":
                from app.knowledge import document_in

                for document_id in config["knowledge_document_ids"]:
                    document = document_in(conn, auth, document_id)
                    if document["status"] != "active" or not document["active_version_id"]:
                        raise AppError("AGENT_CONFIG_INVALID", "Agent 的知识资料不可用。", 422)
            if config["model_profile_id"] != "default":
                model = conn.execute(
                    "SELECT enabled FROM agent_model_profiles WHERE profile_id=%s",
                    (config["model_profile_id"],),
                ).fetchone()
                if not model or not model["enabled"]:
                    raise AppError("AGENT_CONFIG_INVALID", "Agent 的模型配置不可用。", 422)
        row = conn.execute(
            "UPDATE agent_profiles SET enabled=%s,revision=revision+1,"
            "updated_at=clock_timestamp() WHERE agent_id=%s RETURNING *",
            (enabled, agent_id),
        ).fetchone()
        _event(conn, agent_id, auth, "enable" if enabled else "disable", row["revision"])
        return safe_json(row)
