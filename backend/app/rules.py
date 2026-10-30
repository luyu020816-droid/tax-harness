from datetime import date
from decimal import Decimal, InvalidOperation

from app.errors import AppError
from app.identity import Identity, assert_access
from app.values import jsonb, month_bounds, uid

TOPICS = {"amounts", "purpose", "bank"}
CONDITION_TOPICS = {
    "money_summary": "amounts",
    "bank_read_only": "bank",
    "purpose_all": "purpose",
    "purpose_threshold": "purpose",
}


def resolve_in(conn, auth: Identity, company_id: str, period: str, topics: list[str]) -> list[dict]:
    assert_access(conn, auth, company_id)
    if not topics or not set(topics) <= TOPICS:
        raise AppError("INVALID_ARGUMENT", "规则主题须为 amounts、purpose 或 bank。")
    start, end = month_bounds(period)
    result = []
    for topic in dict.fromkeys(topics):
        rows = conn.execute(
            "SELECT r.* FROM rule_versions r JOIN rule_scopes s USING(rule_version_id) "
            "WHERE s.company_id=%s AND r.topic=%s AND r.status='active' "
            "AND r.effective_from<=%s AND (r.effective_to IS NULL OR r.effective_to>=%s) "
            "ORDER BY r.rule_code",
            (company_id, topic, start, end),
        ).fetchall()
        if not rows:
            raise AppError(
                "RULE_UNAVAILABLE",
                "当前范围缺少适用规则，不能据此作业务判断。",
                422,
                details={"company_id": company_id, "period": period, "topic": topic},
            )
        if len(rows) != 1:
            raise AppError("RULE_CONFLICT", "存在冲突的有效规则，需要维护人员处理。", 409)
        from app.knowledge_admin import validate_rule_refs

        result.extend(
            {
                **row,
                "knowledge_refs": validate_rule_refs(
                    conn, str(row["rule_version_id"]), [company_id]
                ),
            }
            for row in rows
        )
    return result


def get_rules(db, auth: Identity, company_id: str, period: str, topics: list[str]) -> dict:
    with db.transaction(readonly=True) as conn:
        rules = resolve_in(conn, auth, company_id, period, topics)
        head = conn.execute(
            "SELECT * FROM company_heads WHERE company_id=%s", (company_id,)
        ).fetchone()
        return {"kind": "rules", "company_id": company_id, "period": period, "rules": rules, **head}


def rule_ref(rule: dict) -> dict:
    result = {
        key: rule[key]
        for key in (
            "rule_version_id",
            "rule_code",
            "display_name",
            "business_version",
            "content_revision",
            "full_text",
            "effective_from",
            "effective_to",
            "status",
        )
    }
    if rule.get("knowledge_refs"):
        result["knowledge_refs"] = rule["knowledge_refs"]
    return result


def template(condition: dict) -> str:
    kind = condition.get("kind")
    allowed = {"kind", "threshold", "operator"} if kind == "purpose_threshold" else {"kind"}
    if set(condition) - allowed:
        raise AppError("RULE_UNSUPPORTED", "执行条件包含不支持的字段。", 422)
    if kind == "money_summary":
        return (
            "发票金额表示价税合计，按企业和月份汇总当前保存数据；银行收入与支出分别相加，三项不相互抵扣。"
            "金额使用十进制或整数分计算，单位元，保留两位小数。月份未明确时先追问。"
            "成功查询但没有记录时合计为0.00元并说明无记录；失败或无权限不能回答为零。"
            "发票与银行流水无逐条对应关系，不据金额推断付款或欠款。"
        )
    if kind == "bank_read_only":
        return (
            "企业编号和银行记录编号共同定位记录，按交易日期确定月份。"
            "收入和支出金额均为正数，按方向分别汇总，支出不重复减负号。"
            "银行记录全部只读；查询检查用户权限和企业范围，不据对方名称、备注或同额推断发票已付款。"
        )
    if kind == "purpose_all":
        return "每张发票都必须填写用途，不设置金额门槛。空白或只有空白字符视为未填写，备注不能代替用途。"
    if kind == "purpose_threshold":
        try:
            amount = Decimal(condition["threshold"])
            if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
                raise ValueError
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise AppError(
                "RULE_UNSUPPORTED", "金额门槛须为非负、最多两位小数的十进制字符串。", 422
            ) from exc
        if condition.get("operator", "gte") != "gte":
            raise AppError("RULE_UNSUPPORTED", "首版仅支持大于或等于的用途门槛。", 422)
        return f"单张发票金额大于或等于 {amount:.2f} 元时必须填写用途；低于此金额可以不填。空白或只有空白字符视为未填写，备注不能代替用途。"
    raise AppError("RULE_UNSUPPORTED", "该执行条件尚未实现，规则只能保留草稿。", 422)


def create_version(conn, data: dict) -> dict:
    companies = data.get("companies", [])
    if not companies:
        raise AppError("INVALID_ARGUMENT", "规则必须指定适用企业。")
    try:
        start = date.fromisoformat(data["effective_from"])
        end = date.fromisoformat(data["effective_to"]) if data.get("effective_to") else None
        if end and end <= start:
            raise ValueError
    except (KeyError, ValueError) as exc:
        raise AppError("INVALID_ARGUMENT", "规则生效区间无效。") from exc
    condition = data.get("condition_json", {})
    condition_topic = CONDITION_TOPICS.get(condition.get("kind"))
    topic = data.get("topic", condition_topic or "purpose")
    if topic not in TOPICS or (condition_topic and topic != condition_topic):
        raise AppError("INVALID_ARGUMENT", "规则主题与执行条件不一致。")
    supported = start.day == 1 and (end is None or end.day == 1)
    try:
        supported = supported and data.get("full_text") == template(condition)
    except AppError:
        supported = False
    for company in sorted(companies):
        if not conn.execute("SELECT 1 FROM companies WHERE company_id=%s", (company,)).fetchone():
            raise AppError("INVALID_ARGUMENT", "规则包含不存在的企业。")
    row = conn.execute(
        "INSERT INTO rule_versions(rule_version_id,rule_code,display_name,business_version,effective_from,effective_to,status,topic,full_text,condition_json,source_ref) "
        "VALUES(%s,%s,%s,%s,%s,%s,'draft',%s,%s,%s,%s) RETURNING *",
        (
            uid(),
            data["rule_code"],
            data["display_name"],
            data["business_version"],
            start,
            end,
            topic,
            data.get("full_text", ""),
            jsonb(condition),
            data.get("source_ref", "local-admin"),
        ),
    ).fetchone()
    for company in companies:
        conn.execute("INSERT INTO rule_scopes VALUES(%s,%s)", (row["rule_version_id"], company))
    return {
        **row,
        "can_activate": supported,
        "reason": None
        if supported
        else "正文、条件或日期不符合首版可执行模板，已保存草稿，未启用。",
    }


def activate(conn, version_id: str, replaces: str | None = None) -> dict:
    row = conn.execute(
        "SELECT * FROM rule_versions WHERE rule_version_id=%s", (version_id,)
    ).fetchone()
    if not row:
        raise AppError("RESOURCE_NOT_FOUND", "规则版本不存在。", 404)
    if row["status"] != "draft" or row["full_text"] != template(row["condition_json"]):
        raise AppError("RULE_UNSUPPORTED", "只有通过受控模板校验的草稿可启用。", 422)
    if row["effective_from"].day != 1 or (row["effective_to"] and row["effective_to"].day != 1):
        raise AppError("RULE_UNSUPPORTED", "首版可执行规则的日期边界必须为月首。", 422)
    scopes = conn.execute(
        "SELECT company_id FROM rule_scopes WHERE rule_version_id=%s ORDER BY company_id",
        (version_id,),
    ).fetchall()
    companies = [r["company_id"] for r in scopes]
    conn.execute(
        "SELECT * FROM company_heads WHERE company_id=ANY(%s) ORDER BY company_id FOR UPDATE",
        (companies,),
    )
    row = conn.execute(
        "SELECT * FROM rule_versions WHERE rule_version_id=%s FOR UPDATE", (version_id,)
    ).fetchone()
    if row["status"] != "draft":
        raise AppError("RULE_CONFLICT", "规则版本已被其他维护操作更改。", 409)
    if CONDITION_TOPICS.get(row["condition_json"].get("kind")) != row["topic"]:
        raise AppError("RULE_UNSUPPORTED", "规则主题与执行条件不一致。", 422)
    from app.knowledge_admin import validate_rule_refs

    validate_rule_refs(conn, version_id)
    if replaces:
        old = conn.execute(
            "SELECT * FROM rule_versions WHERE rule_version_id=%s", (replaces,)
        ).fetchone()
        old_companies = {
            r["company_id"]
            for r in conn.execute(
                "SELECT company_id FROM rule_scopes WHERE rule_version_id=%s", (replaces,)
            ).fetchall()
        }
        if not old or old["topic"] != row["topic"] or old_companies != set(companies):
            raise AppError("RULE_SCOPE_MISMATCH", "替换须保持规则主题和完整企业范围一致。", 409)
        if old["status"] != "active":
            raise AppError("RULE_CONFLICT", "被替换版本已不处于启用状态。", 409)
    conflicts = conn.execute(
        "SELECT DISTINCT r.rule_version_id FROM rule_versions r JOIN rule_scopes s USING(rule_version_id) "
        "WHERE s.company_id=ANY(%s) AND r.topic=%s AND r.status='active' AND r.rule_version_id<>%s "
        "AND daterange(r.effective_from,r.effective_to,'[)') && daterange(%s,%s,'[)')",
        (companies, row["topic"], version_id, row["effective_from"], row["effective_to"]),
    ).fetchall()
    if any(str(c["rule_version_id"]) != replaces for c in conflicts):
        raise AppError("RULE_CONFLICT", "存在未明确替换的有效规则。", 409)
    if replaces:
        conn.execute(
            "UPDATE rule_versions SET status='disabled',content_revision=content_revision+1 WHERE rule_version_id=%s",
            (replaces,),
        )
    conn.execute(
        "UPDATE rule_versions SET status='active',content_revision=content_revision+1 WHERE rule_version_id=%s",
        (version_id,),
    )
    conn.execute(
        "UPDATE company_heads SET rules_epoch=rules_epoch+1 WHERE company_id=ANY(%s)", (companies,)
    )
    return conn.execute(
        "SELECT * FROM rule_versions WHERE rule_version_id=%s", (version_id,)
    ).fetchone()


def disable(conn, version_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM rule_versions WHERE rule_version_id=%s", (version_id,)
    ).fetchone()
    if not row:
        raise AppError("RESOURCE_NOT_FOUND", "规则不存在。", 404)
    companies = [
        r["company_id"]
        for r in conn.execute(
            "SELECT company_id FROM rule_scopes WHERE rule_version_id=%s", (version_id,)
        ).fetchall()
    ]
    conn.execute(
        "SELECT * FROM company_heads WHERE company_id=ANY(%s) ORDER BY company_id FOR UPDATE",
        (companies,),
    )
    conn.execute(
        "UPDATE rule_versions SET status='disabled',content_revision=content_revision+1 WHERE rule_version_id=%s",
        (version_id,),
    )
    conn.execute(
        "UPDATE company_heads SET rules_epoch=rules_epoch+1 WHERE company_id=ANY(%s)", (companies,)
    )
    return {"rule_version_id": version_id, "status": "disabled"}
