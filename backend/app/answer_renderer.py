"""Render program facts and receipts; models select evidence, never invent facts."""

import json
from dataclasses import dataclass, field

from app.errors import AppError
from app.identity import assert_access
from app.sources import read_source
from app.values import safe_json


def missing(message):
    raise AppError("REPLY_EVIDENCE_MISSING", message, 422)


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
            if (
                str(source["turn_id"]) != execution.turn_id
                or source["role"] != "tool"
                or json.loads(source["content"]) != item
            ):
                missing("工具结果与真实来源不一致。")
            results[ref] = item
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


def _result_text(data, view):
    kind = data.get("kind")
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
                    f"任务操作回执：{data['company_id']} {data.get('period') or '月份待补充'}，状态{labels[data['status']]}。",
                    None,
                )
            missing("缺少真实任务结果。")
        task = data["task"]
        return (
            f"当前任务：{task['company_id']} {task.get('period') or '月份待补充'}，状态{task['status']}。"
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


def render(conn, execution, plan, payload, manifest):
    outputs = _results(conn, execution, payload)
    state = json.loads(payload["messages"][0]["content"].split("\n正式状态：\n", 1)[1])
    reply = RenderedReply("", plan.status, plan.clarification, plan.resume_turn_id)
    texts, rule_rows = [], []
    for item in outputs.values():
        if item.get("ok"):
            d = item.get("data") or {}
            rule_rows.extend(d.get("rules", d.get("rule_refs", [])))
    for block in plan.blocks:
        if block.type == "result":
            output = outputs.get(str(block.result_ref))
            if not output or not output.get("ok"):
                missing("只能选择本轮实际入模的成功工具结果。")
            data = output["data"]
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
            text, card = _result_text(data, block.view)
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
            assert_access(conn, execution.auth, row["company_id"])
            current = conn.execute(
                "SELECT * FROM invoices WHERE company_id=%s AND invoice_id=%s",
                (row["company_id"], row["invoice_id"]),
            ).fetchone()
            verified = bool(current) and all(current[k] == v for k, v in row["patch"].items())
            # Preserve a failed read-back as pending unless a real tool verified it.
            committed = row["state"] in ("committed", "no_change")
            verification = (
                row["verification_state"] if verified or not committed else "changed_since_commit"
            )
            from app.mutations import operation_result

            card = safe_json(
                operation_result({**row, "verification_state": verification}, current=current)
            )
            reply.cards.append(card)
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
            texts.append(
                f"{'原操作核验：' if historical else ''}{row['company_id']}发票{row['invoice_id']}：{label}。"
            )
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
        elif block.type == "knowledge_quote":
            for text, card in _knowledge_quotes(conn, execution, str(block.citation_id), outputs):
                texts.append(text)
                reply.cards.append(card)
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
            ref = str(block.source_id)
            found = any(
                any(str(m["source_id"]) == ref for m in (o.get("data") or {}).get("matches", []))
                for o in outputs.values()
                if o.get("ok")
            )
            if not found:
                missing("历史原文未由本轮检索返回。")
            source = read_source(conn, execution.auth, ref)
            if block.quote not in source["content"]:
                missing("历史引用不匹配原文。")
            who = {"user": "用户", "assistant": "助手", "tool": "工具"}.get(
                source["role"], source["role"]
            )
            quoted = "\n".join("> " + line for line in block.quote.splitlines())
            texts.append(f"历史{who}原文（{source['created_at'].date()}）：\n{quoted}")
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
            ): c
            for c in reply.cards
        }.values()
    )
    reply.displayed_view_ids = [c["view_id"] for c in reply.cards if c.get("view_id")]
    return reply


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
    errors = {o.get("error", {}).get("code") for o in outputs.values() if not o.get("ok")}
    knowledge = [
        o.get("data") or {}
        for o in outputs.values()
        if o.get("ok") and (o.get("data") or {}).get("kind") == "knowledge_search"
    ]
    if code == "knowledge_not_found" and any(d["status"] == "not_found" for d in knowledge):
        return "在本轮授权范围、有效版本和检索模式内未找到可引用的资料，不能据此编造依据。"
    if code == "knowledge_unavailable" and any(d["status"] == "unavailable" for d in knowledge):
        return "知识索引或检索依赖暂未就绪；这不代表没有资料，原文件和当前业务数据仍保留。"
    if code == "knowledge_partial" and any(d["status"] == "partial" for d in knowledge):
        return "本轮检索范围或证据覆盖不完整，请结合引用原文核对；需要时补充资料或缩小问题。"
    if code == "knowledge_insufficient" and knowledge:
        return "本次检索结果不足以支持回答这个问题，请补充对应条款或缩小问题范围。候选命中不代表问题已有依据。"
    if (
        code == "knowledge_compare"
        and len({e["document_id"] for d in knowledge for e in d["evidence"]}) > 1
    ):
        return "以下并列展示不同资料的原文，请核对适用范围和生效时间；资料内容不会自动改变当前业务规则。"
    if code in ("acknowledged", "no_changes"):
        if state["known_operations"]:
            missing("已有操作，请使用实际操作回执。")
        return "本轮未修改发票数据。"
    if code == "independent_totals":
        return "发票总额、银行收入和银行支出分别统计，不要求相等，也不据此推断付款或欠款。"
    if code == "unsupported_fields":
        return "本系统只允许按明确要求修改发票用途和备注，其他发票字段及银行记录不可修改。"
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
    if code == "history_not_found" and any(
        o.get("ok") and (o.get("data") or {}).get("not_found") for o in outputs.values()
    ):
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
        "write_confirmation": "请核对修改卡片后本人确认。",
    }
    return item.model_copy(
        update={
            "company_id": company,
            "period": period,
            "summary": "".join(questions[k] for k in item.missing_fields),
        }
    )
