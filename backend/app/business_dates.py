"""Calendar dates and the common business clock for knowledge and workbooks."""

import re
from datetime import date, datetime, timedelta, timezone

from app.errors import AppError

BUSINESS_ZONE = "Asia/Shanghai"
BUSINESS_TZ = timezone(timedelta(hours=8), BUSINESS_ZONE)
DAY = re.compile(
    r"(?<!\d)(?:(\d{4})([-－/／])(\d{1,2})\2(\d{1,2})(?!\d)"
    r"|(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*[日号])"
)
DATE_LIKE = re.compile(r"\d{4}[-－/／.]\d{1,3}[-－/／.]\d+|\d{4}\s*年\s*\d+\s*月\s*\d+")
MONTH_LIKE = re.compile(
    r"\d{4}(?:[-－/／]\d{1,2}|\s*年\s*\d{1,2}\s*月)|\d{1,2}\s*月|本月|上月|上个月|这个月"
)
UNASSERTED_DAY = re.compile(r"(?:不要查|别查|不查询|不是|比如|例如|示例)\s*[：:，,]?\s*$")
WORKBOOK_DATE = re.compile(
    r"(?:工作簿|表格|文件|Excel)(?:版本)?(?:生效日期?|有效日期?|版本日期?|时点)\s*(?:为|是|在|[：:])?\s*",
    re.I,
)
BOOK_VERSION_AFTER = re.compile(
    r"^\s*(?:当日)?(?:有效|适用)的?(?:工作簿|表格|Excel|文件)?版本", re.I
)


def business_today(at=None):
    return (at or datetime.now(BUSINESS_TZ)).astimezone(BUSINESS_TZ).date()


def calendar_day(match, inherited_year=None):
    year, _, month, day, cn_year, cn_month, cn_day = match.groups()
    year = year or cn_year or inherited_year
    if not year:
        raise ValueError("missing calendar year")
    return date(int(year), int(month or cn_month), int(day or cn_day))


def _explicit_day(matches):
    try:
        parsed = {calendar_day(match) for match in matches}
    except ValueError as exc:
        raise AppError(
            "NEEDS_SCOPE_CLARIFICATION", "请提供完整且有效的资料生效日期。", 422
        ) from exc
    if len(parsed) > 1:
        raise AppError("NEEDS_SCOPE_CLARIFICATION", "请确认要采用哪一个资料生效日期。", 422)
    return next(iter(parsed), None)


def knowledge_point(text, period=None):
    matches = list(DAY.finditer(text))
    remainder = DAY.sub(" ", text)
    explicit = _explicit_day(matches)
    if DATE_LIKE.search(remainder):
        raise AppError("NEEDS_SCOPE_CLARIFICATION", "请提供完整且有效的资料生效日期。", 422)
    if explicit and (
        period
        or MONTH_LIKE.search(remainder)
        or any(UNASSERTED_DAY.search(text[: match.start()]) for match in matches)
    ):
        raise AppError(
            "NEEDS_SCOPE_CLARIFICATION",
            "请确认采用一个资料生效日期或一个完整业务月份。",
            422,
        )
    return explicit, [m[0] for m in matches]


def knowledge_date_basis(source, as_of, period):
    explicit, fragments = knowledge_point(source["content"], period)
    if as_of and (not explicit or explicit.isoformat() != as_of):
        raise AppError(
            "NEEDS_SCOPE_CLARIFICATION",
            "工具的资料生效日期需要与本轮原话一致。",
            422,
            details={"expected": explicit.isoformat() if explicit else None},
        )
    day = None if period else explicit or business_today(source["created_at"])
    return day, {
        "kind": "period" if period else "explicit" if explicit else "business_default",
        "fragments": fragments,
        "as_of": day.isoformat() if day else None,
        "timezone": BUSINESS_ZONE,
        "source_id": str(source["source_id"]),
    }


def workbook_reference(text):
    matches = [
        match
        for match in DAY.finditer(text)
        if (
            re.search(WORKBOOK_DATE.pattern + r"$", text[: match.start()], re.I)
            or BOOK_VERSION_AFTER.match(text[match.end() :])
        )
    ]
    try:
        day = _explicit_day(matches)
        if not matches and WORKBOOK_DATE.search(text):
            raise AppError("NEEDS_CLARIFICATION", "请提供完整的工作簿版本日期。", 422)
    except AppError as exc:
        raise AppError(
            "NEEDS_CLARIFICATION", "请确认一个完整且有效的工作簿版本日期。", 422
        ) from exc
    remainder = text
    for match in reversed(matches):
        remainder = remainder[: match.start()] + " " + remainder[match.end() :]
    return remainder, day, [match[0] for match in matches]
