"""Read-only Exa MCP adapter for user-requested public information."""

import json
import re
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx

from app.errors import AppError

ENDPOINT = "https://mcp.exa.ai/mcp?tools=web_search_exa"
_CITY = r"[\u4e00-\u9fff]{2,12}|[A-Za-z][A-Za-z .-]{1,30}"
_WEATHER = re.compile(r"(.*?)(?:天气(?:预报)?|气温)(?:怎么样|如何|呢|多少度|是多少)?")
_DAYS = ("今天", "明天", "后天")
_PUBLIC = re.compile(
    r"(?:请)?(?:帮我)?(?:联网|上网)(?:搜索|查询|查一下|搜一下|查|搜)\s*[:：]?\s*(.{1,200})"
)
_PRIVATE = re.compile(
    r"发票|税号|开户|银行|企业|公司|知识库|上传|附件|上[一个]轮|刚才|密码|密钥|"
    r"(?i:invoice|password|api.?key)|\d{7,}|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}"
)


def request(text, today, previous=None):
    text = text.strip().strip("？?。！!").strip()
    previous = previous or {}
    explicit = _PUBLIC.fullmatch(text)
    if explicit:
        query = explicit[1].strip()
        return {"query": query} if query and not _PRIVATE.search(query) else None
    city, relative = "", None
    weather_text = re.sub(
        r"^(?:请问|请帮我查一下|帮我查一下|查一下|那么|那(?=今天|明天|后天))", "", text
    ).strip()
    if match := _WEATHER.fullmatch(weather_text):
        prefix = match[1].strip()
        relative = next((day for day in _DAYS if day in prefix), "今天")
        city = prefix.replace(relative, "", 1).strip().removesuffix("的").strip()
        if city and not re.fullmatch(_CITY, city):
            return None
        city = city or previous.get("city", "")
    elif (follow := re.fullmatch(r"(?:那|那么)?(今天|明天|后天)呢", text)) and previous.get("city"):
        city, relative = previous["city"], follow[1]
    elif previous.get("missing_city"):
        location = re.fullmatch(rf"(?:我在|查一下|查)?({_CITY})(?:市)?", text)
        if location and text not in {"谢谢", "不用了", "算了", "不了", "随便", "不知道", "取消"}:
            city, relative = location[1], previous["relative"]
    if relative:
        if _PRIVATE.search(city):
            return None
        target = date.fromisoformat(today) + timedelta(days=_DAYS.index(relative))
        return {
            "city": city,
            "relative": relative,
            "weather_date": target.isoformat(),
            "query": f"{city} {relative} {target.isoformat()} weather forecast" if city else "",
        }
    return None


def current_request(conn, execution, today=None):
    current = conn.execute(
        "SELECT text,created_at,upload_ids,knowledge_upload_ids FROM turns WHERE turn_id=%s AND user_id=%s",
        (execution.turn_id, execution.auth.user_id),
    ).fetchone()
    if not current or current["upload_ids"] or current["knowledge_upload_ids"]:
        return None, None
    today = today or current["created_at"].astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    explicit = request(current["text"], today)
    if explicit and explicit["query"]:
        return explicit, None
    if not explicit and not re.fullmatch(
        rf"(?:我在|查一下|查)?({_CITY})(?:[？?。！!])?", current["text"].strip()
    ):
        return None, None
    last = conn.execute(
        "SELECT turn_id,text,created_at,status,pending_clarification,final_cards FROM turns "
        "WHERE session_id=%s AND user_id=%s AND source_seq<%s AND status<>'deleted' "
        "ORDER BY source_seq DESC LIMIT 1",
        (execution.session_id, execution.auth.user_id, execution.source_seq),
    ).fetchone()
    previous, origin = None, None
    if last and last["status"] == "completed":
        previous = next(
            (
                card
                for card in last["final_cards"]
                if card["kind"] == "public_web" and card.get("city")
            ),
            None,
        )
    elif (
        last
        and last["status"] == "waiting_user"
        and (last["pending_clarification"] or {}).get("missing_fields") == ["location"]
    ):
        original_date = last["created_at"].astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        pending = request(last["text"], original_date)
        if pending and pending.get("weather_date") and not pending["city"]:
            previous = {
                "missing_city": True,
                "relative": pending["relative"],
                "date": original_date,
            }
            if explicit is None:
                origin = str(last["turn_id"])
    return request(current["text"], previous["date"] if origin else today, previous), origin


def _rpc(client, method, params, request_id=None):
    body = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        body["id"] = request_id
    with client.stream("POST", ENDPOINT, json=body) as response:
        response.raise_for_status()
        if request_id is None:
            return {}, None
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > 80_000:
                raise ValueError("MCP response too large")
        text = raw.decode("utf-8")
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            for event in text.replace("\r\n", "\n").split("\n\n"):
                lines = [
                    line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:")
                ]
                if lines:
                    message = json.loads("\n".join(lines))
                    if message.get("id") == request_id:
                        break
            else:
                raise ValueError("Missing MCP response")
        else:
            message = json.loads(text)
        if message.get("id") != request_id or not isinstance(message.get("result"), dict):
            raise ValueError("Invalid MCP response")
        return message["result"], response.headers.get("mcp-session-id")


def search(request_data):
    protocol = "2025-11-25"
    with httpx.Client(
        timeout=6, headers={"Accept": "application/json, text/event-stream"}
    ) as client:
        try:
            server, session = _rpc(
                client,
                "initialize",
                {
                    "protocolVersion": protocol,
                    "capabilities": {},
                    "clientInfo": {"name": "tax-harness", "version": "0.1"},
                },
                1,
            )
            if server.get("protocolVersion") != protocol:
                raise ValueError("Unsupported MCP version")
            client.headers["MCP-Protocol-Version"] = protocol
            if session:
                client.headers["MCP-Session-Id"] = session
            _rpc(client, "notifications/initialized", {})
            result, _ = _rpc(
                client,
                "tools/call",
                {
                    "name": "web_search_exa",
                    "arguments": {"query": request_data["query"], "numResults": 3},
                },
                2,
            )
            if result.get("isError"):
                raise ValueError("MCP search failed")
            rows = _results(result, request_data.get("weather_date"))
        except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
            raise AppError(
                "PUBLIC_WEB_UNAVAILABLE", "联网查询暂时不可用，请稍后再试。", 503
            ) from exc
        finally:
            if "MCP-Session-Id" in client.headers:
                try:
                    client.delete(ENDPOINT, timeout=1)
                except httpx.HTTPError:
                    pass
    return {"kind": "public_web", **request_data, "results": rows}


def _results(response, weather_date):
    rows = []
    for entry in response.get("content", []):
        if entry.get("type") != "text":
            continue
        for match in re.finditer(
            r"(?ms)^Title: (?P<title>[^\n]+)\nURL: (?P<url>[^\n]+)\n"
            r"(?P<body>.*?)(?=^Title: |\Z)",
            entry["text"],
        ):
            url = match["url"].strip()
            if len(url) > 600 or re.search(r"[\s<>]", url):
                continue
            source = urlsplit(url)
            if source.scheme != "https" or not source.hostname or source.username:
                continue
            if weather_date and not (
                source.hostname == "exa.ai"
                and source.path.startswith("/library/weather/")
                and parse_qs(source.query).get("date") == [weather_date]
            ):
                continue
            _, _, excerpt = match["body"].partition("Highlights:\n")
            excerpt = next(
                (
                    line.strip()
                    for line in excerpt.splitlines()
                    if line.strip() and not line.startswith("#")
                ),
                "",
            )
            rows.append({"title": match["title"][:180], "url": url, "excerpt": excerpt[:340]})
            if weather_date or len(rows) == 3:
                return rows
    return rows
