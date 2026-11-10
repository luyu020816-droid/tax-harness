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
_WEATHER_TERMS = {
    "overview": r"天气(?:预报)?",
    "low": r"最低(?:气温|温度|温|多少度|几度)?|低温",
    "high": r"最高(?:气温|温度|温|多少度|几度)?|高温",
    "feels": r"体感(?:气温|温度)?",
    "rain": r"降雨概率|下雨概率|降雨|下雨|降水",
    "humidity": r"湿度",
    "wind": r"风速|风力|风大",
    "current": r"当前气温|气温|温度|多少度|几度",
}
_WEATHER = re.compile(
    r"(?P<place>.*?)"
    + "(?:"
    + "|".join(f"(?P<{field}>{pattern})" for field, pattern in _WEATHER_TERMS.items())
    + r")(?:大概|预计)?(?:怎么样|如何|呢|吗|多少度|几度|多少|是多少度?|是几度|多大)?"
)
_DAYS = ("今天", "明天", "后天")
_OTHER_DATES = re.compile(
    r"大后天|昨[天日]|前天|上周|下周|周末|星期[一二三四五六日天]|\d+[月日号]|\d{4}-\d{1,2}-\d{1,2}"
)
_NO_CITY_REPLY = {"谢谢", "不用了", "算了", "不了", "随便", "不知道", "取消", "好的", "可以"}
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
    city, relative, metric = "", None, "overview"
    target = None
    weather_text = re.sub(
        r"^(?:请问|请帮我查一下|帮我查一下|查一下|那么|那(?=今天|明天|后天|最低|最高|温度|气温|体感|湿度|风|会|下雨))",
        "",
        text,
    ).strip()
    weather_text = weather_text.replace("今日", "今天").replace("明日", "明天")
    if match := _WEATHER.fullmatch(weather_text):
        metric = match.lastgroup
        prefix = match["place"].strip()
        if _PRIVATE.search(prefix):
            return None
        if _OTHER_DATES.search(prefix):
            return {"query": "", "unsupported_weather_period": True}
        if metric == "rain":
            prefix = re.sub(r"(?:会不会|会|要不要|要)$", "", prefix)
        now = None
        if metric == "current":
            now = re.search(r"(?:现在|此刻|目前)$", prefix)
            prefix = re.sub(r"(?:现在|此刻|目前)$", "", prefix)
        relative = next((day for day in _DAYS if day in prefix), None)
        city = (prefix.replace(relative, "", 1) if relative else prefix).strip()
        relative = relative or ("今天" if now else None)
        city = city.removesuffix("的").strip()
        if city in {"这里", "当地", "这边"}:
            city = ""
        if city and not re.fullmatch(_CITY, city):
            return None
        if not city and not relative and not previous and match[metric] in {"最低", "最高"}:
            return None
        target = previous.get("weather_date") if not relative else None
        city = city or previous.get("city", "")
        relative = relative or previous.get("relative", "今天")
    elif (follow := re.fullmatch(r"(?:那|那么)?(今天|明天|后天)呢", text)) and previous.get("city"):
        city, relative = previous["city"], follow[1]
    elif previous.get("missing_city"):
        location = re.fullmatch(rf"(?:我在|查一下|查)?({_CITY})(?:市)?", text)
        if location and text not in _NO_CITY_REPLY:
            city, relative, metric = location[1], previous["relative"], previous["metric"]
            target = previous["weather_date"]
    elif previous.get("city") and (
        change := re.fullmatch(rf"(?:换成|换到|改查)\s*({_CITY})(?:呢)?", text)
    ):
        city, relative = change[1].removesuffix("呢"), previous["relative"]
        target = previous["weather_date"]
    if relative:
        if _PRIVATE.search(city):
            return None
        if not target:
            target = (date.fromisoformat(today) + timedelta(days=_DAYS.index(relative))).isoformat()
        return {
            "city": city,
            "relative": relative,
            "weather_date": target,
            "metric": metric,
            "query": f"{city} {relative} {target} weather forecast" if city else "",
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
    if explicit and (explicit["query"] or explicit.get("unsupported_weather_period")):
        return explicit, None
    if not explicit and not re.fullmatch(
        rf"(?:我在|查一下|查)?({_CITY})(?:[？?。！!])?", current["text"].strip()
    ):
        return None, None
    last = conn.execute(
        "SELECT turn_id,text,created_at,status,pending_clarification,final_cards FROM turns "
        "WHERE session_id=%s AND user_id=%s AND source_seq<%s AND status IN ('completed','waiting_user','resolved') "
        "ORDER BY source_seq DESC LIMIT 1",
        (execution.session_id, execution.auth.user_id, execution.source_seq),
    ).fetchone()
    previous, origin = None, None
    if last and last["status"] == "completed":
        previous = next(
            (
                card
                for card in last["final_cards"]
                if card["kind"] == "public_web" and card.get("city") and card.get("weather_date")
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
            previous = {**pending, "missing_city": True}
            if explicit is None:
                origin = str(last["turn_id"])
    return request(current["text"], today, previous), origin


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
            lines = [
                line.strip()
                for line in excerpt.splitlines()
                if line.strip() and not line.startswith("#")
            ]
            summary = next(iter(lines), "")
            row = {"title": match["title"][:180], "url": url, "excerpt": summary[:340]}
            if weather_date:
                # The MCP often puts the daily range below its current-conditions paragraph.
                details = [
                    line
                    for line in lines[1:]
                    if re.match(
                        r"^(?:[-*]\s*)?(?:Currently|High|Low|Humidity|Wind|Feels like|Precipitation|Chance of rain|Rain chance)\b",
                        line,
                        re.I,
                    )
                ]
                if details:
                    row["weather_details"] = "\n".join(details)[:640]
            rows.append(row)
            if weather_date or len(rows) == 3:
                return rows
    return rows
