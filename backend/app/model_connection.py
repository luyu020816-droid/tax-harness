"""Loopback-only model configuration for the local demo process."""

import asyncio
import json
from contextlib import contextmanager, nullcontext
from ipaddress import ip_address
from math import ceil
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from pydantic import Field, SecretStr, ValidationError

from app.config import Settings
from app.errors import AppError
from app.model import HttpModel
from app.model_requests import build_request
from app.tool_schemas import StrictModel


class ConnectionInput(StrictModel):
    base_url: str = Field(min_length=1, max_length=500)
    model_id: str = Field(min_length=1, max_length=200)
    api_key: SecretStr = Field(min_length=1, max_length=2048)


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _check_local(request: Request, *, writing=False):
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    origin = request.headers.get("origin")
    if (
        not request.client
        or not _loopback(request.client.host)
        or not request.url.hostname
        or not _loopback(request.url.hostname)
        or any(
            name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host")
        )
        or ((writing or origin) and origin != expected)
    ):
        raise AppError("ACCESS_DENIED", "模型设置仅对本机同源页面开放。", 403)


def _candidate(base: Settings, form: ConnectionInput) -> Settings:
    url = form.base_url.strip().rstrip("/")
    model = form.model_id.strip()
    key = form.api_key.get_secret_value().strip()
    try:
        parsed = urlsplit(url)
        # Validate port syntax up front as well as the scheme and host.
        _ = parsed.port
        if (
            not parsed.hostname
            or parsed.scheme not in ("http", "https")
            or (parsed.scheme == "http" and not _loopback(parsed.hostname))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(ch.isspace() or ord(ch) < 32 for ch in url)
        ):
            raise ValueError
    except ValueError as exc:
        raise AppError(
            "INVALID_MODEL_URL",
            "请填写 HTTPS API 地址，本机接口可用 HTTP。地址中勿带凭据或查询参数。",
            422,
        ) from exc
    if not model or not key or not all(33 <= ord(ch) <= 126 for ch in key):
        raise AppError("INVALID_MODEL_CONFIG", "请填写有效的模型名称和 API Key。", 422)
    same_provider = url == base.model_base_url.rstrip("/") and model == base.model_id
    return base.model_copy(
        update={
            "model_base_url": url,
            "model_id": model,
            "model_api_key": key,
            "model_context_limit": base.model_context_limit
            if same_provider and base.model_context_limit > base.output_reserve
            else ceil(((base.input_budget or 16000) + base.output_reserve) / 0.95),
            "model_capacity_source": base.model_capacity_source if same_provider else "unknown",
            "model_extra_body": base.model_extra_body if same_provider else {},
        }
    )


class ModelConnection:
    def __init__(self, settings: Settings):
        self.base = settings
        self.current = (settings, HttpModel(settings))
        self.test_status = "untested" if settings.model_configured else "unconfigured"
        self.testing = asyncio.Lock()

    def status(self):
        settings, _ = self.current
        return {
            "configured": settings.model_configured,
            "source": ("env" if settings is self.base else "page")
            if settings.model_configured
            else "none",
            "model_id": settings.model_id if settings.model_configured else None,
            "test_status": self.test_status,
        }

    def _probe_configuration(self):
        return self.current

    async def test_and_enable(self, form: ConnectionInput | None, *, enable_guard=None):
        if self.testing.locked():
            raise AppError("MODEL_TEST_BUSY", "已有连接测试在进行，请稍后再试。", 409)
        async with self.testing:
            try:
                if form is None:
                    settings, candidate = self._probe_configuration()
                else:
                    settings = _candidate(self.base, form)
                    candidate = HttpModel(settings)
                if not settings.model_configured:
                    raise AppError("INVALID_MODEL_CONFIG", "请先填写模型连接配置。", 422)
                payload = build_request(
                    settings,
                    [
                        {
                            "role": "system",
                            "content": "这是 JSON 工具连接测试。必须调用 connection_probe，将 value 设为 ok。",
                        },
                        {"role": "user", "content": "请执行连接测试。"},
                    ],
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "connection_probe",
                                "description": "Return the connection test marker.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"value": {"type": "string", "enum": ["ok"]}},
                                    "required": ["value"],
                                    "additionalProperties": False,
                                },
                            },
                        }
                    ],
                    max_tokens=128,
                )
                async with asyncio.timeout(15):
                    result = await candidate.complete(payload)
                calls = result["message"].get("tool_calls", [])
                if len(calls) != 1 or calls[0]["function"]["name"] != "connection_probe":
                    raise ValueError
                if json.loads(calls[0]["function"]["arguments"]) != {"value": "ok"}:
                    raise ValueError
            except TimeoutError as exc:
                self.test_status = "failed"
                raise AppError(
                    "MODEL_TEST_TIMEOUT", "连接超时，请检查 API 地址和网络。", 422
                ) from exc
            except (ValueError, KeyError, TypeError) as exc:
                self.test_status = "failed"
                raise AppError(
                    "MODEL_TOOL_UNSUPPORTED", "接口已响应，但未返回可用的结构化工具调用。", 422
                ) from exc
            except AppError as exc:
                self.test_status = "failed"
                code = exc.details.get("http_status")
                if code in (401, 403):
                    raise AppError(
                        "MODEL_AUTH_FAILED", "认证失败，请检查 API Key 和模型权限。", 422
                    ) from exc
                if code == 404:
                    raise AppError(
                        "MODEL_NOT_FOUND", "接口或模型未找到，请检查 API 地址和模型名称。", 422
                    ) from exc
                raise
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                self.test_status = "failed"
                raise AppError(
                    "MODEL_TEST_FAILED", "接口连接失败，请检查 API 地址和网络。", 422
                ) from exc
            previous, prior_status = self.current, self.test_status
            try:
                with enable_guard(settings, candidate) if enable_guard else nullcontext():
                    self.current = (settings, candidate)
                    self.test_status = "success"
            except Exception:
                self.current, self.test_status = previous, prior_status
                raise
            return self.status()


def default_version_in(conn, version=None):
    columns = "version,source,model_id,config_fingerprint"
    if version is not None:
        return conn.execute(
            f"SELECT {columns} FROM default_model_versions WHERE version=%s", (version,)
        ).fetchone()
    return conn.execute(
        f"SELECT {columns} FROM default_model_versions ORDER BY version DESC LIMIT 1"
    ).fetchone()


def bind_default_in(conn, turn_id, agent_binding):
    """The caller holds the configuration lock until its acceptance commits."""
    if agent_binding and agent_binding["snapshot"]["model_profile_id"] != "default":
        return None
    active = default_version_in(conn)
    if active:
        conn.execute(
            "UPDATE turns SET default_model_version=%s WHERE turn_id=%s",
            (active["version"], turn_id),
        )
        return active["version"]
    return None


class DefaultModelConnection(ModelConnection):
    """Database orders activations. Page credentials live only in the activating process."""

    def __init__(self, db, settings):
        super().__init__(settings)
        self.db = db
        self.env = self.current
        unavailable = settings.model_copy(update={"model_api_key": ""})
        self.unavailable = (unavailable, HttpModel(unavailable))
        self.versions = {}
        self.tested_version = None

    def initialize(self):
        from app.agent_authority import LOCK_ID

        with self.db.transaction() as conn:
            if not conn.execute(
                "SELECT to_regclass('public.default_model_versions') AS t"
            ).fetchone()["t"]:
                return
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
            if self.base.model_configured and not default_version_in(conn):
                self._insert_in(conn, self.base)

    def _insert_in(self, conn, settings):
        from app.compaction_requests import model_fingerprint

        latest = default_version_in(conn)
        version = latest["version"] + 1 if latest else 1
        conn.execute(
            "INSERT INTO default_model_versions(version,source,model_id,config_fingerprint) VALUES(%s,%s,%s,%s)",
            (
                version,
                "env" if settings is self.base else "page",
                settings.model_id,
                model_fingerprint(settings),
            ),
        )
        return version

    @contextmanager
    def activation(self, auth, settings, candidate):
        from app.agent_authority import LOCK_ID
        from app.agent_profiles import _require_manager
        from app.agent_skills import record_event

        version = None
        try:
            with self.db.transaction() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
                _require_manager(conn, auth, lock=True)
                version = self._insert_in(conn, settings)
                record_event(conn, auth, "model", "default", "publish", version)
                self.versions[version] = (settings, candidate)
                yield
        except BaseException:
            self.versions.pop(version, None)
            raise
        self.tested_version = version

    def _connection_for(self, row):
        from app.compaction_requests import model_fingerprint

        if not row:
            return None
        if row["version"] in self.versions:
            return self.versions[row["version"]]
        if (
            row["source"] == "env"
            and self.base.model_configured
            and (row["config_fingerprint"] == model_fingerprint(self.base))
        ):
            return self.env
        return None

    def resolve(self, conn, version):
        if version is None:
            raise AppError(
                "DEFAULT_MODEL_UNBOUND", "原请求没有可证明的默认模型版本，请配置后重新发起。", 409
            )
        connection = self._connection_for(default_version_in(conn, version))
        if connection is None:
            raise AppError(
                "DEFAULT_MODEL_VERSION_UNAVAILABLE",
                "原连接不可用，请重新配置模型并重新发起请求。",
                503,
                details={"model_profile_id": "default", "model_profile_version": version},
            )
        return connection

    def active_configuration(self):
        with self.db.transaction(readonly=True) as conn:
            return self._connection_for(default_version_in(conn)) or self.unavailable

    def _probe_configuration(self):
        active = self.active_configuration()
        return active if active[0].model_configured else self.env

    def status(self):
        with self.db.transaction(readonly=True) as conn:
            row = default_version_in(conn)
            configured = self._connection_for(row) is not None
        version = row["version"] if row else None
        status = "unconfigured"
        if self.test_status == "failed":
            status = "failed"
        elif configured:
            status = "success" if version == self.tested_version else "untested"
        return {
            "configured": configured,
            "source": row["source"] if row else "none",
            "model_id": row["model_id"] if row else None,
            "version": version,
            "env_available": self.base.model_configured,
            "test_status": status,
        }


def model_connection_router(connection: ModelConnection, db, auth_dependency):
    router = APIRouter(prefix="/api/v1/model-connection")

    @router.get("")
    async def status(request: Request, current=auth_dependency):
        _check_local(request)
        from app.agent_profiles import _require_manager

        with db.transaction(readonly=True) as conn:
            _require_manager(conn, current)
        return connection.status()

    @router.post("/test")
    async def test_connection(request: Request, current=auth_dependency):
        _check_local(request, writing=True)
        from app.agent_profiles import _require_manager

        with db.transaction(readonly=True) as conn:
            _require_manager(conn, current)
        try:
            body = await request.json()
            form = None if body == {} else ConnectionInput.model_validate(body)
        except (ValidationError, ValueError) as exc:
            # Do not use the default validation response, which can echo input.
            raise AppError(
                "INVALID_MODEL_CONFIG", "请检查 API 地址、模型名称和 API Key。", 422
            ) from exc

        @contextmanager
        def enable_guard(settings, candidate):
            if isinstance(connection, DefaultModelConnection):
                with connection.activation(current, settings, candidate):
                    yield
                return
            with db.transaction() as conn:
                from app.agent_authority import LOCK_ID

                conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
                _require_manager(conn, current, lock=True)
                yield

        return await connection.test_and_enable(form, enable_guard=enable_guard)

    return router
