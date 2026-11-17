"""Process-local model connections for separately configured agents.

Only non-secret identifiers and versions are suitable for persistence. A bound
run keeps its resolved adapter; callers must not silently fall back to another
profile when a connection is missing after a restart.
"""

import asyncio
import re

from app.agent_profiles import _require_manager
from app.agent_skills import record_event
from app.errors import AppError
from app.model_connection import ModelConnection


class AgentModels:
    def __init__(self, settings):
        self.settings = settings
        self.connections = {}
        self.versions = {}
        self.locks = {}

    async def test_and_enable(self, profile_id, expected_version, form):
        if not isinstance(profile_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", profile_id):
            raise AppError("INVALID_MODEL_CONFIG", "模型配置编号无效。", 422)
        lock = self.locks.setdefault(profile_id, asyncio.Lock())
        if lock.locked():
            raise AppError("MODEL_TEST_BUSY", "此模型配置正在测试，请稍后再试。", 409)
        async with lock:
            previous = self.connections.get(profile_id)
            current_version = previous[0] if previous else 0
            if expected_version != current_version:
                raise AppError("MODEL_CONFIG_CONFLICT", "模型配置已变化，请刷新后重试。", 409)
            candidate = ModelConnection(self.settings)
            await candidate.test_and_enable(form)
            version = current_version + 1
            self.connections[profile_id] = (version, candidate)
            self.versions[(profile_id, version)] = candidate
            return {"profile_id": profile_id, "version": version, **candidate.status()}

    def resolve(self, profile_id, expected_version):
        connection = self.versions.get((profile_id, expected_version))
        if not connection:
            raise AppError("MODEL_CONFIG_UNAVAILABLE", "所选模型配置不可用，请重新配置。", 503)
        settings, model = connection.current
        if not settings.model_configured:
            raise AppError("MODEL_CONFIG_UNAVAILABLE", "所选模型配置不可用，请重新配置。", 503)
        return settings, model

    def status(self, profile_id):
        connection = self.connections.get(profile_id)
        if not connection:
            return {"profile_id": profile_id, "configured": False, "version": None}
        return {"profile_id": profile_id, "version": connection[0], **connection[1].status()}


class PersistentAgentModels:
    """Persist non-secret metadata only after the individual connection succeeds."""

    def __init__(self, db, settings):
        self.db = db
        self.runtime = AgentModels(settings)

    async def test_and_enable(self, auth, profile_id, name, expected_version, form):
        if not isinstance(profile_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", profile_id):
            raise AppError("INVALID_MODEL_CONFIG", "模型配置编号无效。", 422)
        if profile_id == "default" or not name.strip() or len(name) > 80:
            raise AppError("INVALID_MODEL_CONFIG", "模型配置名称或编号无效。", 422)
        with self.db.transaction(readonly=True) as conn:
            _require_manager(conn, auth)
        lock = self.runtime.locks.setdefault(profile_id, asyncio.Lock())
        if lock.locked():
            raise AppError("MODEL_TEST_BUSY", "此模型配置正在测试，请稍后再试。", 409)
        async with lock:
            candidate = ModelConnection(self.runtime.settings)
            await candidate.test_and_enable(form)
            settings, _ = candidate.current
            with self.db.transaction() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(8123419417)")
                _require_manager(conn, auth, lock=True)
                old = conn.execute(
                    "SELECT version FROM agent_model_profiles WHERE profile_id=%s FOR UPDATE",
                    (profile_id,),
                ).fetchone()
                if (old["version"] if old else 0) != expected_version:
                    raise AppError("MODEL_CONFIG_CONFLICT", "模型配置已变化，请刷新后重试。", 409)
                version = expected_version + 1
                conn.execute(
                    "INSERT INTO agent_model_profiles(profile_id,name,version,model_id,base_url) "
                    "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(profile_id) DO UPDATE SET "
                    "name=excluded.name,version=excluded.version,model_id=excluded.model_id,"
                    "base_url=excluded.base_url,enabled=true,updated_at=clock_timestamp()",
                    (profile_id, name.strip(), version, settings.model_id, settings.model_base_url),
                )
                record_event(conn, auth, "model", profile_id, "publish", version)
            self.runtime.connections[profile_id] = (version, candidate)
            self.runtime.versions[(profile_id, version)] = candidate
            return {"profile_id": profile_id, "version": version, **candidate.status()}

    def resolve(self, profile_id, version):
        return self.runtime.resolve(profile_id, version)

    def list_profiles(self, auth):
        with self.db.transaction(readonly=True) as conn:
            _require_manager(conn, auth)
            profiles = conn.execute(
                "SELECT profile_id,name,version,model_id,base_url,enabled FROM agent_model_profiles "
                "ORDER BY name,profile_id"
            ).fetchall()
        for profile in profiles:
            runtime = self.runtime.connections.get(profile["profile_id"])
            profile["configured"] = bool(runtime and runtime[0] == profile["version"])
        return profiles
