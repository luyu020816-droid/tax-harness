"""Verify a durable invoice operation and queue only that operation when authorized."""

import psycopg

from app import invoice_write, mutations
from app.agent_authority import LOCK_ID, authorize_tool, validate_snapshot
from app.errors import AppError
from app.events import emit
from app.identity import assert_access, owned
from app.sources import read_source
from app.values import digest, jsonb, safe_json, uid


def _resume_plan(conn, auth, turn, operation):
    if turn["status"] not in ("failed", "interrupted", "completed", "resolved"):
        raise AppError("WRITE_PLAN_STALE", "原办理已结束或尚在处理中，请核对办理状态。", 409)
    session = owned(conn, "sessions", "session_id", turn["session_id"], auth)
    if session["selected_company_id"] != operation["company_id"] or session["archived_at"]:
        raise AppError("WRITE_PLAN_STALE", "请在原企业的可办理会话中继续。", 409)
    assert_access(conn, auth, operation["company_id"], write=True)
    source_id = str(operation["source_id"])
    plan_id = (
        str(operation["authorization_plan_id"]) if operation["authorization_plan_id"] else None
    )
    if not plan_id:
        raise AppError("WRITE_PLAN_STALE", "原操作的授权计划不可用，请重新核对。", 409)
    plan = invoice_write.validate(
        conn,
        auth,
        plan_id,
        company_id=operation["company_id"],
        invoice_id=operation["invoice_id"],
        patch=operation["patch"],
        source_id=source_id,
        expected_revision=operation["before_revision"],
    )
    invoice_write._require_current_task(conn, auth, plan)
    invoice_write._require_plan_company(conn, auth, plan)
    validate_snapshot(conn, turn["turn_id"])
    authorize_tool(conn, turn["turn_id"], "update_invoice", {"plan_id": plan_id})
    read_source(conn, auth, source_id)
    target = [
        auth.user_id,
        str(plan["origin_source_id"]),
        operation["company_id"],
        operation["invoice_id"],
    ]
    if (
        plan["status"] != "authorized"
        or plan["session_id"] != turn["session_id"]
        or operation["intent_key"] != digest(target)
        or operation["request_hash"] != digest([*target, operation["patch"]])
    ):
        raise AppError("WRITE_PLAN_STALE", "原操作与授权计划不一致，请重新核对。", 409)
    current = conn.execute(
        "SELECT i.revision,h.rules_epoch FROM invoices i JOIN company_heads h USING(company_id) "
        "WHERE i.company_id=%s AND i.invoice_id=%s",
        (operation["company_id"], operation["invoice_id"]),
    ).fetchone()
    if (
        not current
        or current["revision"] != plan["expected_invoice_revision"]
        or current["rules_epoch"] != plan["rules_epoch"]
    ):
        raise AppError("WRITE_PLAN_STALE", "发票或规则已变化，请重新核对修改计划。", 409)
    mutations._validate_selection(
        conn, auth, operation["company_id"], operation["invoice_id"], plan["selection_ref"], None
    )
    if plan["selection_ref"]:
        view = owned(conn, "result_views", "view_id", plan["selection_ref"]["view_id"], auth)
        if view["session_id"] != turn["session_id"]:
            raise AppError("WRITE_PLAN_STALE", "原选票清单已失效，请重新核对。", 409)
    return plan_id


def verify(db, auth, turn_id, operation_id):
    result = mutations.verify_operation(db, auth, operation_id, turn_id=turn_id, replayed=True)
    response = {"operation": result, "can_resume": False}
    if result["commit_state"] != "prepared":
        return safe_json(response)
    try:
        with db.transaction(readonly=True) as conn:
            turn = owned(conn, "turns", "turn_id", turn_id, auth)
            operation = mutations.operation_in(conn, auth, operation_id, turn_id=turn_id)
            if operation["state"] != "prepared" or turn["status"] in ("queued", "running"):
                raise mutations.verification_pending(operation_id)
            _resume_plan(conn, auth, turn, operation)
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        raise mutations.verification_pending(operation_id) from exc
    except AppError as exc:
        if exc.code == "VERIFICATION_PENDING":
            raise
        response["blocked_reason"] = exc.message
    else:
        response["can_resume"] = True
    return safe_json(response)


def enqueue(db, auth, turn_id, operation_id, *, limits=None):
    """Accept a user action for a settled prepared operation, without calling the model."""
    try:
        with db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (LOCK_ID,))
            conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
            turn = owned(conn, "turns", "turn_id", turn_id, auth, lock=True)
            operation = mutations.operation_in(conn, auth, operation_id, turn_id=turn_id, lock=True)
            if operation["state"] == "prepared":
                if turn["status"] in ("queued", "running"):
                    raise mutations.verification_pending(operation_id)
                plan_id = _resume_plan(conn, auth, turn, operation)
                from app.admission import check

                check(conn, auth.user_id, limits)
                generation = turn["active_run_generation"] + 1
                work = conn.execute(
                    "UPDATE work_items SET state='ready',lease_until=NULL,fencing_token=fencing_token+1,last_error=NULL "
                    "WHERE source_id=%s AND kind='turn' AND state IN ('failed','done','invalidated') RETURNING work_id",
                    (operation["source_id"],),
                ).fetchone()
                if not work:
                    raise AppError("WRITE_PLAN_STALE", "原操作无法继续，请重新核对。", 409)
                action = {"operation_id": operation_id, "plan_id": plan_id}
                request_id = f"invoice-recovery:{operation_id}:{generation}"
                conn.execute(
                    "INSERT INTO memory_change_ops(change_id,user_id,request_id,request_hash,target_type,"
                    "target_id,expected_revision,action,state,result_ref) "
                    "VALUES(%s,%s,%s,%s,'turn',%s,%s,'resume_invoice_operation','committed',%s)",
                    (
                        uid(),
                        auth.user_id,
                        request_id,
                        digest(action),
                        turn_id,
                        generation,
                        jsonb(action),
                    ),
                )
                conn.execute(
                    "UPDATE turns SET status='queued',error=NULL,updated_at=now() WHERE turn_id=%s",
                    (turn_id,),
                )
                emit(conn, turn_id, None, "recovery_queued", action)
                return {"turn_id": turn_id, "queued": True}
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        raise mutations.verification_pending(operation_id) from exc
    return {"turn_id": turn_id, "queued": False, **verify(db, auth, turn_id, operation_id)}


def continuation_in(conn, execution):
    action = conn.execute(
        "SELECT result_ref,expected_revision FROM memory_change_ops WHERE user_id=%s AND target_id=%s "
        "AND target_type='turn' AND action='resume_invoice_operation' AND state='committed' "
        "ORDER BY expected_revision DESC LIMIT 1",
        (execution.auth.user_id, execution.turn_id),
    ).fetchone()
    if action and action["expected_revision"] == execution.generation:
        return action["result_ref"]
    if execution.generation > 1:
        prepared = conn.execute(
            "SELECT operation_id FROM operations WHERE user_id=%s AND source_id=%s "
            "AND state='prepared' ORDER BY operation_id LIMIT 1",
            (execution.auth.user_id, execution.source_id),
        ).fetchone()
        if prepared:
            raise mutations.commit_status_unknown(prepared["operation_id"])
    if action:
        raise AppError(
            "INVOICE_RECOVERY_INTERRUPTED", "原操作已进入恢复流程，请按原编号核实结果。", 409
        )
    return None
