"""Durable, user-owned invoice plans; the operation ledger remains the truth."""

import re

from app.errors import AppError
from app.execution import guard
from app.identity import assert_access, owned
from app.sources import add_source, assess, next_sequence, read_source
from app.values import digest, jsonb, safe_json, uid
from app.write_commands import authorize_direct


def _owned(conn, auth, plan_id, *, lock=False):
    row = conn.execute(
        "SELECT *,expires_at>clock_timestamp() AS unexpired FROM write_plans "
        "WHERE plan_id=%s AND user_id=%s" + (" FOR UPDATE" if lock else ""),
        (plan_id, auth.user_id),
    ).fetchone()
    if not row:
        raise AppError("RESOURCE_NOT_FOUND", "修改计划不存在或不可访问。", 404)
    assert_access(conn, auth, row["company_id"], write=True)
    return row


def public_plan(row):
    return safe_json(
        {
            "kind": "write_plan",
            **{
                k: row[k]
                for k in (
                    "plan_id",
                    "company_id",
                    "invoice_id",
                    "patch",
                    "before_values",
                    "revision",
                    "intent_hash",
                    "status",
                    "expires_at",
                    "operation_id",
                    "confirmation_turn_id",
                )
            },
        }
    )


def get(db, auth, plan_id):
    with db.transaction(readonly=True) as conn:
        return public_plan(_owned(conn, auth, plan_id))


def prepare(
    db,
    auth,
    *,
    company_id,
    invoice_id,
    patch,
    expected_revision,
    source_id,
    source_refs,
    execution=None,
    selection_ref=None,
):
    from app.mutations import _validate_patch, _validate_selection, _validate_target_scope
    from app.sources import validate_refs

    _validate_patch(patch)
    with db.transaction() as conn:
        if execution:
            guard(conn, execution)
            if source_id != execution.source_id:
                raise AppError("WRITE_NOT_AUTHORIZED", "修改必须来自当前用户请求。", 422)
        else:
            conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
        assert_access(conn, auth, company_id, write=True)
        source = read_source(conn, auth, source_id)
        if source["role"] != "user":
            raise AppError("WRITE_NOT_AUTHORIZED", "资料或助手文字不能授权修改。", 422)
        refs = validate_refs(conn, auth, source_refs)
        if source_id not in {str(r["source_id"]) for r in refs}:
            raise AppError("NEEDS_CLARIFICATION", "缺少本轮用户原话。", 422)
        _validate_target_scope(conn, auth, source, company_id, execution)
        if any(v and v not in source["content"] for v in patch.values()):
            raise AppError("NEEDS_CLARIFICATION", "不能推测用途或备注，请提供实际内容。", 422)
        if any(
            v is None
            and not re.search(
                r"(?:清空|清除|删除)\s*(?:该|的)?"
                + field
                + "|"
                + field
                + r"\s*(?:清空|清除|删除|设为空)",
                source["content"],
            )
            for field, v in (("用途" if k == "purpose" else "备注", v) for k, v in patch.items())
        ):
            raise AppError("NEEDS_CLARIFICATION", "请明确需要清空哪个字段。", 422)
        _validate_selection(conn, auth, company_id, invoice_id, selection_ref, execution)
        # Keep exact invoice targeting checks even for a plan awaiting confirmation.
        from app.values import INVOICE_ID_PATTERN

        ids = set(re.findall(rf"(?<![A-Za-z0-9]){INVOICE_ID_PATTERN}(?![0-9])", source["content"]))
        if ids and invoice_id not in ids:
            raise AppError("NEEDS_CLARIFICATION", "修改票号与用户原话不一致。", 422)
        if not ids and not selection_ref:
            raise AppError("NEEDS_CLARIFICATION", "请明确发票编号或有效清单序号。", 422)
        fingerprint = digest([auth.user_id, source_id, company_id, invoice_id, patch])
        old = conn.execute(
            "SELECT * FROM write_plans WHERE user_id=%s AND origin_source_id=%s "
            "AND company_id=%s AND invoice_id=%s",
            (auth.user_id, source_id, company_id, invoice_id),
        ).fetchone()
        if old:
            if old["intent_hash"] != fingerprint:
                raise AppError(
                    "INTENT_CONFLICT", "同条指令对同票的修改已冻结，请用新消息更正。", 409
                )
            return public_plan(old)
        invoice = conn.execute(
            "SELECT * FROM invoices WHERE company_id=%s AND invoice_id=%s", (company_id, invoice_id)
        ).fetchone()
        if not invoice:
            raise AppError(
                "RESOURCE_NOT_FOUND",
                "该企业不存在这张发票。",
                404,
                details={
                    "resource_type": "invoice",
                    "company_id": company_id,
                    "invoice_id": invoice_id,
                },
            )
        if invoice["revision"] != expected_revision:
            raise AppError("VERSION_CONFLICT", "发票已变化，请核对当前记录。", 409)
        head = conn.execute(
            "SELECT * FROM company_heads WHERE company_id=%s", (company_id,)
        ).fetchone()
        aliases = conn.execute(
            "SELECT aliases FROM companies WHERE company_id=%s", (company_id,)
        ).fetchone()["aliases"]
        status = authorize_direct(source["content"], aliases, invoice_id, patch, selection_ref)
        row = conn.execute(
            "INSERT INTO write_plans(plan_id,user_id,session_id,origin_source_id,origin_source_revision,"
            "authorized_source_id,company_id,invoice_id,patch,before_values,expected_invoice_revision,"
            "selection_ref,data_revision,rules_epoch,intent_hash,status,authorization_kind) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                uid(),
                auth.user_id,
                execution.session_id if execution else None,
                source_id,
                source["revision"],
                source_id if status == "authorized" else None,
                company_id,
                invoice_id,
                jsonb(patch),
                jsonb({k: invoice[k] for k in patch}),
                expected_revision,
                jsonb(selection_ref) if selection_ref else None,
                head["data_revision"],
                head["rules_epoch"],
                fingerprint,
                status,
                "direct_command" if status == "authorized" else None,
            ),
        ).fetchone()
        return public_plan(row)


def validate(
    conn, auth, plan_id, *, company_id, invoice_id, patch, source_id, expected_revision, lock=False
):
    if not plan_id:
        raise AppError("WRITE_AUTHORIZATION_REQUIRED", "请先生成并授权修改计划，未保存。", 422)
    row = _owned(conn, auth, plan_id, lock=lock)
    if row["status"] not in ("authorized", "consumed"):
        raise AppError("WRITE_NOT_AUTHORIZED", "该修改未获得本人授权，未保存。", 422)
    if row["status"] != "consumed" and not row["unexpired"]:
        raise AppError("WRITE_PLAN_EXPIRED", "修改计划已过期，请重新核对。", 409)
    if (
        row["company_id"] != company_id
        or row["invoice_id"] != invoice_id
        or row["patch"] != patch
        or str(row["authorized_source_id"]) != source_id
        or row["expected_invoice_revision"] != expected_revision
    ):
        raise AppError("WRITE_NOT_AUTHORIZED", "修改目标或内容与授权计划不一致。", 422)
    origin = read_source(
        conn, auth, str(row["origin_source_id"]), allow_suppressed=row["status"] == "consumed"
    )
    if origin["revision"] != row["origin_source_revision"]:
        raise AppError("SOURCE_INVALIDATED", "原修改来源已变化。", 409)
    return row


def commit(db, auth, plan_id, *, execution):
    from app.mutations import update_invoice

    with db.transaction(readonly=True) as conn:
        plan = _owned(conn, auth, plan_id)
        if str(plan["authorized_source_id"]) != execution.source_id:
            raise AppError("WRITE_NOT_AUTHORIZED", "计划不属于本轮授权。", 422)
        source = read_source(conn, auth, execution.source_id)
    return update_invoice(
        db,
        auth,
        company_id=plan["company_id"],
        invoice_id=plan["invoice_id"],
        patch=plan["patch"],
        expected_revision=plan["expected_invoice_revision"],
        source_id=execution.source_id,
        source_refs=[{"source_id": execution.source_id, "quote": source["content"]}],
        selection_ref=plan["selection_ref"],
        execution=execution,
        plan_id=plan_id,
    )


def confirm(db, auth, plan_id, *, expected_revision, intent_hash, request_id):
    from app.events import emit

    with db.transaction() as conn:
        conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
        plan = _owned(conn, auth, plan_id)
        conn.execute(
            "SELECT 1 FROM company_heads WHERE company_id=%s FOR UPDATE", (plan["company_id"],)
        )
        plan = _owned(conn, auth, plan_id, lock=True)
        if plan["intent_hash"] != intent_hash:
            raise AppError("VERSION_CONFLICT", "确认内容与计划不一致。", 409)
        if plan["confirmation_turn_id"]:
            return {"turn_id": str(plan["confirmation_turn_id"]), "replayed": True}
        if (
            plan["status"] != "needs_confirmation"
            or not plan["unexpired"]
            or plan["revision"] != expected_revision
        ):
            raise AppError("WRITE_PLAN_STALE", "计划已失效，请重新核对。", 409)
        session = owned(conn, "sessions", "session_id", str(plan["session_id"]), auth)
        if session["selected_company_id"] != plan["company_id"]:
            raise AppError("WRITE_PLAN_STALE", "当前企业已切换，请重新核对目标。", 409)
        invoice = conn.execute(
            "SELECT revision FROM invoices WHERE company_id=%s AND invoice_id=%s FOR UPDATE",
            (plan["company_id"], plan["invoice_id"]),
        ).fetchone()
        head = conn.execute(
            "SELECT * FROM company_heads WHERE company_id=%s", (plan["company_id"],)
        ).fetchone()
        if (
            invoice["revision"] != plan["expected_invoice_revision"]
            or head["rules_epoch"] != plan["rules_epoch"]
        ):
            raise AppError("WRITE_PLAN_STALE", "发票或规则已变化，请重新核对。", 409)
        origin = read_source(conn, auth, str(plan["origin_source_id"]))
        if origin["revision"] != plan["origin_source_revision"]:
            raise AppError("SOURCE_INVALIDATED", "原修改来源已变化，请重新核对。", 409)
        turn_id, seq = uid(), next_sequence(conn, auth)
        text = f"确认修改{plan['company_id']}发票{plan['invoice_id']}用途/备注。"
        conn.execute(
            "INSERT INTO turns(turn_id,session_id,user_id,client_request_id,request_hash,source_seq,text,request_kind,control_ref) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,'write_confirmation',%s)",
            (
                turn_id,
                plan["session_id"],
                auth.user_id,
                f"confirm:{plan_id}:{request_id}",
                digest([plan_id, intent_hash]),
                seq,
                text,
                plan_id,
            ),
        )
        source = add_source(
            conn,
            auth,
            {
                "action": "confirm_invoice_write",
                "plan_id": plan_id,
                "company_id": plan["company_id"],
                "invoice_id": plan["invoice_id"],
                "patch": plan["patch"],
            },
            kind="user_action",
            turn_id=turn_id,
            source_seq=seq,
            companies=[plan["company_id"]],
        )
        conn.execute(
            "INSERT INTO work_items(work_id,user_id,source_id,kind) VALUES(%s,%s,%s,'turn')",
            (uid(), auth.user_id, source["source_id"]),
        )
        conn.execute(
            "UPDATE write_plans SET status='authorized',authorization_kind='user_confirmation',"
            "authorized_source_id=%s,confirmation_turn_id=%s,revision=revision+1 WHERE plan_id=%s",
            (source["source_id"], turn_id, plan_id),
        )
        assess(conn, str(source["source_id"]), "accepted")
        emit(conn, turn_id, None, "accepted", {"turn_id": turn_id, "status": "queued"})
        return {"turn_id": turn_id, "replayed": False}


def cancel(db, auth, plan_id):
    with db.transaction() as conn:
        conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
        row = _owned(conn, auth, plan_id, lock=True)
        if row["status"] in ("consumed", "revoked"):
            return public_plan(row)
        if row["confirmation_turn_id"]:
            conn.execute(
                "UPDATE turns SET status='cancelled' WHERE turn_id=%s AND status IN ('queued','running')",
                (row["confirmation_turn_id"],),
            )
            conn.execute(
                "UPDATE work_items SET state='cancelled' WHERE source_id IN (SELECT source_id FROM sources WHERE turn_id=%s)",
                (row["confirmation_turn_id"],),
            )
        row = conn.execute(
            "UPDATE write_plans SET status='revoked',revision=revision+1 WHERE plan_id=%s RETURNING *",
            (plan_id,),
        ).fetchone()
        add_source(
            conn,
            auth,
            {"action": "cancel_invoice_write", "plan_id": plan_id},
            kind="user_action",
            source_seq=next_sequence(conn, auth),
            companies=[row["company_id"]],
        )
        closed = conn.execute(
            "UPDATE turns SET status='resolved',pending_clarification=jsonb_set(pending_clarification,'{state}','\"cancelled\"') "
            "WHERE turn_id=(SELECT turn_id FROM sources WHERE source_id=%s) AND status='waiting_user' RETURNING turn_id",
            (row["origin_source_id"],),
        ).fetchone()
        if closed:
            conn.execute(
                "UPDATE users SET memory_epoch=memory_epoch+1 WHERE user_id=%s", (auth.user_id,)
            )
            conn.execute(
                "UPDATE summaries SET status='invalidated' WHERE user_id=%s AND status='active'",
                (auth.user_id,),
            )
        return public_plan(row)
