"""Loopback-only model configuration for the local demo process."""

import asyncio
import json
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
            else ceil((base.input_budget + base.output_reserve) / 0.95),
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

    async def test_and_enable(self, form: ConnectionInput | None):
        if self.testing.locked():
            raise AppError("MODEL_TEST_BUSY", "已有连接测试在进行，请稍后再试。", 409)
        async with self.testing:
            try:
                settings, candidate = self.current
                if form is not None:
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
            self.current = (settings, candidate)
            self.test_status = "success"
            return self.status()


def model_connection_router(connection: ModelConnection):
    router = APIRouter(prefix="/api/v1/model-connection")

    @router.get("")
    async def status(request: Request):
        _check_local(request)
        return connection.status()

    @router.post("/test")
    async def test_connection(request: Request):
        _check_local(request, writing=True)
        try:
            body = await request.json()
            form = None if body == {} else ConnectionInput.model_validate(body)
        except (ValidationError, ValueError) as exc:
            # Do not use the default validation response, which can echo input.
            raise AppError(
                "INVALID_MODEL_CONFIG", "请检查 API 地址、模型名称和 API Key。", 422
            ) from exc
        return await connection.test_and_enable(form)

    return router
