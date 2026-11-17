"""Versioned, reusable skill instructions and their required tool actions."""

from pydantic import Field

from app.agent_configuration import validate_grants
from app.agent_profiles import _require_manager
from app.errors import AppError
from app.identity import Identity
from app.tool_schemas import TOOLS, StrictModel
from app.values import jsonb, safe_json, uid


class SkillDraft(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    instructions: str = Field(min_length=1, max_length=12000)
    tool_grants: dict[str, list[str]] = Field(default_factory=dict, max_length=100)
    requires_knowledge: bool = False


def record_event(conn, auth, kind, capability_id, action, revision):
    conn.execute(
        "INSERT INTO agent_capability_events(event_id,actor_id,kind,capability_id,action,revision) "
        "VALUES(%s,%s,%s,%s,%s,%s)",
        (uid(), auth.user_id, kind, capability_id, action, revision),
    )


def publish(db, auth: Identity, draft: SkillDraft, *, skill_id=None, expected_revision=0):
    name = draft.name.strip()
    if not name or not draft.instructions.strip():
        raise AppError("SKILL_CONFIG_INVALID", "Skill 名称和指引不能为空。", 422)
    grants = {key: frozenset(actions) for key, actions in draft.tool_grants.items()}
    validate_grants(grants, frozenset(TOOLS))
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(8123419417)")
        _require_manager(conn, auth, lock=True)
        existing = None
        if skill_id:
            existing = conn.execute(
                "SELECT * FROM agent_skills WHERE skill_id=%s FOR UPDATE", (skill_id,)
            ).fetchone()
            if not existing:
                raise AppError("RESOURCE_NOT_FOUND", "Skill 不存在。", 404)
        if (existing["revision"] if existing else 0) != expected_revision:
            raise AppError("SKILL_CONFIG_CONFLICT", "Skill 已变化，请刷新后重试。", 409)
        if conn.execute(
            "SELECT 1 FROM agent_skills WHERE lower(btrim(name))=lower(%s) "
            "AND (%s::uuid IS NULL OR skill_id<>%s::uuid)",
            (name, skill_id, skill_id),
        ).fetchone():
            raise AppError("SKILL_NAME_EXISTS", "Skill 名称已存在。", 409)
        config = draft.model_copy(update={"name": name}).model_dump()
        if existing:
            row = conn.execute(
                "UPDATE agent_skills SET name=%s,revision=revision+1,current_version=current_version+1,"
                "updated_at=clock_timestamp() WHERE skill_id=%s RETURNING *",
                (name, skill_id),
            ).fetchone()
        else:
            skill_id = uid()
            row = conn.execute(
                "INSERT INTO agent_skills(skill_id,name,current_version) VALUES(%s,%s,1) RETURNING *",
                (skill_id, name),
            ).fetchone()
        conn.execute(
            "INSERT INTO agent_skill_versions(skill_id,version,config,published_by) VALUES(%s,%s,%s,%s)",
            (skill_id, row["current_version"], jsonb(config), auth.user_id),
        )
        record_event(conn, auth, "skill", skill_id, "publish", row["revision"])
        return safe_json({**row, "config": config})


def set_enabled(db, auth: Identity, skill_id: str, expected_revision: int, enabled: bool):
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(8123419417)")
        _require_manager(conn, auth, lock=True)
        row = conn.execute(
            "SELECT * FROM agent_skills WHERE skill_id=%s FOR UPDATE", (skill_id,)
        ).fetchone()
        if not row:
            raise AppError("RESOURCE_NOT_FOUND", "Skill 不存在。", 404)
        if row["revision"] != expected_revision:
            raise AppError("SKILL_CONFIG_CONFLICT", "Skill 已变化，请刷新后重试。", 409)
        if row["enabled"] == enabled:
            return safe_json(row)
        row = conn.execute(
            "UPDATE agent_skills SET enabled=%s,revision=revision+1,updated_at=clock_timestamp() "
            "WHERE skill_id=%s RETURNING *",
            (enabled, skill_id),
        ).fetchone()
        record_event(
            conn, auth, "skill", skill_id, "enable" if enabled else "disable", row["revision"]
        )
        return safe_json(row)
