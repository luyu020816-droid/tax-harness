from typing import Protocol

import httpx

from app.config import Settings
from app.errors import AppError


class ModelAdapter(Protocol):
    mode: str

    async def complete(self, payload: dict) -> dict: ...


class HttpModel:
    mode = "live"

    def __init__(self, settings: Settings):
        self.settings = settings

    async def complete(self, payload: dict) -> dict:
        if not self.settings.model_configured:
            raise AppError(
                "MODEL_UNAVAILABLE", "模型尚未配置，请在本地 .env 填写接口与模型参数。", 503
            )
        base = self.settings.model_base_url.rstrip("/")
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": "Bearer " + self.settings.model_api_key,
                        "Content-Type": "application/json",
                    },
                )
            if response.status_code >= 400:
                context_error = any(
                    word in response.text.lower()
                    for word in (
                        "context_length_exceeded",
                        "maximum context",
                        "context window",
                        "context length",
                    )
                )
                if context_error:
                    raise AppError(
                        "CONTEXT_CAPACITY_EXCEEDED", "模型拒绝超长输入，请缩小处理范围。", 422
                    )
                raise AppError(
                    "MODEL_UNAVAILABLE",
                    f"模型接口请求失败（HTTP {response.status_code}），请检查配置或稍后重试。",
                    503,
                    response.status_code >= 500 or response.status_code == 429,
                )
            body = response.json()
            message = body["choices"][0]["message"]
            if message.get("reasoning_content"):
                raise AppError(
                    "MODEL_PROTOCOL_UNSUPPORTED",
                    "请在模型配置中关闭需要回传隐含推理的模式，当前适配器仅支持标准工具消息。",
                    422,
                )
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list) or any(
                not c.get("id") or c.get("type") != "function" for c in calls
            ):
                raise ValueError("invalid tool protocol")
            result = {"role": "assistant", "content": message.get("content")}
            if calls:
                result["tool_calls"] = calls
            return {
                "message": result,
                "usage": body.get("usage"),
                "finish_reason": body["choices"][0].get("finish_reason"),
            }
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise AppError(
                "MODEL_UNAVAILABLE", "模型连接暂时不可用，已保留本轮进度。", 503, True
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError(
                "MODEL_PROTOCOL_UNSUPPORTED", "模型返回不符合结构化工具协议。", 422
            ) from exc
