"""Local-only configuration APIs; agent execution remains separately gated."""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field, ValidationError

from app import agent_profiles, agent_skills, agent_tools
from app.agent_authority import LOCK_ID, effective_snapshot
from app.agent_profiles import AgentDraft, _require_manager
from app.agent_skills import SkillDraft
from app.errors import AppError
from app.model_connection import ConnectionInput, _check_local
from app.tool_schemas import StrictModel
from app.values import safe_json


class EditAgent(StrictModel):
    expected_revision: int = Field(ge=1)
    draft: AgentDraft


class Revision(StrictModel):
    expected_revision: int = Field(ge=1)


class Enable(Revision):
    enabled: bool


class ToolEnable(StrictModel):
    expected_revision: int = Field(ge=0)
    enabled: bool


class EditSkill(StrictModel):
    expected_revision: int = Field(ge=1)
    draft: SkillDraft


class ModelTest(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    expected_version: int = Field(ge=0)
    connection: ConnectionInput


def agent_admin_router(db, auth_dependency, models):
    router = APIRouter(prefix="/api/v1/agent-admin")

    def check(request, auth, *, writing=False):
        _check_local(request, writing=writing)
        with db.transaction(readonly=True) as conn:
            _require_manager(conn, auth)

    @router.get("/agents")
    def agents(request: Request, current=auth_dependency):
        check(request, current)
        with db.transaction(readonly=True) as conn:
            conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (LOCK_ID,))
            rows = conn.execute(
                "SELECT p.*,v.config AS published_config FROM agent_profiles p "
                "LEFT JOIN agent_profile_versions v ON v.agent_id=p.agent_id "
                "AND v.version=p.current_version ORDER BY p.name,p.agent_id"
            ).fetchall()
            for row in rows:
                config = row["published_config"]
                row["inactive_skills"] = effective_snapshot(conn, config)["inactive_skills"] if config else []
        return {"agents": safe_json(rows), "runtime_ready": True}

    @router.post("/agents", status_code=201)
    def create_agent(body: AgentDraft, request: Request, current=auth_dependency):
        check(request, current, writing=True)
        return agent_profiles.create_draft(db, current, body)

    @router.put("/agents/{agent_id}")
    def edit_agent(agent_id: UUID, body: EditAgent, request: Request, current=auth_dependency):
        check(request, current, writing=True)
        return agent_profiles.update_draft(
            db, current, str(agent_id), body.expected_revision, body.draft
        )

    @router.post("/agents/{agent_id}/publish")
    def publish_agent(agent_id: UUID, body: Revision, request: Request, current=auth_dependency):
        check(request, current, writing=True)
        return agent_profiles.publish(db, current, str(agent_id), body.expected_revision)

    @router.post("/agents/{agent_id}/enabled")
    def enable_agent(agent_id: UUID, body: Enable, request: Request, current=auth_dependency):
        check(request, current, writing=True)
        return agent_profiles.set_enabled(
            db, current, str(agent_id), body.expected_revision, body.enabled
        )

    @router.get("/skills")
    def skills(request: Request, current=auth_dependency):
        check(request, current)
        with db.transaction(readonly=True) as conn:
            rows = conn.execute(
                "SELECT s.*,v.config FROM agent_skills s JOIN agent_skill_versions v "
                "ON v.skill_id=s.skill_id AND v.version=s.current_version ORDER BY s.name,s.skill_id"
            ).fetchall()
        return safe_json(rows)

    @router.post("/skills", status_code=201)
    def create_skill(body: SkillDraft, request: Request, current=auth_dependency):
        check(request, current, writing=True)
        return agent_skills.publish(db, current, body)

    @router.put("/skills/{skill_id}")
    def edit_skill(skill_id: UUID, body: EditSkill, request: Request, current=auth_dependency):
        check(request, current, writing=True)
        return agent_skills.publish(
            db,
            current,
            body.draft,
            skill_id=str(skill_id),
            expected_revision=body.expected_revision,
        )

    @router.post("/skills/{skill_id}/enabled")
    def enable_skill(skill_id: UUID, body: Enable, request: Request, current=auth_dependency):
        check(request, current, writing=True)
        return agent_skills.set_enabled(
            db, current, str(skill_id), body.expected_revision, body.enabled
        )

    @router.get("/tools")
    def tools(request: Request, current=auth_dependency):
        check(request, current)
        return agent_tools.catalog(db)

    @router.post("/tools/{name}/enabled")
    def enable_tool(name: str, body: ToolEnable, request: Request, current=auth_dependency):
        check(request, current, writing=True)
        return agent_tools.set_enabled(db, current, name, body.expected_revision, body.enabled)

    @router.get("/models")
    def model_profiles(request: Request, current=auth_dependency):
        check(request, current)
        return safe_json(models.list_profiles(current))

    @router.post("/models/{profile_id}/test")
    async def test_model(profile_id: str, request: Request, current=auth_dependency):
        check(request, current, writing=True)
        try:
            body = ModelTest.model_validate(await request.json())
        except (ValidationError, ValueError) as exc:
            raise AppError("INVALID_MODEL_CONFIG", "请检查模型配置、版本与凭据。", 422) from exc
        return await models.test_and_enable(
            current, profile_id, body.name, body.expected_version, body.connection
        )

    return router
