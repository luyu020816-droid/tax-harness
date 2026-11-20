"""Authority, execution and evidence for immutable workbook versions."""

import hashlib
from datetime import date

from app import business_dates
from app.agent_authority import authorize_tool, restrict_knowledge
from app.errors import AppError
from app.execution import guard
from app.knowledge import document_in
from app.knowledge_parser import MEDIA, parse_isolated
from app.knowledge_versions import selected_in, timing
from app.sources import read_source
from app.turns import mentioned_companies
from app.values import jsonb, safe_json, uid
from app.workbook_scope import validate_request
from app.xlsx_workbooks import PARSER_VERSION, calculate

PREVIEW_LIMIT = 12


def active_version(conn, auth, document_id, version_id=None, *, as_of=None, lock=False):
    document = document_in(conn, auth, document_id)
    if lock:
        conn.execute(
            "SELECT 1 FROM knowledge_documents WHERE document_id=%s FOR SHARE", (document_id,)
        )
    if document["status"] == "disabled":
        raise AppError("WORKBOOK_UNAVAILABLE", "表格已停用，请重新选择有效资料。", 409)
    day = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of or business_dates.business_today()
    selected = selected_in(conn, auth, document_id, day)
    if selected and version_id is not None and str(selected["version_id"]) != str(version_id):
        raise AppError("WORKBOOK_UNAVAILABLE", "表格已换版，请重新选择该时点的有效版本。", 409)
    if not selected:
        raise AppError("WORKBOOK_UNAVAILABLE", "该时点没有有效表格，请重新选择资料或日期。", 409)
    row = conn.execute(
        "SELECT v.document_id,v.company_id,v.version_id,v.edition,v.parser_version,f.original_name,f.file_id,f.sha256 "
        "FROM knowledge_versions v "
        "JOIN knowledge_files f USING(file_id) JOIN workbook_snapshots w USING(version_id) "
        "WHERE v.document_id=%s AND v.version_id=%s",
        (document["document_id"], selected["version_id"]),
    ).fetchone()
    if not row:
        raise AppError(
            "WORKBOOK_UNAVAILABLE", "表格尚未就绪或不在有效期内，请重新选择有效资料。", 409
        )
    return safe_json({**row, "version_as_of": day, "publication_revision": selected["publication_revision"]})


def validate_dependency(conn, auth, turn_id, dependency, *, lock=False):
    authorize_tool(conn, turn_id, dependency["tool"], {})
    restrict_knowledge(conn, turn_id, [str(dependency["document_id"])])
    return active_version(
        conn, auth, dependency["document_id"], dependency["version_id"],
        as_of=dependency.get("version_as_of"), lock=lock,
    )


def _recheck_required():
    return AppError(
        "WORKBOOK_RECHECK_REQUIRED",
        "表格结果需要重新核验，请选择有效的原始 Excel 并重新查询。",
        409,
    )


def snapshot_in(conn, version_id):
    """Load a snapshot after the caller has authorized this exact version."""
    snapshot = conn.execute(
        "SELECT snapshot FROM workbook_snapshots WHERE version_id=%s", (version_id,)
    ).fetchone()
    if snapshot and snapshot["snapshot"].get("parser_version") == PARSER_VERSION:
        return snapshot["snapshot"]
    original = conn.execute(
        "SELECT f.file_bytes,f.sha256 FROM knowledge_versions v JOIN knowledge_files f USING(file_id) "
        "WHERE v.version_id=%s",
        (version_id,),
    ).fetchone()
    if not original or not original["file_bytes"]:
        raise _recheck_required()
    content = bytes(original["file_bytes"])
    if hashlib.sha256(content).hexdigest() != original["sha256"]:
        raise _recheck_required()
    try:
        return parse_isolated(content, ".xlsx")
    except AppError as exc:
        raise _recheck_required() from exc


def previous_user_source(conn, execution, company_id):
    prior = conn.execute(
        "SELECT s.source_id FROM turns t JOIN sources s USING(turn_id) "
        "WHERE t.session_id=%s AND t.user_id=%s AND t.source_seq<%s "
        "AND t.request_kind<>'compact' AND s.role='user' AND s.source_seq=t.source_seq "
        "ORDER BY t.source_seq DESC LIMIT 1",
        (execution.session_id, execution.auth.user_id, execution.source_seq),
    ).fetchone()
    return read_source(conn, execution.auth, prior["source_id"], company_id=company_id) if prior else None


def candidates(conn, execution, company_id, knowledge_attachments, *, source):
    if not company_id:
        return [], False
    if knowledge_attachments:
        return [
            {
                key: item[key] for key in (
                    "document_id", "version_id", "original_name", "state", "version_as_of",
                    "version_basis", "version_fragments",
                ) if key in item
            }
            for item in knowledge_attachments
            if item["media_type"] == MEDIA[".xlsx"]
        ], False
    previous = conn.execute(
        "SELECT t.turn_id,t.status,t.knowledge_upload_ids,b.agent_id, "
        """t.final_cards @> '[{"kind":"workbook_result"}]'::jsonb AS has_workbook_result, """
        "EXISTS(SELECT 1 FROM knowledge_files f WHERE f.file_id=ANY(t.knowledge_upload_ids) "
        "AND f.media_type=%s) AS has_workbook_attachment FROM turns t "
        "LEFT JOIN agent_turn_bindings b USING(turn_id) "
        "WHERE t.session_id=%s AND t.user_id=%s AND t.source_seq<%s AND t.request_kind<>'compact' "
        "ORDER BY t.source_seq DESC LIMIT 1",
        (MEDIA[".xlsx"], execution.session_id, execution.auth.user_id, execution.source_seq),
    ).fetchone()
    if not previous:
        return [], False
    had_workbook = previous["has_workbook_result"] or (
        previous["status"] == "waiting_user" and previous["has_workbook_attachment"]
    )
    binding = conn.execute(
        "SELECT agent_id FROM agent_turn_bindings WHERE turn_id=%s", (execution.turn_id,)
    ).fetchone()
    if previous["agent_id"] != (binding or {}).get("agent_id"):
        return [], had_workbook
    if not had_workbook:
        return [], False
    _, requested_day, fragments = business_dates.workbook_reference(source["content"])
    default_day = business_dates.business_today(source["created_at"])
    if previous["status"] == "waiting_user" and previous["knowledge_upload_ids"]:
        from app.knowledge import upload_metadata

        try:
            prior = previous_user_source(conn, execution, company_id)
            item = upload_metadata(
                conn, execution.auth, previous["knowledge_upload_ids"][0], company_id,
                question=source["content"] if requested_day else (prior or {}).get("content", ""),
                requested_at=source["created_at"],
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
                return [{**item, "continuing_clarification": True}], False
        except AppError:
            return [], had_workbook

    if not previous["has_workbook_result"]:
        return [], had_workbook
    rows = conn.execute(
        "SELECT DISTINCT d.document_id,w.version_id,f.original_name,w.query "
        "FROM runs r JOIN workbook_results w USING(run_id) JOIN knowledge_versions v USING(version_id) "
        "JOIN knowledge_documents d USING(document_id) JOIN knowledge_files f USING(file_id) "
        "JOIN turns t USING(turn_id) "
        "WHERE r.turn_id=%s AND r.generation=t.active_run_generation AND t.status IN ('completed','resolved') "
        "AND d.company_id=%s AND w.result->>'parser_version'=%s "
        "AND t.final_cards @> jsonb_build_array(jsonb_build_object('result_id',w.result_id))",
        (previous["turn_id"], company_id, PARSER_VERSION),
    ).fetchall()
    available = []
    for row in rows:
        carried = row["query"].get("version_basis") == "explicit"
        reference_day = requested_day or (row["query"].get("version_as_of") if carried else None) or default_day
        try:
            meta = validate_dependency(
                conn,
                execution.auth,
                execution.turn_id,
                {
                    **safe_json(row),
                    "tool": "query_workbook",
                    "version_as_of": reference_day,
                    "version_id": None if requested_day else row["version_id"],
                },
            )
        except AppError:
            continue
        available.append(
            {
                **meta,
                "state": "ready",
                "previous_query": row["query"] if meta["version_id"] == str(row["version_id"]) else {},
                "version_basis": "explicit" if requested_day or carried else "business_default",
                "version_fragments": fragments if requested_day else row["query"].get("version_fragments", []),
            }
        )
    return available, not available


def dispatch(db, execution, name, args):
    with db.transaction() as conn:
        guard(conn, execution, shared=True)
        company = conn.execute(
            "SELECT selected_company_id FROM sessions WHERE session_id=%s", (execution.session_id,)
        ).fetchone()["selected_company_id"]
        source = read_source(conn, execution.auth, execution.source_id)
        text = source["content"]
        requested = mentioned_companies(conn, text)
        if requested and requested != [company]:
            raise AppError("TARGET_SCOPE_MISMATCH", "本轮指定的企业与当前表格企业不一致。", 422)
        from app.knowledge import upload_metadata

        turn = conn.execute(
            "SELECT knowledge_upload_ids FROM turns WHERE turn_id=%s", (execution.turn_id,)
        ).fetchone()
        attachments = [
            upload_metadata(
                conn, execution.auth, value, company, question=text, requested_at=source["created_at"],
            )
            for value in turn["knowledge_upload_ids"]
        ]
        visible, unavailable = candidates(conn, execution, company, attachments, source=source)
        match = [item for item in visible if item["document_id"] == args["document_id"]]
        if len(match) != 1:
            if unavailable:
                raise AppError(
                    "WORKBOOK_UNAVAILABLE", "上一轮表格当前不可用，请重新选择有效 Excel 资料。", 409
                )
            raise AppError(
                "NEEDS_CLARIFICATION", "请在当前企业选择并添加要查询的 Excel 资料。", 422
            )
        selected = match[0]
        version_id = args.get("version_id", selected["version_id"])
        if version_id != selected["version_id"]:
            raise AppError("WORKBOOK_UNAVAILABLE", "表格版本已变化，请重新选择有效资料。", 409)
        if args.get("version_as_of") and args["version_as_of"] != selected.get("version_as_of"):
            raise AppError("WORKBOOK_SCOPE_MISMATCH", "工具的工作簿版本日期与原话不符。", 422)
        dependency = {
            "document_id": args["document_id"], "version_id": version_id, "tool": name,
            "version_as_of": selected.get("version_as_of"),
        }
        meta = validate_dependency(conn, execution.auth, execution.turn_id, dependency, lock=True)
        snapshot = snapshot_in(conn, version_id)
        meta["parser_version"] = snapshot["parser_version"]
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
        earlier = None
        if selected.get("continuing_clarification"):
            earlier = previous_user_source(conn, execution, company)
            if earlier:
                earlier = {**earlier, "content": business_dates.workbook_reference(earlier["content"])[0]}
        query = validate_request(
            args,
            snapshot,
            business_dates.workbook_reference(text)[0],
            selected.get("previous_query", {}),
            created_at=source["created_at"],
            prior_source=earlier,
        )
        query.update(
            version_as_of=meta["version_as_of"],
            version_basis=selected.get("version_basis", "business_default"),
            version_fragments=selected.get("version_fragments", []),
        )
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
            "version_timing": timing(date.fromisoformat(meta["version_as_of"])),
            "currency": result["currency"],
            "total": result["total"],
            "count": result["count"],
            "groups": result["groups"][:PREVIEW_LIMIT],
            "group_count": len(result["groups"]),
            "row_numbers": [row[0] for row in result["rows"][:PREVIEW_LIMIT]],
            "headers": result["headers"],
        }


def validate_result(conn, auth, result_id, *, run_id=None, include_result=False):
    fields = ",w.result,w.query" if include_result else ""
    row = conn.execute(
        "SELECT w.run_id,w.version_id,v.document_id,r.turn_id,w.result->>'parser_version' AS parser_version,"
        "w.query->>'version_as_of' AS version_as_of"
        + fields
        + " FROM workbook_results w JOIN runs r USING(run_id) "
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
            "version_as_of": row["version_as_of"],
        },
    )
    if row["parser_version"] != PARSER_VERSION:
        raise _recheck_required()
    return row, meta


def read_result(conn, auth, result_id, *, run_id=None, offset=0, group_offset=0):
    row, meta = validate_result(conn, auth, result_id, run_id=run_id, include_result=True)
    result = row["result"]
    return {
        **meta,
        "result_id": str(result_id),
        "query": row["query"],
        "version_timing": timing(date.fromisoformat(meta["version_as_of"])),
        "parser_version": result["parser_version"],
        "currency": result["currency"],
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
