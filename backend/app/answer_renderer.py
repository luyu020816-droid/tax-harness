"""Render program facts and receipts; models select evidence, never invent facts."""

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.errors import AppError
from app.identity import assert_access
from app.sources import read_source
from app.tool_projection import project_rule_repeats
from app.values import canonical, safe_json


def missing(message, *, details=None):
    raise AppError("REPLY_EVIDENCE_MISSING", message, 422, details=details or {})


def direct_conversation_topic(text):
    """Recognize standalone requests whose answers are known to the host."""
    text = re.sub(r"[\s？?。！!，,]", "", text)
    text = re.sub(r"^(?:请问|请告诉我)", "", text)
    match = re.fullmatch(
        r"(今天|明天)(?:是?(?:星期几|周几|几号|什么日期|哪一天)(?:具体日期|几月几号|几号|星期几|周几)?|的?具体日期)",
        text,
    )
    if match:
        return "today" if match[1] == "今天" else "tomorrow"
    if text in {"卡了", "卡了吗", "卡住了吗", "刚才卡了吗", "刚才卡住了吗", "是不是卡了"}:
        return "previous_turn_status"
    if text in {"你能做什么", "你可以做什么", "你有什么功能", "你能帮我做什么"}:
        return "capabilities"
    return None


def _conversation_answer(conn, execution, topic, frame):
    if topic == "capabilities":
        return "我可以查比赛样例里的发票和银行收支，解释适用规则，检查并补充发票用途或备注，也能继续你尚未完成的任务。"
    if topic in ("today", "tomorrow"):
        day = date.fromisoformat(frame["current_date"])
        if topic == "tomorrow":
            day += timedelta(days=1)
        label = "今天" if topic == "today" else "明天"
        return f"{label}是 {day.year} 年 {day.month} 月 {day.day} 日，星期{'一二三四五六日'[day.weekday()]}。"
    previous = conn.execute(
        "SELECT status FROM turns WHERE session_id=%s AND user_id=%s AND source_seq<%s "
        "AND status<>'deleted' AND request_kind<>'compact' ORDER BY source_seq DESC LIMIT 1",
        (execution.session_id, execution.auth.user_id, execution.source_seq),
    ).fetchone()
    return {
        "completed": "上一轮已完成。",
        "resolved": "上一轮已在后续回复中处理。",
        "waiting_user": "上一轮在等你补充信息。",
        "failed": "上一轮处理失败，未正常答完。可以在运行洞察查看原因。",
        "cancelled": "上一轮已取消。",
        "interrupted": "上一轮已中断。",
        "queued": "上一轮仍在排队。",
        "running": "上一轮仍在处理。",
    }.get(previous["status"] if previous else None, "当前会话没有可核对的上一轮处理状态。")


@dataclass
class RenderedReply:
    message: str
    status: str
    clarification: object = None
    resume_turn_id: object = None
    displayed_view_ids: list = field(default_factory=list)
    cards: list = field(default_factory=list)
    preference_action: str | None = None
    preference_status: str = "not_requested"


def _results(conn, execution, payload):
    results = {}
    observed = []
    for message in payload["messages"]:
        if message.get("role") != "tool":
            continue
        output = json.loads(message["content"])
        candidates = [output]
        if isinstance(output.get("data"), dict) and output["data"].get("post_save_check"):
            candidates.append(output["data"]["post_save_check"])
        for item in candidates:
            ref = item.get("result_ref")
            if not ref:
                continue
            source = read_source(conn, execution.auth, ref)
            if str(source["turn_id"]) != execution.turn_id:
                continue  # Historical tool evidence cannot authorize this turn's answer.
            if source["role"] != "tool":
                missing("工具结果与真实来源不一致。")
            results[ref] = json.loads(source["content"])
            observed.append((ref, item))
    projected = dict(zip(results, project_rule_repeats(list(results.values())), strict=True))
    for ref, item in observed:
        if item != results[ref] and item != projected[ref]:
            missing("工具结果与真实来源不一致。")
    return results


def _rules(data):
    rows = data.get("rule_refs", data.get("rules", []))
    return "、".join(f"《{r['display_name']}》{r['business_version']}版" for r in rows)


def _purpose_explanations(conn, refs):
    texts = {}
    for ref in refs:
        rule = conn.execute(
            "SELECT condition_json FROM rule_versions WHERE rule_version_id=%s AND status='active'",
            (ref["rule_version_id"],),
        ).fetchone()
        condition = rule["condition_json"] if rule else {}
        if condition.get("kind") == "purpose_all":
            texts["purpose_all"] = (
                "当前规则要求每张发票填写用途，不设金额门槛；空白视为未填，备注不能代替用途。"
            )
        if condition.get("kind") == "purpose_threshold":
            texts["purpose_threshold"] = (
                f"当前规则要求单张金额大于或等于{condition['threshold']}元时填写用途，正好达到门槛也算；低于门槛可不填，备注不能代替用途。"
            )
    return texts


_WEATHER_CONDITIONS = {
    "clear": "晴朗",
    "mainly clear": "大致晴朗",
    "sunny": "晴朗",
    "mostly sunny": "大致晴朗",
    "partly cloudy": "局部多云",
    "mostly cloudy": "多云",
    "cloudy": "多云",
    "overcast": "是阴天",
    "fog": "有雾",
    "foggy": "有雾",
    "drizzle": "有毛毛雨",
    "light drizzle": "有毛毛雨",
    "light rain": "有小雨",
    "rain": "有雨",
    "rain showers": "有阵雨",
    "snow": "有雪",
    "thunderstorm": "有雷雨",
}
_CELSIUS = r"(?:-?\d{1,3}(?:\.\d{1,2})?\s*°F\s*\()?(-?\d{1,3}(?:\.\d{1,2})?)\s*°C"
_WEATHER_LABELS = {
    "low": "最低气温",
    "high": "最高气温",
    "current": "气温",
    "feels": "体感温度",
    "rain": "降雨概率",
    "humidity": "湿度",
    "wind": "风速",
}


def _weather_text(data, today):
    forecast = date.fromisoformat(data["weather_date"])
    day = f"{forecast.month}月{forecast.day}日"
    if today:
        offset = (forecast - date.fromisoformat(today)).days
        day = {0: "今天", 1: "明天", 2: "后天"}.get(offset, day)
    city = data["city"].removesuffix("的").strip()
    if not data["results"]:
        return f"我暂时没查到{city}{day}的天气预报。"

    row = data["results"][0]
    excerpt = row["excerpt"]
    details = "\n".join((excerpt, row.get("weather_details", "")))
    condition = _WEATHER_CONDITIONS.get(re.split(r"[,.;]", excerpt, maxsplit=1)[0].strip().lower())
    current = re.match(rf"^(?:[a-z ]+,\s*|currently:\s*)?{_CELSIUS}", excerpt, re.I)
    high = re.search(rf"\bhigh\s*:?\s*{_CELSIUS}", details, re.I)
    low = re.search(rf"\blow\s*:?\s*{_CELSIUS}", details, re.I)
    source = f"\n\n[天气来源](<{row['url']}>)"
    metric = data.get("metric", "overview")
    if metric in _WEATHER_LABELS:
        if metric in {"low", "high", "current", "feels"}:
            measurement = {
                "low": low,
                "high": high,
                "current": current or re.search(rf"\bcurrently\s*:\s*{_CELSIUS}", details, re.I),
                "feels": re.search(rf"\bfeels like\s*:?\s*{_CELSIUS}", details, re.I),
            }[metric]
            value = f"约 {round(float(measurement[1]))}°C" if measurement else ""
        else:
            pattern = {
                "rain": r"(?:chance of rain|rain chance|precipitation probability)\s*:?\s*(\d{1,3}\s*%)",
                "humidity": r"\bhumidity\s*:?\s*(\d{1,3}\s*%)",
                "wind": r"\bwinds?\s*:?\s*(\d{1,3}(?:\.\d{1,2})?)\s*(km/h|mph)",
            }[metric]
            measurement = re.search(pattern, details, re.I)
            value = measurement[1].replace(" ", "") if measurement else ""
            if measurement and metric == "wind":
                speed = float(value) * (1.609344 if measurement[2].lower() == "mph" else 1)
                value = f"约 {round(speed)} 公里/小时"
        label = _WEATHER_LABELS[metric]
        if value:
            wording = value if value.startswith("约") else f"为 {value}"
            return f"{city}{day}{label}{wording}。{source}"
        return f"这份预报没有给出{city}{day}的{label}。{source}"
    facts = []
    if condition:
        facts.append(condition)
    if current:
        facts.append(f"气温约 {round(float(current[1]))}°C")
    if high and low:
        facts.append(f"预计 {round(float(low[1]))}–{round(float(high[1]))}°C")
    elif high:
        facts.append(f"最高约 {round(float(high[1]))}°C")
    elif low:
        facts.append(f"最低约 {round(float(low[1]))}°C")
    text = f"{city}{day}{'，'.join(facts)}。" if facts else f"{city}{day}的天气详情可以查看原预报。"
    return text + source


def _result_text(data, view, *, today=None):
    kind = data.get("kind")
    if kind == "knowledge_draft" and view == "knowledge_draft":
        return "已准备文字入库草稿，请核对卡片并点击提交；当前尚未入库。", data
    if kind == "public_web" and view == "public_web":
        if data.get("weather_date"):
            return _weather_text(data, today), data
        if not data["results"]:
            return "这次联网查询没有找到可核对的结果。", data
        heading = "以下是本次联网查询找到的公开来源。"
        entries = []
        for row in data["results"]:
            # Remote excerpts are quoted text, not HTML, links, or images.
            text = re.sub(r"([\\\[\]<>`*])", r"\\\1", row["title"] + "  \n" + row["excerpt"])
            entries.append(f"{text}  \n[查看来源](<{row['url']}>)")
        return "\n\n".join([heading, *entries]), data
    if kind in ("import_preview", "import_receipt") and view == "invoice_import_batch":
        return "导入批次实际状态及回执见卡片。", data
    if kind != view and view not in ("task", "preferences"):
        missing("结果视图与实际工具结果不一致。")
    company = data.get("company_id", "")
    label = {"C001": "甲企业", "C002": "乙企业"}.get(company, company)
    period = data.get("period") or ""
    if view == "monthly_summary":
        text = f"{label}{period}：发票总额{data['invoice_total']:.2f}元，银行收入{data['bank_income']:.2f}元，银行支出{data['bank_expense']:.2f}元。"
        if not data["has_records"]:
            text += "本次成功查询，没有记录。"
        return text + f"依据{_rules(data)}。", data
    if view == "purpose_check":
        return (
            f"{label}{period}已检查：必填{data['required_count']}张，缺少用途{data['missing_count']}张，明细见列表。依据{_rules(data)}。",
            data,
        )
    if view == "invoices":
        blank = sum(not (r.get("purpose") or "").strip() for r in data["rows"])
        return (
            f"{label}{period}共找到{data['total']}张发票；本页展示{len(data['rows'])}张，其中{blank}张用途为空。",
            data,
        )
    if view == "rules":
        return f"当前适用规则：{_rules(data)}。完整依据可在规则详情核对。", data
    if view == "task":
        if "task" not in data:
            labels = {
                "cancelled": "已取消",
                "deleted": "已删除",
                "active": "进行中",
                "waiting_user": "等待补充",
                "completed": "已完成",
            }
            if data.get("task_id") and "memory_epoch" in data and data.get("status") in labels:
                return (
                    f"任务操作回执：{label} {data.get('period') or '月份待补充'}，状态{labels[data['status']]}。",
                    None,
                )
            missing("缺少真实任务结果。")
        task = data["task"]
        status = (
            {"active": "进行中", "waiting_user": "等待补充"}.get(task["status"], task["status"])
            if task
            else ""
        )
        return (
            f"当前任务：{ {'C001': '甲企业', 'C002': '乙企业'}.get(task['company_id'], task['company_id']) } {task.get('period') or '月份待补充'}，状态{status}。"
            if task
            else "该企业当前没有未完成任务。"
        ), None
    if view == "preferences":
        if "preferences" not in data:
            missing("缺少真实偏好列表。")
        prefs = data["preferences"]
        return (
            "当前长期偏好：" + "；".join(str(p["value"]) for p in prefs) + "。"
            if prefs
            else "当前没有保存的长期偏好。"
        ), None
    missing("不支持该结果视图。")


def operation_receipt(conn, auth, row, *, historical=False):
    """Render a DB-owned operation using current values, never upgrade pending verification."""
    assert_access(conn, auth, row["company_id"])
    current = conn.execute(
        "SELECT * FROM invoices WHERE company_id=%s AND invoice_id=%s",
        (row["company_id"], row["invoice_id"]),
    ).fetchone()
    verified = bool(current) and all(current[k] == v for k, v in row["patch"].items())
    committed = row["state"] in ("committed", "no_change")
    verification = (
        row["verification_state"] if verified or not committed else "changed_since_commit"
    )
    from app.mutations import operation_result

    card = safe_json(operation_result({**row, "verification_state": verification}, current=current))
    label = (
        "本项未保存"
        if row["state"] == "rejected"
        else "保存状态尚未确定，请按原操作编号核验"
        if not committed
        else "当前已是该值，已核验"
        if row["state"] == "no_change" and verification == "verified"
        else {
            "verified": "已保存并核验",
            "pending": "已提交，核验待完成",
            "changed_since_commit": "本次曾提交，当前值已有变化",
        }[verification]
    )
    return (
        f"{'原操作核验：' if historical else ''}{ {'C001': '甲企业', 'C002': '乙企业'}.get(row['company_id'], row['company_id']) }发票{row['invoice_id']}：{label}。",
        card,
    )


def _validate_delivery(conn, execution, plan, reply, outputs):
    """A true selected result is insufficient if other executed work is hidden.

    The durable execution ledger outlives dropped context groups. It records
    performed work, not a claim that every natural-language intent was found.
    """
    delivered = {(c["kind"], c.get("company_id"), c.get("period")) for c in reply.cards}
    missing_results = set()
    # Prepared drafts are real performed work: an acknowledgement cannot hide
    # the user's only entry point for manually publishing the selected text.
    delivered_drafts = {
        c.get("draft_request_id") for c in reply.cards if c["kind"] == "knowledge_draft"
    }
    for row in conn.execute(
        "SELECT s.content FROM tool_executions e JOIN runs r USING(run_id) "
        "JOIN sources s ON s.source_id=e.result_source_id "
        "WHERE r.turn_id=%s AND e.name='prepare_knowledge_text' AND e.state='done'",
        (execution.turn_id,),
    ).fetchall():
        output = json.loads(row["content"])
        draft = output.get("data") or {}
        if output.get("ok") and draft.get("draft_request_id") not in delivered_drafts:
            missing_results.add(("knowledge_draft", draft["company_id"], None))
    for row in conn.execute(
        "SELECT e.name,e.arguments FROM tool_executions e JOIN runs r USING(run_id) "
        "WHERE r.turn_id=%s AND e.state='done' AND e.name IN ('query_month','check_purposes')",
        (execution.turn_id,),
    ).fetchall():
        kind = {"query_month": "monthly_summary", "check_purposes": "purpose_check"}[row["name"]]
        identity = (kind, row["arguments"]["company_id"], row["arguments"]["period"])
        if identity not in delivered:
            missing_results.add(identity)
    delivered_operations = {str(c["operation_id"]) for c in reply.cards if c["kind"] == "operation"}
    missing_operations = [
        str(r["operation_id"])
        for r in conn.execute(
            "SELECT operation_id FROM operations WHERE user_id=%s AND source_id=%s AND state IN ('committed','no_change') ORDER BY committed_at",
            (execution.auth.user_id, execution.source_id),
        ).fetchall()
        if str(r["operation_id"]) not in delivered_operations
    ]
    delivered_preferences = {
        str(outputs[str(b.result_ref)]["data"]["preference_id"])
        for b in plan.blocks
        if b.type == "preference_receipt"
    }
    missing_preferences = [
        str(r["result_ref"]["preference_id"])
        for r in conn.execute(
            "SELECT result_ref FROM memory_change_ops WHERE user_id=%s AND target_type='preference' AND state='committed' AND request_id LIKE %s",
            (execution.auth.user_id, f"tool:{execution.source_id}:%"),
        ).fetchall()
        if str(r["result_ref"]["preference_id"]) not in delivered_preferences
    ]
    from app.history_retrieval import continuation_in

    continuation = continuation_in(conn, execution)
    if continuation and not any(c["kind"] == "history_page" for c in reply.cards):
        missing_results.add(("history_page", continuation.get("company_id"), None))
    if missing_results or missing_operations or missing_preferences:
        raise AppError(
            "REPLY_EVIDENCE_MISSING",
            "最终答复遗漏本轮已执行结果、已提交回执或请求的续页。未交付成功，请补齐真实依据；已提交数据不得重复修改。",
            422,
            details={
                "results": [
                    {"view": kind, "company_id": company, "period": period}
                    for kind, company, period in sorted(missing_results)
                ],
                "operation_ids": missing_operations,
                "preference_ids": missing_preferences,
                **({"history_arguments": continuation} if continuation else {}),
                "instruction": "选择本轮已入模的完整结果及回执；knowledge_draft必须用result块和实际result_ref展示草稿，不能仅回复未修改。旧结果不在当前输入时先重新读取。不要通过重写措辞或无关说明绕过缺项。",
            },
        )


def _uncovered_public_queries(outputs, selected_refs):
    """Require each distinct actual public query, not every duplicate execution."""

    def query_key(output):
        data = output["data"]
        return canonical({key: value for key, value in data.items() if key != "results"})

    public = {
        ref: output
        for ref, output in outputs.items()
        if output.get("ok") and (output.get("data") or {}).get("kind") == "public_web"
    }
    required = {query_key(output) for output in public.values()}
    delivered = {query_key(public[ref]) for ref in selected_refs if ref in public}
    return required - delivered


def render(conn, execution, plan, payload, manifest, *, verified_knowledge=None):
    outputs = _results(conn, execution, payload)
    state = json.loads(payload["messages"][0]["content"].split("\n正式状态：\n", 1)[1])
    reply = RenderedReply("", plan.status, plan.clarification, plan.resume_turn_id)
    texts, rule_rows = [], []
    legacy_insufficient = any(
        (block.type == "explanation" and block.code == "knowledge_insufficient")
        or (block.type == "knowledge_answer" and block.coverage == "insufficient")
        for block in plan.blocks
    )
    delivered_web = {
        str(block.result_ref)
        for block in plan.blocks
        if block.type == "result" and block.view == "public_web"
    }
    if _uncovered_public_queries(outputs, delivered_web):
        missing("答复遗漏本轮公开查询结果，请用public_web视图及其真实编号交付。")
    for item in outputs.values():
        if item.get("ok"):
            d = item.get("data") or {}
            rule_rows.extend(d.get("rules", d.get("rule_refs", [])))
    claim_index = 0
    knowledge_reference_numbers = {}
    for block in plan.blocks:
        if block.type == "result":
            output = outputs.get(str(block.result_ref))
            if not output or not output.get("ok"):
                missing("只能选择本轮实际入模的成功工具结果。")
            data = output["data"]
            if block.view == "public_web" and data.get("clarification_turn_id"):
                origin = data["clarification_turn_id"]
                if reply.resume_turn_id and str(reply.resume_turn_id) != origin:
                    missing("续问编号不匹配已核验的天气地点。")
                reply.resume_turn_id = origin
            if block.view == "history_page":
                if data.get("kind") not in ("history_search", "conversation_search") or (
                    data.get("kind") == "history_search" and data.get("retrieval_mode") != "date"
                ):
                    missing(
                        "历史整页视图只能使用实际日期浏览结果。保留已经成功的检索，不要改查更大日期范围；按允许的说明码或已返回原文结束。",
                        details={
                            "result_ref": str(block.result_ref),
                            "allowed_answer_codes": data.get("allowed_answer_codes", []),
                            "allow_history_quotes": bool(data.get("matches")),
                            "retry_search_required": False,
                        },
                    )
                from app.tool_schemas import HistoryBlock

                for item in data["matches"]:
                    history_block = HistoryBlock(
                        type="history_quote",
                        source_id=item["source_id"],
                        citation_id=item["citation_id"],
                        quote=item["content"],
                    )
                    text, card = _history_quote(conn, execution, history_block, outputs)
                    texts.append(text)
                    reply.cards.append(card)
                texts.append(
                    "本轮历史检索覆盖不足，未命中不代表过去没有说过。"
                    if data["status"] == "partial"
                    else ""
                )
                texts.append(
                    "本次候选展示完毕，可调整描述继续查找。"
                    if data.get("retrieval_mode") == "ranked" and not data.get("next_cursor")
                    else "本页还有后续记录，可继续下一页。"
                    if data.get("has_more", bool(data.get("next_cursor")))
                    else "已到本次历史浏览末页。"
                    if data.get("coverage", {}).get("page_exhausted") or data["matches"]
                    else "本次历史查询未找到当前可读的原文。"
                )
                reply.cards.append(
                    {
                        "kind": "history_page",
                        "company_id": data.get("scope_snapshot", {}).get("company_id"),
                        "query_id": data["query_id"],
                    }
                )
                continue
            if block.view == "monthly_summary" and data.get("kind") != "monthly_summary":
                missing("结果视图与实际工具结果不一致。")
            # Stored JSON uses decimal strings. Conversion stays in the host.
            if block.view == "monthly_summary":
                from decimal import Decimal

                data = {
                    **data,
                    **{
                        k: Decimal(data[k])
                        for k in ("invoice_total", "bank_income", "bank_expense")
                    },
                }
            text, card = _result_text(
                data, block.view, today=state["request_frame"].get("current_date")
            )
            if block.view == "rules":
                for explanation in _purpose_explanations(conn, data.get("rules", [])).values():
                    text += "\n\n" + explanation
            texts.append(text)
            if card:
                reply.cards.append(safe_json(card))
        elif block.type == "operation":
            operation_id = str(block.operation_id)
            receipts = [
                o["data"]
                for o in outputs.values()
                if o.get("ok") and str((o.get("data") or {}).get("operation_id")) == operation_id
            ]
            evidence = bool(receipts)
            evidence = evidence or any(
                str(o["operation_id"]) == operation_id for o in state["known_operations"]
            )
            row = conn.execute(
                "SELECT * FROM operations WHERE operation_id=%s AND user_id=%s",
                (operation_id, execution.auth.user_id),
            ).fetchone()
            historical = row and str(row["source_id"]) != execution.source_id
            if (
                not evidence
                or not row
                or (historical and not any(r.get("status_query") for r in receipts))
            ):
                missing("本轮没有相符的真实操作记录，不能声称保存成功。")
            text, card = operation_receipt(conn, execution.auth, row, historical=historical)
            texts.append(text)
            reply.cards.append(card)
            for output in outputs.values():
                child = (output.get("data") or {}).get("post_save_check")
                if (
                    str((output.get("data") or {}).get("operation_id")) == operation_id
                    and child
                    and child.get("ok")
                ):
                    text, card = _result_text(child["data"], "purpose_check")
                    texts.append(text)
                    reply.cards.append(safe_json(card))
        elif block.type == "conversation_answer":
            texts.append(_conversation_answer(conn, execution, block.topic, state["request_frame"]))
        elif block.type == "chat_answer":
            text = block.text.strip()
            if any(
                item.get("ok")
                and (item.get("data") or {}).get("kind")
                in ("knowledge_search", "knowledge_evidence")
                for item in outputs.values()
            ):
                missing("知识检索后的事实回答须使用knowledge_answer并关联本轮证据。")
            if (
                any(ref in outputs and outputs[ref].get("ok") for ref in delivered_web)
                and re.search(r"天气|气温|温度|体感|降雨|湿度|风速|气象|最高|最低|阴天|晴天", text)
                and re.search(r"\d[\d,.]*\s*(?:°\s*[cf]|[℃℉]|摄氏度|华氏度|度|%)", text, re.I)
            ):
                raise AppError(
                    "REPLY_EVIDENCE_MISSING",
                    "天气数据由真实公开查询结果渲染，请勿在普通答复中重复未经核验的温度。",
                    422,
                    details={
                        "repair_kind": "public_web_chat_facts",
                        "instruction": "保留真实public_web结果块，删除chat_answer中的天气数值；"
                        "用户另外提出的普通问题可用独立chat_answer回答。",
                    },
                )
            if not text or re.search(
                r"\bC\d{3,}\b|\bF\d{3,}\b|\bR\d{3,}\b|[￥¥]|\d[\d,.]*\s*(?:元|万元|°C|摄氏度)|"
                r"已(?:经)?(?:保存|修改|核验|入库|提交|写入|更新|查询到)|保存成功|规则版本|发票总额",
                text,
                re.I,
            ):
                missing("普通答复只能用于交流，请用可核验的结果块表达具体事实或操作状态。")
            if (state["request_frame"].get("request_items") or outputs) and len(plan.blocks) == 1:
                missing("普通答复不能代替本轮业务、知识或联网结果。")
            texts.append(text)
        elif block.type == "knowledge_quote":
            for text, card in _knowledge_quotes(conn, execution, str(block.citation_id), outputs):
                if not legacy_insufficient:
                    texts.append(text)
                    reply.cards.append(card)
        elif block.type == "knowledge_answer":
            knowledge = [
                (item.get("data") or {})
                for item in outputs.values()
                if item.get("ok")
                and (item.get("data") or {}).get("kind")
                in ("knowledge_search", "knowledge_evidence")
            ]
            if not knowledge:
                missing("知识回答需要本轮实际检索结果。")
            approved = (verified_knowledge or {}).get("approved", set())
            accepted = 0
            for claim in block.claims:
                index = claim_index
                claim_index += 1
                # Revalidate at final commit, including evidence not approved by
                # the verifier, so fake or revoked citations cannot be laundered.
                cards = []
                for citation_id in claim.citation_ids:
                    cards.extend(
                        card
                        for _, card in _knowledge_quotes(conn, execution, str(citation_id), outputs)
                    )
                if index in approved:
                    labels = []
                    for card in cards:
                        citation = card["citation_id"]
                        if citation not in knowledge_reference_numbers:
                            knowledge_reference_numbers[citation] = (
                                len(knowledge_reference_numbers) + 1
                            )
                        number = knowledge_reference_numbers[citation]
                        labels.append(str(number))
                        reply.cards.append({**card, "reference_label": f"来源{number}"})
                    texts.append(f"{claim.text.strip()}（来源{'、'.join(dict.fromkeys(labels))}）")
                    accepted += 1
            if not accepted:
                if any(item.get("status") == "unavailable" for item in knowledge):
                    texts.append("知识资料暂时无法检索，请稍后再试。")
                elif any(item.get("status") == "partial" for item in knowledge):
                    texts.append(
                        "这次检索的资料尚不完整，暂时无法确认答案。请稍后重试或补充相关制度。"
                    )
                elif block.claims and (verified_knowledge or {}).get("status") != "checked":
                    texts.append("我已检索到资料，但暂时无法完成答案核验，请稍后重试。")
                else:
                    question = read_source(
                        conn, execution.auth, execution.source_id, allow_suppressed=True
                    )["content"].strip()
                    if 0 < len(question) <= 60 and "\n" not in question:
                        question = re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", question)
                        texts.append(
                            f"关于“{question}”，我这次没有找到足以确认答案的资料。你可以补充相关制度，或说明更具体的场景。"
                        )
                    else:
                        texts.append(
                            "这次找到的资料不足以可靠回答这个问题。你可以补充相关制度，或说明更具体的场景。"
                        )
            elif (
                block.coverage == "partial"
                or accepted < len(block.claims)
                or any(item.get("status") == "partial" for item in knowledge)
            ):
                texts.append(
                    "其他部分目前没有足够依据确认；你可以补充相关资料，或说明想了解的具体范围。"
                )
        elif block.type == "preference_receipt":
            output = outputs.get(str(block.result_ref)) or {}
            pref = output.get("data") or {}
            op = conn.execute(
                "SELECT result_ref FROM memory_change_ops WHERE user_id=%s AND target_type='preference' "
                "AND action=%s AND state='committed' AND request_id LIKE %s "
                "AND result_ref->>'preference_id'=%s LIMIT 1",
                (
                    execution.auth.user_id,
                    block.action,
                    f"tool:{execution.source_id}:%",
                    str(pref.get("preference_id")),
                ),
            ).fetchone()
            if not output.get("ok") or not op:
                missing("缺少本轮偏好操作提交证据。")
            reply.preference_action, reply.preference_status = block.action, "committed"
            texts.append(
                "已删除该长期偏好。"
                if block.action == "delete"
                else f"已{'记住' if block.action == 'remember' else '更正'}长期偏好：{op['result_ref']['value']}。"
            )
        elif block.type == "history_quote":
            text, card = _history_quote(conn, execution, block, outputs)
            texts.append(text)
            reply.cards.append(card)
        elif block.type == "confirmation":
            ref = str(block.plan_id)
            if not any(
                o.get("ok") and str((o.get("data") or {}).get("plan_id")) == ref
                for o in outputs.values()
            ):
                missing("修改计划未由本轮工具返回。")
            from app.invoice_write import _owned, public_plan

            row = _owned(conn, execution.auth, ref)
            if row["status"] != "needs_confirmation" or not row["unexpired"]:
                missing("该计划不是有效的待本人确认状态。")
            reply.cards.append(public_plan(row))
            texts.append("修改尚未保存。")
        else:
            texts.append(_explanation(conn, execution, block.code, outputs, rule_rows, state))
    if plan.clarification:
        reply.clarification = _clarification(conn, execution, plan.clarification, state, outputs)
        texts.append(reply.clarification.summary)
    paragraphs = [paragraph for text in texts for paragraph in text.split("\n\n") if paragraph]
    reply.message = "\n\n".join(dict.fromkeys(paragraphs))
    reply.cards = list(
        {
            (
                c["kind"],
                c.get("company_id"),
                c.get("period"),
                c.get("operation_id"),
                c.get("plan_id"),
                c.get("citation_id"),
                c.get("quote_start"),
                c.get("quote_end"),
                c.get("query_id"),
                c.get("query") if c["kind"] == "public_web" else None,
                c.get("metric") if c["kind"] == "public_web" else None,
            ): c
            for c in reply.cards
        }.values()
    )
    reply.displayed_view_ids = [c["view_id"] for c in reply.cards if c.get("view_id")]
    _validate_delivery(conn, execution, plan, reply, outputs)
    from app.request_checklist import bind_waiting_items, verify_items, waiting_items

    frame = state["request_frame"]
    items = list(frame.get("request_items", []))
    origin_id = frame.get("checklist_origin_turn_id")
    if origin_id and reply.resume_turn_id and str(reply.resume_turn_id) != origin_id:
        missing("续办编号与本轮补充范围关联的原请求不一致。")
    reply.resume_turn_id = origin_id or reply.resume_turn_id
    inherited = []
    if reply.resume_turn_id:
        old = conn.execute(
            "SELECT source_seq,status,final_response_ref,final_cards FROM turns "
            "WHERE turn_id=%s AND user_id=%s",
            (reply.resume_turn_id, execution.auth.user_id),
        ).fetchone()
        inherited = waiting_items(old) if old else []
        if inherited:
            if (
                old["source_seq"] >= execution.source_seq
                or old["status"] != "waiting_user"
                or not old["final_response_ref"]
            ):
                raise AppError("SOURCE_INVALIDATED", "原待补充事项不可在本轮续办。", 409)
            read_source(conn, execution.auth, str(old["final_response_ref"]))
            inherited = bind_waiting_items(inherited, frame["available_scope_refs"])
            if any(i["company_id"] is None or i["period"] is None for i in inherited):
                missing("原请求仍缺企业或月份。请先澄清并保留原请求。")
            items = list(
                {
                    (item["tool"], item["company_id"], item["period"]): item
                    for item in [*items, *inherited]
                }.values()
            )
    if items:
        failures = []
        for row in conn.execute(
            "SELECT e.name,e.arguments,e.result_source_id,s.content FROM tool_executions e JOIN runs r USING(run_id) "
            "JOIN sources s ON s.source_id=e.result_source_id WHERE r.turn_id=%s AND e.state IN ('done','failed')",
            (execution.turn_id,),
        ).fetchall():
            result = json.loads(row["content"])
            if (
                str(row["result_source_id"]) in outputs
                and not result.get("ok")
                and result.get("error", {}).get("code")
                in ("ACCESS_DENIED", "PERIOD_UNSUPPORTED", "RULE_UNAVAILABLE", "RULE_CONFLICT")
            ):
                failures.append({**row, "error": result["error"]})
        # A validated host permission refusal does not need a doomed tool call.
        # Check each scope independently: denial of one company cannot hide an
        # omitted query for a different, authorized company.
        if any(b.type == "explanation" and b.code == "access_denied" for b in plan.blocks):
            for item in items:
                if not item["company_id"]:
                    continue
                try:
                    assert_access(conn, execution.auth, item["company_id"])
                except AppError as exc:
                    if exc.code != "ACCESS_DENIED":
                        raise
                    failures.append(
                        {"name": item["tool"], "arguments": item, "error": exc.as_dict()}
                    )
        progress = verify_items(items, reply.cards, reply.clarification, failures)
        reply.cards.append({"kind": "request_progress", "company_id": "", "items": progress})
        verified = sum(i["status"] == "verified" for i in progress)
        blocked = sum(i["status"] == "blocked" for i in progress)
        waiting = sum(i["status"] == "waiting_user" for i in progress)
        summary = f"本轮事项：已核对{verified}项"
        if blocked:
            summary += f"，无法执行{blocked}项（原因见事项清单）"
        if waiting:
            summary += f"，待补充{waiting}项"
        reply.message += "\n\n" + summary + "。"
    return reply


def _history_quote(conn, execution, block, outputs):
    ref = str(block.source_id) if block.source_id else None
    served = []
    for output in outputs.values():
        data = output.get("data") or {}
        if output.get("ok") and data.get("kind") in ("history_search", "conversation_search"):
            served.extend(data.get("matches", []))
        elif output.get("ok") and data.get("kind") in ("history_evidence", "conversation_evidence"):
            served.append(data)
    original = next(
        (
            m
            for m in served
            if (ref is None or str(m["source_id"]) == ref)
            and (block.quote is None or block.quote in m["content"])
            and (not block.citation_id or str(block.citation_id) == m.get("citation_id"))
        ),
        None,
    )
    if not original:
        missing("历史摘录未真正进入本轮模型输入，不能引用原文其他部分。")
    ref = str(original["source_id"])
    quote = block.quote or original["content"]
    from app.history_retrieval import read_evidence_in

    evidence = read_evidence_in(conn, execution.auth, original["citation_id"])
    if (
        evidence["revision"] != original["revision"]
        or evidence["excerpt_hash"] != original["excerpt_hash"]
    ):
        missing("历史引文已更正或失效。")
    source = read_source(conn, execution.auth, ref)
    if quote not in source["content"]:
        missing("历史引用不匹配原文。")
    who = {"user": "用户", "assistant": "助手", "tool": "工具"}.get(source["role"], source["role"])
    from app.history_retrieval import CST

    text = f"历史{who}原文（{source['created_at'].astimezone(CST).date()}）见来源卡片。这只是当时记录，当前业务状态须重新查询。"
    card = {
        "kind": "history_quote",
        "session_id": evidence.get("session_id", execution.session_id),
        "company_id": evidence.get("company_id"),
        "citation_id": evidence["citation_id"],
        "source_id": ref,
        "quote_start": evidence["locator"]["char_start"] + evidence["excerpt"].index(quote),
        "quote_end": evidence["locator"]["char_start"]
        + evidence["excerpt"].index(quote)
        + len(quote),
    }
    return text, card


def _knowledge_quotes(conn, execution, citation_id, outputs):
    from app.knowledge_retrieval import read_evidence_group_in

    served = {}
    for output in outputs.values():
        data = output.get("data") or {}
        if not output.get("ok"):
            continue
        if data.get("kind") == "knowledge_search":
            served.update((e["citation_id"], e) for e in data.get("evidence", []))
        if data.get("kind") == "knowledge_evidence":
            served.update((e["citation_id"], e) for e in [data, *data.get("related_evidence", [])])
    if citation_id not in served:
        missing("只能引用本轮真正入模的知识证据。")
    group = read_evidence_group_in(conn, execution.auth, citation_id, lock=True)
    result = []
    for evidence in group:
        original = served.get(evidence["citation_id"])
        if not original:
            missing("必要条款尚未全部入模，请先用read_evidence重新读取该引用的完整条款组。")
        if (
            original["excerpt_hash"] != evidence["excerpt_hash"]
            or original["excerpt"] != evidence["excerpt"]
        ):
            missing("实际入模知识与当前引用不一致。")
        locators = []
        for locator in evidence["locator"]:
            kind = locator["type"]
            locators.append(
                f"第{locator['page']}页"
                if kind == "pdf"
                else f"第{locator['from']}—{locator['to']}行"
                if kind == "lines"
                else f"第{locator['paragraph']}段"
                if kind == "docx_paragraph"
                else f"表{locator['table']}第{locator['row']}行（含表头）"
            )
        label = "、".join(dict.fromkeys(locators))
        result.append(
            (
                f"本轮资料依据：《{evidence['title']}》第{evidence['edition']}版，{label}。原文见下方来源卡片；显示时会重新核对引用有效性。",
                {
                    "kind": "knowledge_quote",
                    "company_id": evidence["company_id"],
                    "citation_id": evidence["citation_id"],
                    "title": evidence["title"],
                    "edition": evidence["edition"],
                    "locator_label": label,
                },
            )
        )
    return result


def _explanation(conn, execution, code, outputs, rules, state):
    if code == "amount_materials":
        return "可按企业和月份查询三类金额资料：发票总额、银行收入和银行支出。"
    if code == "amount_material_unsupported":
        return (
            "当前系统未提供你问的这类金额资料。支持按企业和月份查询发票总额、银行收入和银行支出。"
        )
    if code == "conversation_boundary" and state.get("conversation_retrieval") == "disabled":
        return "普通聊天不会自动进入知识库，也不自动检索其他会话。请打开原会话继续，或手动提交相关资料到知识库；明确保存的任务和偏好独立保留。"
    errors = {o.get("error", {}).get("code") for o in outputs.values() if not o.get("ok")}
    knowledge = [
        o.get("data") or {}
        for o in outputs.values()
        if o.get("ok") and (o.get("data") or {}).get("kind") == "knowledge_search"
    ]
    history = [
        (o.get("data") or {})
        for o in outputs.values()
        if o.get("ok")
        and (o.get("data") or {}).get("kind") in ("history_search", "conversation_search")
    ]
    if code == "history_partial" and any(d["status"] == "partial" for d in history):
        return "历史索引、检索模式或摘录预算尚未完整覆盖本轮范围；未命中不能证明过去没有说过，请缩小条件或按日期查找。"
    if code == "history_not_found" and history and all(d["status"] == "partial" for d in history):
        missing(
            "本轮历史状态为partial，不能断言没有原话；请用history_partial或history_insufficient。"
        )
    if code == "history_insufficient" and history:
        return "本次找到的历史片段不足以支持回答，不能据此推断过去的说法或当前业务状态。"
    if code == "history_unavailable" and any(d["status"] == "unavailable" for d in history):
        return "历史检索依赖暂不可用，已保存原话和正式任务仍保留，请稍后重试或按日期查找。"
    if code == "knowledge_not_found" and any(d["status"] == "not_found" for d in knowledge):
        return "在本轮授权范围、有效版本和检索模式内未找到可引用的资料，不能据此编造依据。"
    if code == "knowledge_unavailable" and any(d["status"] == "unavailable" for d in knowledge):
        return "知识索引或检索依赖暂未就绪；这不代表没有资料，原文件和当前业务数据仍保留。"
    if code == "knowledge_partial" and any(d["status"] == "partial" for d in knowledge):
        return "本轮检索范围或证据覆盖不完整，请结合引用原文核对；需要时补充资料或缩小问题。"
    if code == "knowledge_insufficient" and knowledge:
        return "我这次没有找到能确认答案的资料。你可以补充相关制度，或说明更具体的范围。"
    if (
        code == "knowledge_compare"
        and len({e["document_id"] for d in knowledge for e in d["evidence"]}) > 1
    ):
        return "以下并列展示不同资料的原文，请核对适用范围和生效时间；资料内容不会自动改变当前业务规则。"
    if code in ("acknowledged", "no_changes"):
        if state["known_operations"]:
            missing(
                "此说明会声称整轮未修改，与已有保存记录矛盾。保留已有operation块，仅删除acknowledged/no_changes说明块；不需要重复查询或保存，也不要把回执改成task视图。",
                details={
                    "drop_explanation_codes": ["acknowledged", "no_changes"],
                    "keep_operation_blocks": True,
                    "retry_search_required": False,
                },
            )
        source = read_source(conn, execution.auth, execution.source_id, allow_suppressed=True)
        if code == "acknowledged":
            if not outputs and re.search(
                r"[？?]|有(?:什么|没有)|为什么|怎么(?:样|办)|推荐|什么是", source["content"]
            ):
                missing("本轮有明确问题。请实际回答，普通对话可使用chat_answer。")
            return "收到。"
        if not re.search(
            r"(?:没|未|是否|不要|别|没有).{0,8}(?:修改|保存|写入)|(?:修改|保存|写入).{0,5}(?:了吗|没有)",
            source["content"],
        ):
            missing("本轮没有要求说明修改状态。请回答用户实际提出的问题。")
        return "本轮未修改发票数据。"
    if code == "independent_totals":
        return "发票总额、银行收入和银行支出分别统计，不要求相等，也不据此推断付款或欠款。"
    if code == "unsupported_fields":
        source = read_source(conn, execution.auth, execution.source_id, allow_suppressed=True)
        content = source["content"]
        edit = r"修改|更改|改成|改为|改一下|更新|变更|改掉|把.{1,40}改|\b(?:change|edit|update)\b"
        field = r"发票|银行|字段|金额|抬头|税号|开票|流水|\b(?:invoice|bank|amount|field)\b"
        if not (re.search(edit, content, re.I) and re.search(field, content, re.I)):
            missing("本轮没有要求修改字段。请回答本轮问题，或如实说明当前能力限制。")
        return "本系统只允许按明确要求修改发票用途和备注，其他发票字段及银行记录不可修改。"
    if code == "public_web_scope_required" and "PUBLIC_WEB_SCOPE_REQUIRED" in errors:
        return "请明确要联网查的公开信息，或直接提供要查询的城市。"
    if code == "public_web_unavailable" and "PUBLIC_WEB_UNAVAILABLE" in errors:
        return "联网查询暂时不可用，请稍后再试。"
    if code == "public_weather_period" and "PUBLIC_WEB_WEATHER_PERIOD" in errors:
        return "我目前可以查今天、明天和后天的天气预报。"
    if code == "cannot_infer_payment":
        return "现有发票与银行资料没有逐票付款关联，不能判断哪些发票已付款或欠款。"
    if code == "read_only":
        company = state["selected_company_id"]
        grant = assert_access(conn, execution.auth, company) if company else None
        if not grant or grant["can_update"]:
            missing("当前权限不支持这个只读拒绝理由。")
        return "当前登录身份对该企业只有查看权限，本轮不能修改发票。"
    if code == "access_denied":
        from app.turns import mentioned_companies

        source = read_source(conn, execution.auth, execution.source_id, allow_suppressed=True)
        requested = mentioned_companies(conn, source["content"])
        denied = False
        for company in requested:
            try:
                assert_access(conn, execution.auth, company)
            except AppError:
                denied = True
        if not denied and "ACCESS_DENIED" not in errors:
            missing("没有相符的权限拒绝依据。")
        return "当前登录身份无权处理请求中的企业或操作，不能改查另一家企业代替。"
    if code == "unsupported_period" and "PERIOD_UNSUPPORTED" in errors:
        return "初赛用途检查只支持2026年9月；其他月份可以查询金额。"
    if code == "target_not_found" and any(
        not o.get("ok")
        and o.get("error", {}).get("code") == "RESOURCE_NOT_FOUND"
        and o.get("error", {}).get("details", {}).get("resource_type") == "invoice"
        for o in outputs.values()
    ):
        return "本次查询未找到对应发票，未执行修改，请核对企业和编号。"
    if code == "rule_activation_boundary":
        return "聊天不能启用一条新税法或改变当前业务规则。规则须经过受控的版本维护与生效操作；本轮未改变规则，也未据此跳过用途检查。"
    if code == "private_memory_boundary":
        return (
            "任务、偏好和历史只允许查看及管理当前登录用户本人的资料，不能提供其他用户的私人记忆。"
        )
    if code == "target_not_found" and any(
        o.get("ok") and (o.get("data") or {}).get("kind") == "invoices" and not o["data"]["rows"]
        for o in outputs.values()
    ):
        return "本次查询未找到对应发票，未执行修改，请核对企业和编号。"
    if code == "history_not_found" and any(d["status"] == "not_found" for d in history):
        return "本轮历史查找没有找到匹配原文，不能编造过去说过的内容。"
    if code == "no_tasks" and not state["task"] and not state["task_candidates"]:
        return "当前授权范围内没有可继续的未完成任务。"
    if code == "write_not_authorized" and (
        "WRITE_NOT_AUTHORIZED" in errors
        or any((o.get("data") or {}).get("status") == "prohibited" for o in outputs.values())
    ):
        return "这段内容不是本轮执行修改的授权，没有保存发票。"
    if code == "request_purpose_values" and any(
        (o.get("data") or {}).get("missing_count", 0) > 0 for o in outputs.values()
    ):
        return "如需补充，请按发票编号提供实际用途；不能根据票面项目推测。"
    if code in ("purpose_all", "purpose_threshold"):
        if text := _purpose_explanations(conn, rules).get(code):
            return text
    missing("说明缺少对应的真实状态依据，请选用实际结果或正确的拒绝理由。")


def _clarification(conn, execution, item, state, outputs):
    import re

    source = read_source(conn, execution.auth, execution.source_id, allow_suppressed=True)
    fields = set(item.missing_fields)
    if fields == {"location"} and item.intent == "other":
        city_required = any(
            (output.get("error") or {}).get("code") == "PUBLIC_WEB_CITY_REQUIRED"
            for output in outputs.values()
        )
        if not city_required:
            missing("没有需要补充城市的天气请求。")
        return item.model_copy(
            update={"company_id": None, "period": None, "summary": "你想查哪个城市的天气？"}
        )
    from app.turns import mentioned_companies

    requested = mentioned_companies(conn, source["content"])
    companies = requested or (
        [state["selected_company_id"]] if state["selected_company_id"] else []
    )
    company = item.company_id or (companies[0] if len(companies) == 1 else None)
    if company and companies and company not in companies:
        missing("待补充事项的企业与当前明确范围不一致。")
    period = item.period
    if period and company:
        from app.request_evidence import require_period

        try:
            require_period(conn, execution, company, period)
        except AppError as exc:
            if exc.code == "NEEDS_SCOPE_CLARIFICATION":
                missing("待补充事项的月份也必须有真实来源，不能由模型自填。")
            raise
    if not period and "period" not in fields:
        scopes = [
            s for s in state["request_frame"]["available_scope_refs"] if s["company_id"] == company
        ]
        if len(scopes) == 1:
            period = scopes[0]["period"]
    if "company_id" in fields and (
        state["selected_company_id"] or mentioned_companies(conn, source["content"])
    ):
        missing("企业已确定，不应重复追问。")
    if "period" in fields:
        if len(state["request_frame"]["available_scope_refs"]) == 1:
            missing("月份已明确，不应重复追问。")
        if re.search(r"F\d{3,}", source["content"]):
            missing("发票编号已明确，定位或权限拒绝不需要补月份。")
    if "write_confirmation" in fields and not any(
        (o.get("data") or {}).get("status") == "needs_confirmation" for o in outputs.values()
    ):
        missing("没有实际待确认的修改计划。")
    questions = {
        "company_id": "请说明要处理的企业。",
        "period": "请提供要查询的年月。",
        "invoice_id": "请提供明确发票编号。",
        "purpose": "请按发票编号提供实际用途。",
        "note": "请提供需要填写的备注。",
        "intent": "请说明本轮需要办理的事项。",
        "target": "你指的是哪一份资料或哪一条规则？",
        "location": "你想查哪个城市的天气？",
        "write_confirmation": "请核对修改卡片后本人确认。",
    }
    return item.model_copy(
        update={
            "company_id": company,
            "period": period,
            "summary": "".join(questions[k] for k in item.missing_fields),
        }
    )
