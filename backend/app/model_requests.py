from app.config import Settings
from app.errors import AppError

PROTECTED = {
    "messages",
    "tools",
    "model",
    "stream",
    "max_tokens",
    "tool_choice",
    "parallel_tool_calls",
    "response_format",
}


def build_request(
    settings: Settings,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    max_tokens: int | None = None,
) -> dict:
    if PROTECTED.intersection(settings.model_extra_body):
        raise AppError("INVALID_ARGUMENT", "MODEL_EXTRA_BODY 不能覆盖核心请求或响应协议字段。")
    payload = {
        **settings.model_extra_body,
        "model": settings.model_id,
        "max_tokens": max_tokens if max_tokens is not None else settings.output_reserve,
        "stream": False,
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    if tools is not None:
        payload.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
    return payload
