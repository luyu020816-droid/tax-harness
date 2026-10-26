import re

import psycopg

from app.errors import AppError
from app.execution import Execution, guard, guard_company
from app.identity import Identity, assert_access, owned
from app.sources import read_source, validate_refs
from app.tasks import _revision, current_in
from app.values import digest, jsonb, uid


def _validate_patch(patch: dict) -> None:
    if not patch or not set(patch) <= {"purpose", "note"}:
        raise AppError("INVALID_ARGUMENT", "只能修改发票用途和备注。")
    if any(v is not None and (not isinstance(v, str) or len(v) > 2000) for v in patch.values()):
        raise AppError("INVALID_ARGUMENT", "用途和备注须为文本或空值，每项最多 2000 字符。")


def _validate_target_scope(conn, auth, source, company_id, execution):
    companies = conn.execute("SELECT company_id,aliases FROM companies").fetchall()
    mentioned = {
        c["company_id"]
        for c in companies
        if any(alias and alias in source["content"] for alias in c["aliases"])
    }
    if mentioned and company_id not in mentioned:
        raise AppError("NEEDS_CLARIFICATION", "修改企业与用户明确指令不一致。", 422)
    if not mentioned and execution:
        session = owned(conn, "sessions", "session_id", execution.session_id, auth)
        selected = session["selected_company_id"]
        if selected and selected != company_id:
            raise AppError("NEEDS_CLARIFICATION", "当前选择的企业与目标不一致，请明确企业。", 422)
        if not selected and not current_in(conn, auth, company_id):
            raise AppError("NEEDS_CLARIFICATION", "需要明确修改所属企业。", 422)


def _validate_selection(conn, auth, company_id, invoice_id, selection, execution):
    if not selection:
        return
    view = owned(conn, "result_views", "view_id", selection["view_id"], auth)
    latest = conn.execute(
        "SELECT v.view_id FROM result_views v JOIN sources s ON s.source_id=v.result_source_id WHERE v.user_id=%s AND v.session_id=%s AND v.company_id=%s AND v.status='active' AND v.displayed_event_seq IS NOT NULL ORDER BY s.source_seq DESC,s.created_at DESC LIMIT 1",
        (auth.user_id, view["session_id"], company_id),
    ).fetchone()
    head = conn.execute("SELECT * FROM company_heads WHERE company_id=%s", (company_id,)).fetchone()
    if (
        view["status"] != "active"
        or not latest
        or str(latest["view_id"]) != str(view["view_id"])
        or view["displayed_event_seq"] is None
        or view["company_id"] != company_id
        or (execution and str(view["session_id"]) != execution.session_id)
        or any(view[k] != head[k] for k in ("data_revision", "rules_epoch"))
    ):
        raise AppError("STALE_CONTEXT", "列表已失效或尚未展示，请明确发票编号。", 409)
    index = selection.get("row_index", 0) - 1
    if (
        index < 0
        or index >= len(view["ordered_keys"])
        or view["ordered_keys"][index]["invoice_id"] != invoice_id
    ):
        raise AppError("NEEDS_CLARIFICATION", "列表序号与发票目标不一致。", 422)


def _existing(conn, auth, intent_key, fingerprint, operation_id):
    by_id = conn.execute(
        "SELECT * FROM operations WHERE operation_id=%s", (operation_id,)
    ).fetchone()
    if by_id and (by_id["user_id"] != auth.user_id or by_id["intent_key"] != intent_key):
        raise AppError("IDEMPOTENCY_CONFLICT", "操作编号与原请求不一致。", 409)
    row = conn.execute("SELECT * FROM operations WHERE intent_key=%s", (intent_key,)).fetchone()
    if row and row["request_hash"] != fingerprint:
        raise AppError(
            "INTENT_CONFLICT", "同一条指令对这张发票出现了不同修改内容，请明确更正。", 409
        )
    return row


def operation_result(
    row: dict, *, replayed=False, current=None, head=None, memory_epoch=None
) -> dict:
    result = {
        "kind": "operation",
        "operation_id": row["operation_id"],
        "company_id": row["company_id"],
        "invoice_id": row["invoice_id"],
        "commit_state": row["state"],
        "verification_state": row["verification_state"],
        "before_values": row["before_values"],
        "saved_values": row["after_values"],
        "patch": row["patch"],
        "before_revision": row["before_revision"],
        "after_revision": row["after_revision"],
        "committed_at": row["committed_at"],
        "replayed": replayed,
    }
    if current:
        result["current_values"] = {k: current[k] for k in row["patch"]}
    if head:
        result.update(head)
    if memory_epoch is not None:
        result["memory_epoch"] = memory_epoch
    if row["last_error"]:
        result["error"] = row["last_error"]
    return result


def verify_operation(
    db, auth: Identity, operation_id: str, *, replayed=False, memory_epoch=None, known_row=None
) -> dict:
    row = known_row
    try:
        with db.transaction(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE operation_id=%s AND user_id=%s",
                (operation_id, auth.user_id),
            ).fetchone()
            if not row:
                raise AppError("RESOURCE_NOT_FOUND", "操作不存在或不可访问。", 404)
            assert_access(conn, auth, row["company_id"])
            if row["state"] not in ("committed", "no_change"):
                return operation_result(row, replayed=replayed)
            current = conn.execute(
                "SELECT * FROM invoices WHERE company_id=%s AND invoice_id=%s",
                (row["company_id"], row["invoice_id"]),
            ).fetchone()
            head = conn.execute(
                "SELECT *,transaction_timestamp() AS as_of FROM company_heads WHERE company_id=%s",
                (row["company_id"],),
            ).fetchone()
            verified = bool(
                current and all(current[k] == value for k, value in row["patch"].items())
            )
        state = "verified" if verified else "changed_since_commit"
        with db.transaction() as conn:
            row = conn.execute(
                "UPDATE operations SET verification_state=%s WHERE operation_id=%s AND user_id=%s RETURNING *",
                (state, operation_id, auth.user_id),
            ).fetchone()
        return operation_result(
            row, replayed=replayed, current=current, head=head, memory_epoch=memory_epoch
        )
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        # Commit truth remains in the operation ledger even if this read-back cannot complete.
        if row and row["state"] in ("committed", "no_change"):
            return operation_result(
                {**row, "verification_state": "pending"},
                replayed=replayed,
                memory_epoch=memory_epoch,
            )
        raise AppError(
            "VERIFICATION_PENDING",
            "当前无法读取操作结果，请按原编号稍后核对。",
            503,
            True,
            {"operation_id": operation_id},
        ) from exc


def update_invoice(
    db,
    auth: Identity,
    *,
    company_id: str,
    invoice_id: str,
    patch: dict,
    expected_revision: int,
    source_id: str,
    source_refs: list[dict],
    operation_id=None,
    selection_ref=None,
    execution: Execution | None = None,
) -> dict:
    _validate_patch(patch)
    operation_id = operation_id or uid()
    intent_key = digest([auth.user_id, source_id, company_id, invoice_id])
    fingerprint = digest([auth.user_id, source_id, company_id, invoice_id, patch])
    terminal = False
    with db.transaction() as conn:
        assert_access(conn, auth, company_id, write=True)
        row = _existing(conn, auth, intent_key, fingerprint, operation_id)
        if row and row["state"] in ("committed", "no_change", "rejected"):
            terminal = True
        if not row:
            source = read_source(conn, auth, source_id)
            refs = validate_refs(conn, auth, source_refs)
            if source_id not in [str(r["source_id"]) for r in refs]:
                raise AppError("NEEDS_CLARIFICATION", "需要本轮明确修改指令。", 422)
            if any(value and value not in source["content"] for value in patch.values()):
                raise AppError(
                    "NEEDS_CLARIFICATION", "用途或备注内容必须有用户原话支持，不能自动猜测。", 422
                )
            _validate_target_scope(conn, auth, source, company_id, execution)
            mentioned_ids = set(
                re.findall(r"(?<![A-Za-z0-9])F[0-9]{3}(?![0-9])", source["content"])
            )
            if mentioned_ids and invoice_id not in mentioned_ids:
                raise AppError("NEEDS_CLARIFICATION", "修改票号与本轮用户明确指令不一致。", 422)
            if (
                not mentioned_ids
                and not selection_ref
                and re.search(
                    r"第[一二三四五六七八九十0-9]+[张条个]|[这那首末]张|最后一张", source["content"]
                )
            ):
                raise AppError(
                    "NEEDS_CLARIFICATION", "按序号或这张指代时，需要实际展示的有效列表。", 422
                )
            if not conn.execute(
                "SELECT 1 FROM invoices WHERE company_id=%s AND invoice_id=%s",
                (company_id, invoice_id),
            ).fetchone():
                raise AppError("RESOURCE_NOT_FOUND", "该企业不存在这张发票。", 404)
            conn.execute(
                "INSERT INTO operations(operation_id,user_id,company_id,invoice_id,source_id,intent_key,request_hash,before_revision,patch,state) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'prepared') ON CONFLICT DO NOTHING",
                (
                    operation_id,
                    auth.user_id,
                    company_id,
                    invoice_id,
                    source_id,
                    intent_key,
                    fingerprint,
                    expected_revision,
                    jsonb(patch),
                ),
            )
            row = _existing(conn, auth, intent_key, fingerprint, operation_id)
            if not row:
                raise AppError("IDEMPOTENCY_CONFLICT", "操作编号与已有请求冲突。", 409)
        operation_id = str(row["operation_id"])
    if terminal:
        return verify_operation(db, auth, operation_id, replayed=True, known_row=row)
    epoch = None
    try:
        with db.transaction() as conn:
            if execution:
                user = guard(conn, execution)
            else:
                user = conn.execute(
                    "SELECT * FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,)
                ).fetchone()
            assert_access(conn, auth, company_id, write=True)
            guard_company(conn, execution, company_id)
            invoice = conn.execute(
                "SELECT * FROM invoices WHERE company_id=%s AND invoice_id=%s FOR UPDATE",
                (company_id, invoice_id),
            ).fetchone()
            row = conn.execute(
                "SELECT * FROM operations WHERE operation_id=%s FOR UPDATE", (operation_id,)
            ).fetchone()
            if row["state"] in ("committed", "no_change", "rejected"):
                terminal = True
            else:
                source = read_source(conn, auth, source_id)
                validate_refs(conn, auth, source_refs)
                _validate_target_scope(conn, auth, source, company_id, execution)
                _validate_selection(conn, auth, company_id, invoice_id, selection_ref, execution)
                if invoice["revision"] != row["before_revision"]:
                    raise AppError(
                        "VERSION_CONFLICT", "发票已被更新，请查看当前值后明确更正。", 409
                    )
                changed = any(invoice[k] != value for k, value in patch.items())
                if changed:
                    from psycopg import sql

                    assignments = sql.SQL(",").join(
                        sql.SQL("{}=%s").format(sql.Identifier(k)) for k in patch
                    )
                    invoice_after = conn.execute(
                        sql.SQL(
                            "UPDATE invoices SET {},revision=revision+1 WHERE company_id=%s AND invoice_id=%s RETURNING *"
                        ).format(assignments),
                        [*patch.values(), company_id, invoice_id],
                    ).fetchone()
                    conn.execute(
                        "UPDATE company_heads SET data_revision=data_revision+1 WHERE company_id=%s",
                        (company_id,),
                    )
                else:
                    invoice_after = invoice
                row = conn.execute(
                    "UPDATE operations SET state=%s,before_values=%s,after_values=%s,after_revision=%s,committed_at=now() WHERE operation_id=%s RETURNING *",
                    (
                        "committed" if changed else "no_change",
                        jsonb({k: invoice[k] for k in patch}),
                        jsonb({k: invoice_after[k] for k in patch}),
                        invoice_after["revision"],
                        operation_id,
                    ),
                ).fetchone()
                task = current_in(conn, auth, company_id)
                if (
                    changed
                    and task
                    and task["intent"] == "complete_purposes"
                    and task["period"] == invoice["invoice_date"].strftime("%Y-%m")
                ):
                    task = conn.execute(
                        "UPDATE tasks SET revision=revision+1,authority_source_seq=greatest(authority_source_seq,%s),"
                        "unresolved_json=unresolved_json||%s,updated_at=now() WHERE task_id=%s RETURNING *",
                        (
                            source["source_seq"],
                            jsonb({"needs_refresh": True, "last_operation_id": operation_id}),
                            task["task_id"],
                        ),
                    ).fetchone()
                    _revision(conn, task, "invoice_saved", source_refs, source["source_seq"])
                    conn.execute(
                        "UPDATE task_slots SET revision=revision+1,authority_source_seq=greatest(authority_source_seq,%s) WHERE user_id=%s AND company_id=%s",
                        (source["source_seq"], auth.user_id, company_id),
                    )
                    epoch = conn.execute(
                        "UPDATE users SET memory_epoch=memory_epoch+1 WHERE user_id=%s RETURNING memory_epoch",
                        (auth.user_id,),
                    ).fetchone()["memory_epoch"]
                else:
                    epoch = user["memory_epoch"]
    except AppError as exc:
        if exc.code != "STALE_CONTEXT":
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE operations SET state='rejected',last_error=%s WHERE operation_id=%s AND state='prepared'",
                    (jsonb(exc.as_dict()), operation_id),
                )
        raise
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        try:
            with db.transaction(readonly=True) as conn:
                committed = conn.execute(
                    "SELECT state FROM operations WHERE operation_id=%s", (operation_id,)
                ).fetchone()
            if committed and committed["state"] in ("committed", "no_change"):
                return verify_operation(db, auth, operation_id)
        except psycopg.Error:
            pass
        raise AppError(
            "COMMIT_STATUS_UNKNOWN",
            "保存结果暂时无法确认，请按原操作编号核对，不要重复修改。",
            503,
            True,
            {"operation_id": operation_id},
        ) from exc
    return verify_operation(
        db, auth, operation_id, replayed=terminal, memory_epoch=epoch, known_row=row
    )
