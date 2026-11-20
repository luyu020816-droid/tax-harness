"""Bind workbook filters and columns to the user's request before computing."""

import re
from datetime import date, timedelta, timezone
from itertools import groupby

from app.business_dates import DAY, calendar_day
from app.errors import AppError
from app.xlsx_workbooks import clean

MONTH = re.compile(
    r"(?<!\d)(?:(\d{4})\s*(?:年\s*|[-/])\s*(\d{1,2})\s*月?"
    r"|(?:(\d{4})\s*年\s*)?([一二三四五六七八九十]{1,2}|\d{1,2})\s*月)(?!\d)"
)
MONTH_NUMBERS = {
    word: i for i, word in enumerate("一 二 三 四 五 六 七 八 九 十 十一 十二".split(), 1)
}
ALL_DATES = re.compile(r"全部日期|不[限按]日期|所有日期|整[张个]表|全表|所有(?:明细|行|记录)")
RELATIVE = re.compile(r"本月|这个月|上个?月|今年|去年|本周|上周|今天|昨天|近\s*\d+\s*[天月]")
GROUP = re.compile(r"(?:分|按|每[个种])\s*(?:费用)?(?:类别|类型)")
NO_GROUP = re.compile(
    r"只(?:要|看)(?:合计|总[计额])|(?:不|不用|不要)分组|(?:不|不用|不要)[按分](?:费用)?(?:类别|类型)"
)
ALL_CATEGORIES = re.compile(r"(?:全部|所有|不限)(?:费用)?(?:类别|类型)|取消(?:类别|费用)筛选")
COLUMN_REF = re.compile(r"(?<![A-Za-z])([A-Z]{1,2})\s*列", re.I)
ONLY = re.compile(r"(?:只|仅)(?:看|查|算|统计|汇总)")
CATEGORY_FILTER = re.compile(
    r"(?:排除|不含|不包括|不算)|[^\s，,。;]{1,16}(?:费|费类)(?=合计|多少|[，,。;\s]|$)"
)


def _clarify(message):
    raise AppError("NEEDS_CLARIFICATION", message, 422)


def _check(field, actual, expected, label):
    if actual != expected:
        raise AppError(
            "WORKBOOK_SCOPE_MISMATCH",
            f"工具{label}与用户确认的口径不符。",
            422,
            details={"field": field, "expected": expected, "actual": actual},
        )


def _mentioned(options, text):
    names = {name for name in options.values() if name in text}
    matched = set()
    for _, peers in groupby(sorted(names, key=len, reverse=True), key=len):
        present = [name for name in peers if name in text]
        matched.update(present)
        for name in present:
            text = text.replace(name, " ")
    return {key for key, name in options.items() if name in matched}


def _date_scope(text, previous, created_at):
    days = list(DAY.finditer(text))
    remainder = DAY.sub(" ", text)
    months = list(MONTH.finditer(remainder))
    relative = set(RELATIVE.findall(remainder))
    entire = bool(ALL_DATES.search(remainder))
    noninclusive = re.search(
        r"不(?:含|包括)(?:结束|截止)[日天]|以前|之前|之后|以后|截至|截止到", text
    )
    if (
        sum(map(bool, (days, months, relative, entire))) > 1
        or (
            (days or months or relative or entire)
            and re.search(r"不要查|别查|不查询|不是|例如|比如|示例", text)
        )
        or ((days or months) and noninclusive)
    ):
        _clarify("请确认唯一的计算日期范围。")
    try:
        if entire:
            return True, None, None
        if days:
            if (
                len(days) > 2
                or (
                    len(days) == 2
                    and not re.fullmatch(
                        r"\s*(?:至|到|~|—|–|-)\s*", text[days[0].end() : days[1].start()]
                    )
                )
                or (
                    len(days) == 1 and re.match(r"\s*(?:至|到|~|—|–|-)\s*\d", text[days[0].end() :])
                )
            ):
                _clarify("请确认明确的起止日。")
            bounds = []
            for match in days:
                if not (match[1] or match[5] or bounds):
                    _clarify("请确认起始日的年份。")
                bounds.append(calendar_day(match, bounds[0].year if bounds else None))
            if bounds[0] > bounds[-1]:
                _clarify("起始日不能晚于结束日，请确认日期范围。")
            return False, bounds[0].isoformat(), (bounds[-1] + timedelta(days=1)).isoformat()
        if months:
            if len(months) != 1:
                _clarify("请确认要计算的唯一月份，或提供明确起止日。")
            numeric_year, numeric_month, chinese_year, native_month = months[0].groups()
            year, month = numeric_year or chinese_year, numeric_month or native_month
            if not year:
                start, end = previous.get("date_from"), previous.get("date_to_exclusive")
                if (
                    not start
                    or not end
                    or start[:4] != (date.fromisoformat(end) - timedelta(days=1)).isoformat()[:4]
                ):
                    _clarify("请确认该月份的年份。")
                year = start[:4]
            month = MONTH_NUMBERS.get(month, month)
            first = date(int(year), int(month), 1)
        elif relative:
            shifts = {"本月": 0, "这个月": 0, "上月": 1, "上个月": 1}
            if not created_at or len(relative) != 1 or not relative <= shifts.keys():
                _clarify("请提供一个明确的月份或起止日。")
            today = created_at.astimezone(timezone(timedelta(hours=8))).date()
            first = (today.replace(day=1) - timedelta(days=shifts[relative.pop()])).replace(day=1)
        else:
            return None
        end = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        return False, first.isoformat(), end.isoformat()
    except (ValueError, OverflowError) as exc:
        raise AppError("NEEDS_CLARIFICATION", "请确认日期范围中的有效日历日期。", 422) from exc


def _selection(options, selected, inherited, text, earlier, field, label, column=False):
    def explicit(question):
        named = _mentioned(options, question)
        if column:
            named |= {col.upper() for col in COLUMN_REF.findall(question)} & options.keys()
        if named and re.search(r"(?:不要|别|不)用|排除", question):
            _clarify(f"请明确要使用的{label}。")
        return named

    matches = explicit(text) or explicit(earlier)
    if len(matches) > 1:
        _clarify(f"请确认要使用哪一个{label}。")
    if matches:
        _check(field, selected, next(iter(matches)), label)
    elif len(options) > 1:
        if inherited in options:
            _check(field, selected, inherited, label)
        else:
            _clarify(
                "请确认要计算哪一个工作表。" if not column else "请确认使用哪一列作为计算口径。"
            )


def _category_scope(labels, text, earlier, previous):
    for question in (text, earlier):
        question = NO_GROUP.sub("", question)
        matches = _mentioned({label: label for label in labels}, question)
        if len(matches) > 1 or (
            matches and re.search(r"排除|不含|不包括|不算|不要|别查|不查", question)
        ):
            _clarify("请确认要计算的单一费用类别。")
        if ALL_CATEGORIES.search(question):
            if matches:
                _clarify("请确认是查询指定类别还是全部费用类别。")
            return None
        if matches:
            return next(iter(matches))
        tails = [question[m.end() :].lstrip() for m in ONLY.finditer(question)]
        if CATEGORY_FILTER.search(question) or any(
            not (
                tail.startswith(("实付", "实际支付", "合计", "总计"))
                or any(pattern.match(tail) for pattern in (DAY, MONTH, RELATIVE, ALL_DATES))
            )
            for tail in tails
        ):
            _clarify("请确认要查询的费用类别，使用表格中的类别名称。")
    return previous.get("category")


def validate_request(query, snapshot, text, previous, *, created_at=None, prior_source=None):
    text = clean(text)
    earlier = clean(prior_source["content"]) if prior_source else ""
    sheets = {s["name"]: s for s in snapshot["sheets"]}
    choices = {name: clean(name) for name, s in sheets.items() if not s["reason"]}
    names = {name: clean(name) for name in sheets}
    for name in _mentioned(names, text) or _mentioned(names, earlier):
        choices[name] = clean(name)
    _selection(choices, query["sheet"], previous.get("sheet"), text, earlier, "sheet", "工作表")
    sheet = sheets.get(query["sheet"])
    if not sheet or sheet["reason"]:
        raise AppError(
            "WORKBOOK_SHEET_UNAVAILABLE",
            sheet["reason"] if sheet else "未找到可查询工作表。",
            422,
        )
    if any(
        query["columns"].get(field) not in options for field, options in sheet["columns"].items()
    ):
        raise AppError("WORKBOOK_COLUMN_REQUIRED", "请确认表格日期、费用类别和金额列。", 422)
    recognized_columns = set().union(*(cols.keys() for cols in sheet["columns"].values()))
    explicit_columns = COLUMN_REF.findall(text) or COLUMN_REF.findall(earlier)
    if {col.upper() for col in explicit_columns} - recognized_columns:
        _clarify("请确认要使用的日期、费用类别或金额列，指定列不在候选列中。")
    prior_columns = previous.get("columns", {}) if previous.get("sheet") == query["sheet"] else {}
    for field, options in sheet["columns"].items():
        _selection(
            {key: clean(name) for key, name in options.items()},
            query["columns"][field],
            prior_columns.get(field),
            text,
            earlier,
            f"columns.{field}",
            "候选列",
            column=True,
        )
    amount = clean(sheet["columns"]["amount"][query["columns"]["amount"]])
    if re.search(r"实付|实际支付", f"{text} {earlier}") and not amount.startswith(
        ("实付金额", "实际支付金额")
    ):
        _clarify("当前金额列不能确认为实付，请确认计算口径。")

    scope = _date_scope(text, previous, created_at)
    if scope is None and prior_source:
        scope = _date_scope(earlier, previous, prior_source["created_at"])
    if scope is None:
        if not (previous.get("all_dates") or previous.get("date_from")):
            _clarify("请先提供日期范围，或明确要求计算全部日期。")
        scope = (
            bool(previous.get("all_dates")),
            previous.get("date_from"),
            previous.get("date_to_exclusive"),
        )
    for field, expected in zip(("all_dates", "date_from", "date_to_exclusive"), scope, strict=True):
        _check(
            field,
            query.get(field, False if field == "all_dates" else None),
            expected,
            "日期范围或月份",
        )

    labels = set()
    for row in sheet["rows"]:
        kind, label = row[1][query["columns"]["category"]] or (None, None)
        if kind == "text" and clean(label):
            labels.add(clean(label))
    category = _category_scope(labels, text, earlier, previous)
    requested = query.get("category")
    _check("category", clean(requested) if requested is not None else None, category, "费用类别")

    group_by = previous.get("group_by", "none")
    for question in (text, earlier):
        separate = bool(GROUP.search(NO_GROUP.sub("", question)))
        combined = bool(NO_GROUP.search(question))
        if (
            not separate
            and re.search(r"分组|分项|按[^，,。；;]{1,16}(?:拆分|拆开)", question)
            and not combined
        ):
            _clarify("当前支持按费用类别分组，请确认分组口径。")
        if separate and combined:
            _clarify("请确认是否需要按费用类别分组。")
        if separate or combined:
            group_by = "category" if separate else "none"
            break
    _check("group_by", query.get("group_by", "none"), group_by, "分组")
    return {
        "sheet": sheet["name"],
        "columns": query["columns"],
        "all_dates": scope[0],
        "date_from": scope[1],
        "date_to_exclusive": scope[2],
        "category": category,
        "group_by": group_by,
    }
