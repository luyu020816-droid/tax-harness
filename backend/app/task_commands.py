"""Recognize explicit task commands and bind them to the selected task."""

import re
import unicodedata

_TASK_INTENT = {
    "query_month": "月度查询|发票查询|月度资料查询",
    "explain_rules": "规则说明|规则解释|规则查询",
    "complete_purposes": "用途补充|用途|发票用途",
}
_MONTH_NAMES = ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二")
_MONTH_WORDS = r"0?[1-9]|1[0-2]|[一二三四五六七八九]|十[一二]?"
_TASK_NOUN = r"(?:任务|待办|这项|这一项|该项)"
_TASK_REQUEST = r"(?:(?:请帮我|帮我|请|只|仅|先|现在))*"
_CLEAR_FIELDS = {
    "show_columns": "展示列|显示列|show_columns",
    "pending": "待复核事项|复核要求|pending",
    "period": "月份|办理月份|period",
}


def authorize_task_clear(text, fields, *, target=""):
    """Accept positive, named field removal with wording supported by the workbench."""
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).rstrip("。！!")
    clauses = re.split(r"[，,；;、]", text)
    matches = {
        field
        for field in fields
        if field in _CLEAR_FIELDS
        and any(
            re.fullmatch(
                rf"{_TASK_REQUEST}(?:清空|清除|删除){target}(?:{_CLEAR_FIELDS[field]})", clause
            )
            for clause in clauses
        )
    }
    return len(clauses) == len(matches) and matches == set(fields)


def _task_selector(task, aliases, *, replacement=False, explicit_company=False):
    company = "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
    company_selector = f"(?:(?:{company})(?:的)?)" + ("" if explicit_company else "?")
    period = task["period"]
    when = ""
    if period:
        year, padded_month = period.split("-")
        month = int(padded_month)
        when = (
            rf"(?:(?:{year}年)?(?:{padded_month}|{month}|{_MONTH_NAMES[month - 1]})月"
            rf"|{year}[-/](?:{padded_month}|{month})月?)(?:的)?"
        )
        if not replacement:
            when = f"(?:{when})?"
    intent = f"(?:{_TASK_INTENT[task['intent']]})" + ("" if replacement else "?")
    return rf"(?:当前|本次|这次|这个|这条|此|该)?{company_selector}{when}{intent}{_TASK_NOUN}"


def authorize_task_addition(text, proposed, aliases):
    if not proposed["intent"]:
        return False
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).rstrip("。！!")
    target = _task_selector(proposed, aliases, replacement=True)
    return bool(
        re.fullmatch(
            rf"{_TASK_REQUEST}(?:另建|新增|再建|追加){target}(?:[，,；;]保留(?:已有|原有)待办)?",
            text,
        )
    )


def authorize_task_update(text, task, aliases, changes):
    target = _task_selector(task, aliases)
    clear = list(changes.get("clear_unresolved", ()))
    if changes.get("clear_period"):
        clear.append("period")
    if changes["intent"] not in (None, task["intent"]) or changes["unresolved"]:
        return False
    if clear:
        return changes["period"] is None and authorize_task_clear(
            text, clear, target=rf"{target}的?"
        )
    if not changes["period"]:
        return False
    year, month = changes["period"].split("-")
    date = rf"(?:{year}年(?:{month}|{int(month)})月|{year}[-/]{month})"
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).rstrip("。！!")
    return bool(
        re.fullmatch(
            rf"{_TASK_REQUEST}(?:把|将){target}(?:的)?(?:办理)?月份(?:改为|改成){date}", text
        )
    )


def task_intents(text):
    return {intent for intent, label in _TASK_INTENT.items() if re.search(label, text)}


def authorize_task_change(text, action, task, aliases, replacement=None, replacement_aliases=()):
    """Recognize one command whose scope agrees with the pending action."""
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    clauses = [part for part in re.split(r"[，,；;。！!]", text) if part]
    if not clauses:
        return False
    cross_company = replacement is not None and replacement["company_id"] != task["company_id"]
    target = _task_selector(task, aliases, explicit_company=cross_company)
    if action == "replace":
        if replacement is None or not replacement["intent"]:
            return False
        new_target = _task_selector(
            replacement, replacement_aliases, replacement=True, explicit_company=cross_company
        )
        return len(clauses) == 1 and bool(
            re.fullmatch(
                rf"{_TASK_REQUEST}(?:把)?{target}(?:替换为|换成|换为){new_target}", clauses[0]
            )
        )

    verb = {"cancel": "取消|终止", "delete": "删除"}[action]
    if not re.fullmatch(rf"{_TASK_REQUEST}(?:(?:{verb}){target}|把{target}(?:{verb}))", clauses[0]):
        return False
    for tail in clauses[1:]:
        if tail == "不再继续办理" or (action == "cancel" and tail == "剩下的发票不办了"):
            continue
        preserved = re.fullmatch(rf"({_MONTH_WORDS})月(?:的任务|这项|待办)?(?:继续)?保留", tail)
        if preserved and task["period"]:
            token = preserved[1]
            month = int(token) if token.isdecimal() else _MONTH_NAMES.index(token) + 1
            if month != int(task["period"][5:]):
                continue
        return False
    return True
