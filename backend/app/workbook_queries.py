"""Authority, execution and evidence for immutable workbook versions."""

import re
from datetime import date

from app.agent_authority import authorize_tool, restrict_knowledge
from app.errors import AppError
from app.execution import guard
from app.knowledge import document_in
from app.knowledge_parser import MEDIA
from app.sources import read_source
from app.turns import mentioned_companies
from app.values import jsonb, safe_json, uid
from app.xlsx_workbooks import calculate

PREVIEW_LIMIT = 12
MONTH = re.compile(r"(?<!\d)(?:(\d{4})\s*年\s*)?(1[0-2]|0?[1-9])\s*月(?!\s*\d)")
DATE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]")


def active_version(conn, auth, document_id, version_id, *, lock=False):
    document = document_in(conn, auth, document_id)
    row = conn.execute(
        "SELECT d.document_id,d.company_id,v.version_id,v.edition,v.parser_version,f.original_name "
        "FROM knowledge_documents d JOIN knowledge_versions v ON v.version_id=d.active_version_id "
        "JOIN knowledge_files f USING(file_id) JOIN workbook_snapshots w USING(version_id) "
        "WHERE d.document_id=%s AND d.status='active' AND v.state='ready' AND v.version_id=%s "
        "AND v.effective_from<=current_date AND (v.effective_to IS NULL OR v.effective_to>current_date)"
        + (" FOR SHARE OF d" if lock else ""),
        (document["document_id"], version_id),
    ).fetchone()
    if not row:
        raise AppError(
            "WORKBOOK_UNAVAILABLE", "表格尚未就绪、已换版或停用，请重新选择有效资料。", 409
        )
    return safe_json(row)


def validate_dependency(conn, auth, turn_id, dependency, *, lock=False):
    authorize_tool(conn, turn_id, dependency["tool"], {})
    restrict_knowledge(conn, turn_id, [str(dependency["document_id"])])
    return active_version(
        conn, auth, dependency["document_id"], dependency["version_id"], lock=lock
    )


def candidates(conn, execution, company_id, knowledge_attachments):
    if not company_id:
        return []
    if knowledge_attachments:
        return [
            {key: item[key] for key in ("document_id", "version_id", "original_name", "state")}
            for item in knowledge_attachments
            if item["media_type"] == MEDIA[".xlsx"]
        ]
    previous = conn.execute(
        "SELECT t.turn_id,t.status,t.knowledge_upload_ids,b.agent_id FROM turns t "
        "LEFT JOIN agent_turn_bindings b USING(turn_id) "
        "WHERE t.session_id=%s AND t.user_id=%s AND t.source_seq<%s AND t.request_kind<>'compact' "
        "ORDER BY t.source_seq DESC LIMIT 1",
        (execution.session_id, execution.auth.user_id, execution.source_seq),
    ).fetchone()
    if not previous:
        return []
    binding = conn.execute(
        "SELECT agent_id FROM agent_turn_bindings WHERE turn_id=%s", (execution.turn_id,)
    ).fetchone()
    if previous["agent_id"] != (binding or {}).get("agent_id"):
        return []
    if previous["status"] == "waiting_user" and previous["knowledge_upload_ids"]:
        from app.knowledge import upload_metadata

        try:
            item = upload_metadata(
                conn, execution.auth, previous["knowledge_upload_ids"][0], company_id
            )
            if item["media_type"] == MEDIA[".xlsx"] and item["state"] == "ready":
                validate_dependency(
                    conn,
                    execution.auth,
                    execution.turn_id,
                    {
                        **item,
                        "tool": "query_workbook",
                    },
                )
                return [{**item, "continuing_clarification": True}]
        except AppError:
            return []
    rows = conn.execute(
        "SELECT DISTINCT d.document_id,w.version_id,f.original_name,w.query "
        "FROM runs r JOIN workbook_results w USING(run_id) JOIN knowledge_versions v USING(version_id) "
        "JOIN knowledge_documents d USING(document_id) JOIN knowledge_files f USING(file_id) "
        "JOIN turns t USING(turn_id) "
        "WHERE r.turn_id=%s AND r.generation=t.active_run_generation AND t.status IN ('completed','resolved') "
        "AND d.company_id=%s AND t.final_cards @> jsonb_build_array(jsonb_build_object('result_id',w.result_id))",
        (previous["turn_id"], company_id),
    ).fetchall()
    available = []
    for row in rows:
        try:
            meta = validate_dependency(
                conn,
                execution.auth,
                execution.turn_id,
                {
                    **safe_json(row),
                    "tool": "query_workbook",
                },
            )
        except AppError:
            continue
        available.append(
            {
                **meta,
                "state": "ready",
                "previous_query": row["query"],
            }
        )
    return available


def validate_request(query, snapshot, text, previous):
    from app.request_evidence import explicit_periods
    from app.xlsx_workbooks import clean

    text = clean(text)
    sheet = next((s for s in snapshot["sheets"] if s["name"] == query["sheet"]), None)
    if sheet and not sheet["reason"]:
        usable = [s for s in snapshot["sheets"] if not s["reason"]]
        if len(usable) > 1 and not (
            query["sheet"] == previous.get("sheet") or query["sheet"] in text
        ):
            raise AppError("NEEDS_CLARIFICATION", "请确认要计算哪一个工作表。", 422)
        for field, candidates in sheet["columns"].items():
            column = query["columns"][field]
            header = candidates.get(column)
            if (
                len(candidates) > 1
                and header
                and not (
                    query["sheet"] == previous.get("sheet")
                    and column == previous.get("columns", {}).get(field)
                    or clean(header) in text
                    or re.search(rf"(?<![A-Za-z]){column}\s*列", text, re.I)
                )
            ):
                raise AppError("NEEDS_CLARIFICATION", "请确认使用哪一列作为计算口径。", 422)
        amount = sheet["columns"]["amount"].get(query["columns"]["amount"])
        if (
            amount
            and re.search(r"实付|实际支付", text)
            and clean(amount) not in {"实付金额", "实际支付金额"}
        ):
            raise AppError("NEEDS_CLARIFICATION", "当前金额列不能确认为实付，请确认计算口径。", 422)
    if query.get("all_dates"):
        if re.search(r"全部日期|不[限按]日期|所有日期|整[张个]表|全表|所有(?:明细|行|记录)", text):
            return
        if previous.get("all_dates") and not MONTH.search(text) and not DATE.search(text):
            return
        raise AppError("NEEDS_CLARIFICATION", "请确认日期范围，或明确要求计算全部日期。", 422)
    periods = explicit_periods(text) if not DATE.search(text) else set()
    months = list(MONTH.finditer(text))
    if (len(periods) == 1 or (not periods and len(months) == 1)) and not DATE.search(text):
        year, month = next(iter(periods)).split("-") if periods else months[0].groups()
        if not year:
            year = (previous.get("date_from") or "")[:4]
        if year:
            y, m = int(year), int(month)
            if not 1 <= y <= 9998:
                raise AppError("INVALID_ARGUMENT", "指定年份超出可计算范围。", 422)
            expected = date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1)
            if (query["date_from"], query["date_to_exclusive"]) == tuple(
                d.isoformat() for d in expected
            ):
                return
            raise AppError("WORKBOOK_SCOPE_MISMATCH", "工具日期范围与用户指定月份不符。", 422)
        raise AppError("NEEDS_CLARIFICATION", "请确认该月份的年份。", 422)
    if (
        len(periods) > 1
        or len(months) > 1
        or DATE.search(text)
        or re.search(r"本月|这个月|上个月|今年|去年", text)
    ):
        return
    if previous and all(
        query.get(k) == previous.get(k) for k in ("date_from", "date_to_exclusive")
    ):
        return
    raise AppError("NEEDS_CLARIFICATION", "请先提供日期范围。", 422)


def dispatch(db, execution, name, args):
    with db.transaction() as conn:
        guard(conn, execution, shared=True)
        company = conn.execute(
            "SELECT selected_company_id FROM sessions WHERE session_id=%s", (execution.session_id,)
        ).fetchone()["selected_company_id"]
        text = read_source(conn, execution.auth, execution.source_id)["content"]
        requested = mentioned_companies(conn, text)
        if requested and requested != [company]:
            raise AppError("TARGET_SCOPE_MISMATCH", "本轮指定的企业与当前表格企业不一致。", 422)
        from app.knowledge import upload_metadata

        turn = conn.execute(
            "SELECT knowledge_upload_ids FROM turns WHERE turn_id=%s", (execution.turn_id,)
        ).fetchone()
        attachments = [
            upload_metadata(conn, execution.auth, value, company)
            for value in turn["knowledge_upload_ids"]
        ]
        visible = candidates(conn, execution, company, attachments)
        match = [item for item in visible if item["document_id"] == args["document_id"]]
        if len(match) != 1:
            raise AppError(
                "NEEDS_CLARIFICATION", "请在当前企业选择并添加要查询的 Excel 资料。", 422
            )
        selected = match[0]
        version_id = args.get("version_id", selected["version_id"])
        if version_id != selected["version_id"]:
            raise AppError("WORKBOOK_UNAVAILABLE", "表格版本已变化，请重新选择有效资料。", 409)
        dependency = {"document_id": args["document_id"], "version_id": version_id, "tool": name}
        meta = validate_dependency(conn, execution.auth, execution.turn_id, dependency, lock=True)
        snapshot = conn.execute(
            "SELECT snapshot FROM workbook_snapshots WHERE version_id=%s", (version_id,)
        ).fetchone()["snapshot"]
        if name == "inspect_workbook":
            return {
                **meta,
                "kind": "workbook_structure",
                "workbook_dependency": dependency,
                "sheets": [
                    {key: value for key, value in sheet.items() if key != "rows"}
                    for sheet in snapshot["sheets"]
                ],
            }
        query = {
            key: value for key, value in args.items() if key not in ("document_id", "version_id")
        }
        if selected.get("continuing_clarification"):
            prior = conn.execute(
                "SELECT text FROM turns WHERE session_id=%s AND user_id=%s AND source_seq<%s "
                "AND request_kind<>'compact' ORDER BY source_seq DESC LIMIT 1",
                (execution.session_id, execution.auth.user_id, execution.source_seq),
            ).fetchone()
            text = f"{prior['text']}。{text}" if prior else text
        validate_request(query, snapshot, text, selected.get("previous_query", {}))
        result = calculate(
            snapshot,
            sheet=query["sheet"],
            columns=query["columns"],
            start=date.fromisoformat(query["date_from"]) if query.get("date_from") else None,
            end=date.fromisoformat(query["date_to_exclusive"])
            if query.get("date_to_exclusive")
            else None,
            category=query.get("category"),
            group_by=query.get("group_by", "none"),
        )
        result_id = uid()
        conn.execute(
            "INSERT INTO workbook_results(result_id,run_id,version_id,query,result) VALUES(%s,%s,%s,%s,%s)",
            (result_id, execution.run_id, version_id, jsonb(query), jsonb(result)),
        )
        return {
            **meta,
            "kind": "workbook_result",
            "workbook_dependency": dependency,
            "result_id": result_id,
            "query": query,
            "currency": "CNY",
            "total": result["total"],
            "count": result["count"],
            "groups": result["groups"][:PREVIEW_LIMIT],
            "group_count": len(result["groups"]),
            "row_numbers": [row[0] for row in result["rows"][:PREVIEW_LIMIT]],
            "headers": result["headers"],
        }


def read_result(conn, auth, result_id, *, run_id=None, offset=0, group_offset=0):
    row = conn.execute(
        "SELECT w.*,v.document_id,r.turn_id FROM workbook_results w JOIN runs r USING(run_id) "
        "JOIN turns t USING(turn_id) JOIN sessions s USING(session_id) JOIN knowledge_versions v USING(version_id) "
        "WHERE w.result_id=%s AND t.user_id=%s AND s.deleted_at IS NULL AND t.status<>'deleted'",
        (result_id, auth.user_id),
    ).fetchone()
    if not row or (run_id and str(row["run_id"]) != run_id):
        raise AppError("RESOURCE_NOT_FOUND", "表格结果不存在或不可访问。", 404)
    meta = validate_dependency(
        conn,
        auth,
        row["turn_id"],
        {
            "document_id": row["document_id"],
            "version_id": row["version_id"],
            "tool": "query_workbook",
        },
    )
    result = row["result"]
    return {
        **meta,
        "result_id": str(row["result_id"]),
        "query": row["query"],
        "currency": "CNY",
        "total": result["total"],
        "count": result["count"],
        "headers": result["headers"],
        "rows": result["rows"][offset : offset + 25],
        "groups": result["groups"][group_offset : group_offset + 25],
        "next_offset": offset + 25 if offset + 25 < result["count"] else None,
        "next_group_offset": group_offset + 25
        if group_offset + 25 < len(result["groups"])
        else None,
    }
