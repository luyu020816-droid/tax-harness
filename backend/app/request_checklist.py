"""Bounded checks for explicit business requests, never a business dispatcher.

The model still chooses tools. This independent checklist catches omissions in
supported, unambiguous clauses; it does not claim general intent understanding.
No checklist item grants write authority or changes a persistent task.
"""

import re

from app.errors import AppError
from app.request_evidence import request_periods

VIEWS = {
    "query_month": ("monthly_summary", "查询月度资料", "query_month"),
    "check_purposes": ("purpose_check", "检查发票用途", "complete_purposes"),
    "get_rules": ("rules", "解释适用规则", "explain_rules"),
}
QUOTES = re.compile(r'“[^”]*”|「[^」]*」|"[^"]*"|‘[^’]*’')
SKIP = re.compile(
    r"不要|别查|无需|不用|不查询|不检查|不解释|例如|比如|示例|假设|如果|以前|历史|原话|上次|过去"
)


def compile_items(text, companies, selected_company, available, created_at=None):
    """Recognize only explicit read/check requests with unambiguous scopes.

    Quoted instructions, negative/conditional/history clauses and paired
    multi-company/multi-month clauses are deliberately not interpreted.
    """
    text = QUOTES.sub(" ", text)
    items = []
    inherited = None
    for clause in re.split(r"[，,；;。\n]|并且|同时|并(?=解释|说明|检查|查询|查一下)", text):
        if not clause.strip() or SKIP.search(clause):
            continue
        tools = []
        if re.search(r"查询|查一下|查下|查看|汇总|统计", clause) and re.search(
            r"月度|发票总额|发票金额|银行收[入支]|银行支出|收支", clause
        ):
            tools.append("query_month")
        if (
            not re.search(
                r"(?<![A-Za-z0-9])F\d+(?![A-Za-z0-9])|第[一二三四五六七八九十\d]+张|这张|该票|此票",
                clause,
            )
            and re.search(r"检查|核对|查一下|查询|查下", clause)
            and re.search(r"用途|待补项|必填项", clause)
        ):
            tools.append("check_purposes")
        if re.search(r"解释|说明|查看|查询", clause) and re.search(r"规则|必填依据", clause):
            tools.append("get_rules")
        named = [c["company_id"] for c in companies if any(a in clause for a in c["aliases"] if a)]
        periods = sorted(request_periods(clause, created_at))
        if len(named) > 1 and len(periods) > 1:
            inherited = None
            continue  # Do not invent the cross product of paired scopes.
        if named and periods:
            scopes = [(c, p) for c in named for p in periods]
        elif periods and not named and inherited and len({c for c, _ in inherited}) == 1:
            # An explicit continuation month keeps the preceding company,
            # even when the UI currently selects a different company.
            scopes = [(inherited[0][0], p) for p in periods]
        elif inherited and not named and not periods:
            scopes = inherited
        else:
            candidates = [(s["company_id"], s["period"]) for s in available]
            scopes = [
                (c, p)
                for c, p in candidates
                if (not named or c in named) and (not periods or p in periods)
            ]
            if not named and not periods and len(scopes) > 1:
                scopes = []
            if not scopes:
                targets = named or ([selected_company] if selected_company else [None])
                scopes = [(c, p) for c in targets for p in (periods or [None])]
        if named or periods:
            inherited = scopes
        for tool in tools:
            for company, period in scopes:
                identity = (tool, company, period)
                if any((i["tool"], i["company_id"], i["period"]) == identity for i in items):
                    continue
                items.append(
                    {
                        "tool": tool,
                        "view": VIEWS[tool][0],
                        "label": VIEWS[tool][1],
                        "company_id": company,
                        "period": period,
                        "source_quote": clause.strip(),
                    }
                )
    return items


def verify_items(items, cards, clarification, failures):
    """Only delivered evidence, scoped tool failure or necessary clarification closes an item."""
    progress, missing = [], []
    for item in items:
        identity = (item["view"], item["company_id"], item["period"])
        delivered = any(
            (c["kind"], c.get("company_id"), c.get("period")) == identity for c in cards
        )
        failure = next(
            (
                f
                for f in failures
                if (f["name"], f["arguments"].get("company_id"), f["arguments"].get("period"))
                == (item["tool"], item["company_id"], item["period"])
            ),
            None,
        )
        needs = [k for k in ("company_id", "period") if item[k] is None]
        waiting = bool(
            needs
            and clarification
            and clarification.intent in {v[2] for v in VIEWS.values()}
            and set(needs) <= set(clarification.missing_fields)
            and all(
                item[k] is None or getattr(clarification, k) == item[k]
                for k in ("company_id", "period")
            )
        )
        status = (
            "verified"
            if delivered
            else "blocked"
            if failure
            else "waiting_user"
            if waiting
            else "pending"
        )
        row = {**item, "status": status}
        if failure and not delivered:
            row["reason"] = failure["error"]["message"]
        if status == "pending":
            missing.append(row)
        progress.append(row)
    if missing:
        raise AppError(
            "REPLY_EVIDENCE_MISSING",
            "本轮明确要求的事项尚未交付，不能将部分结果当作全部完成。",
            422,
            details={
                "request_items": missing,
                "instruction": "补办列出的事项并展示真实结果；缺少企业或月份时必要追问。"
                "不要重复保存已提交操作，不要用无关说明或虚构澄清跳过事项。",
            },
        )
    return progress
