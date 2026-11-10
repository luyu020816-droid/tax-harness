"""Read-only Exa MCP adapter for user-requested public information."""

import json
import re
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx

from app.errors import AppError
from app.sources import read_source

ENDPOINT = "https://mcp.exa.ai/mcp?tools=web_search_exa"
_CITY = r"[\u4e00-\u9fff]{2,12}|[A-Za-z][A-Za-z .-]{1,30}"
_DAYS = ("今天", "明天", "后天")
_OTHER_DATES = re.compile(
    r"大后天|昨[天日]|前天|上周|下周|周末|星期[一二三四五六日天]|\d+[月日号]|\d{4}-\d{1,2}-\d{1,2}"
)
_PUBLIC = re.compile(
    r"(?:请)?(?:帮我)?(?:联网|上网)(?:搜索|查询|查一下|搜一下|查|搜)\s*[:：]?\s*(.{1,200})"
)
_PRIVATE = re.compile(
    r"发票|税号|开户|银行|企业|公司|知识库|上传|附件|上[一个]轮|刚才|密码|密钥|"
    r"(?i:invoice|password|api.?key)|\d{7,}|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}"
)
_WEATHER_EVIDENCE = re.compile(
    r"天气|气温|温度|温差|几度|冷热|下雨|下雪|雨伞|淋雨|降雨|降水|阴晴|多云|湿度|风速|风力|风大|体感|(?i:\bweather\b)"
)
_WEATHER_FOLLOWUP = re.compile(r"最低|最高|多低|多高|今天|明天|后天|换成|换到|改查|现在")


def authorized_query(conn, execution, args):
    current = conn.execute(
        "SELECT text,created_at,upload_ids,knowledge_upload_ids FROM turns "
        "WHERE turn_id=%s AND user_id=%s AND session_id=%s",
        (execution.turn_id, execution.auth.user_id, execution.session_id),
    ).fetchone()
    question = args["question"].strip()
    # A quote alone must not let the model strip a negation or quote an example.
    original = current["text"] if current else ""
    unquoted = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"', "", original)
    if (
        not current
        or current["upload_ids"]
        or current["knowledge_upload_ids"]
        or not question
        or question not in unquoted
        or re.search(
            r"不要|不用|别(?:查|搜|联网)|禁止|无需|不必|不(?:要)?联网|假设|例如|比如", unquoted
        )
        or _PRIVATE.search(question)
    ):
        raise AppError("PUBLIC_WEB_SCOPE_REQUIRED", "请提供本轮用户明确请求的公开查询原话。", 422)
    if args["kind"] == "search":
        explicit = _PUBLIC.fullmatch(question.strip("？?。！!"))
        if not explicit or not explicit[1].strip():
            raise AppError("PUBLIC_WEB_SCOPE_REQUIRED", "请确认需要联网查询的公开关键词。", 422)
        # The transmitted query comes from the user's explicit request, never from model prose.
        return {"query": explicit[1].strip()}

    last = conn.execute(
        "SELECT turn_id,text,created_at,status,pending_clarification,final_cards,final_response_ref FROM turns "
        "WHERE session_id=%s AND user_id=%s AND source_seq<%s "
        "AND status IN ('completed','waiting_user','resolved') ORDER BY source_seq DESC LIMIT 1",
        (execution.session_id, execution.auth.user_id, execution.source_seq),
    ).fetchone()
    previous, origin = None, None
    if last and last["final_response_ref"]:
        previous = next(
            (
                card
                for card in last["final_cards"]
                if card["kind"] == "public_web" and card.get("city")
            ),
            None,
        )
        if (
            last["status"] == "waiting_user"
            and (last["pending_clarification"] or {}).get("missing_fields") == ["location"]
            and not _WEATHER_EVIDENCE.search(question)
        ):
            origin = str(last["turn_id"])
    city = (args.get("city") or "").strip().removesuffix("的")
    if origin or (previous and city and city not in question):
        read_source(conn, execution.auth, str(last["final_response_ref"]))
    if not (
        _WEATHER_EVIDENCE.search(question)
        or (previous and _WEATHER_FOLLOWUP.search(question))
        or (origin and city and city in question)
    ):
        raise AppError("PUBLIC_WEB_SCOPE_REQUIRED", "本轮缺少可核对的天气请求。", 422)
    if not city:
        raise AppError("PUBLIC_WEB_CITY_REQUIRED", "请补充要查询天气的城市。", 422)
    if (
        not re.fullmatch(_CITY, city)
        or _PRIVATE.search(city)
        or (city not in question and (not previous or city != previous["city"]))
    ):
        raise AppError("PUBLIC_WEB_SCOPE_REQUIRED", "请确认用户提供的城市名称。", 422)
    if _OTHER_DATES.search(question):
        raise AppError("PUBLIC_WEB_WEATHER_PERIOD", "目前可查今天、明天和后天的天气。", 422)
    today = current["created_at"].astimezone(ZoneInfo("Asia/Shanghai")).date()
    offset = args.get("days_ahead", 0)
    explicit_day = next((index for index, word in enumerate(_DAYS) if word in question), None)
    if explicit_day is not None and explicit_day != offset:
        raise AppError(
            "PUBLIC_WEB_SCOPE_REQUIRED", "预报日期与本轮问句不一致，请按明确日期查询。", 422
        )
    if explicit_day is None and re.search(r"现在|此刻|目前", question):
        offset = 0
    elif explicit_day is None and origin:
        prior_day = next((i for i, word in enumerate(_DAYS) if word in last["text"]), 0)
        prior_today = last["created_at"].astimezone(ZoneInfo("Asia/Shanghai")).date()
        offset = (prior_today + timedelta(days=prior_day) - today).days
    elif (
        explicit_day is None
        and previous
        and previous.get("weather_date")
        and (city not in question or re.search(r"换成|换到|改查", question))
    ):
        offset = (date.fromisoformat(previous["weather_date"]) - today).days
    if offset not in range(len(_DAYS)):
        raise AppError("PUBLIC_WEB_WEATHER_PERIOD", "目前可查今天、明天和后天的天气。", 422)
    target = (today + timedelta(days=offset)).isoformat()
    return {
        "city": city,
        "relative": _DAYS[offset],
        "weather_date": target,
        "metric": args.get("metric", "overview"),
        "query": f"{city} {_DAYS[offset]} {target} weather forecast",
        **({"clarification_turn_id": origin} if origin else {}),
    }


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
