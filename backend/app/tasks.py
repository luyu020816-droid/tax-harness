from app.errors import AppError
from app.execution import Execution
from app.identity import Identity, assert_access, owned
from app.memory_changes import assert_newer, begin_change, finish_change, suppress_old_history
from app.values import jsonb, month_bounds, uid

UNFINISHED = ("active", "waiting_user")
INTENTS = {"query_month", "explain_rules", "complete_purposes"}


def current_in(conn, auth: Identity, company_id: str) -> dict | None:
    assert_access(conn, auth, company_id)
    return conn.execute(
        "SELECT t.* FROM task_slots s JOIN tasks t ON t.task_id=s.current_task_id "
        "WHERE s.user_id=%s AND s.company_id=%s AND t.status IN ('active','waiting_user')",
        (auth.user_id, company_id),
    ).fetchone()


def list_tasks(db, auth: Identity, company_id=None) -> list[dict]:
    with db.transaction(readonly=True) as conn:
        if company_id:
            assert_access(conn, auth, company_id)
        return conn.execute(
            "SELECT t.*,coalesce(t.checked_data_revision=h.data_revision AND t.checked_rules_epoch=h.rules_epoch,false) AS snapshot_current FROM tasks t JOIN user_company_grants g USING(user_id,company_id) JOIN company_heads h USING(company_id) "
            "WHERE t.user_id=%s AND g.can_read AND t.status<>'deleted' "
            "AND (%s::text IS NULL OR t.company_id=%s) ORDER BY t.updated_at DESC LIMIT 100",
            (auth.user_id, company_id, company_id),
        ).fetchall()


def _slot(conn, auth, company_id):
    conn.execute(
        "INSERT INTO task_slots(user_id,company_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
        (auth.user_id, company_id),
    )
    return conn.execute(
        "SELECT * FROM task_slots WHERE user_id=%s AND company_id=%s FOR UPDATE",
        (auth.user_id, company_id),
    ).fetchone()


def _revision(conn, task, change_type, refs, sequence):
    conn.execute(
        "INSERT INTO task_revisions(task_id,revision,change_type,snapshot,source_refs,source_seq) VALUES(%s,%s,%s,%s,%s,%s)",
        (task["task_id"], task["revision"], change_type, jsonb(task), jsonb(refs), sequence),
    )


def _close_pending(conn, task, status):
    conn.execute(
        "UPDATE turns SET status=%s,pending_clarification=jsonb_set(pending_clarification,'{state}',to_jsonb(%s::text)),updated_at=now() "
        "WHERE user_id=%s AND status='waiting_user' AND pending_clarification->>'task_id'=%s "
        "AND pending_clarification->>'task_generation'=%s",
        (status, status, task["user_id"], str(task["task_id"]), str(task["generation"])),
    )
    conn.execute(
        "UPDATE result_views SET status='invalidated' WHERE user_id=%s AND company_id=%s",
        (task["user_id"], task["company_id"]),
    )


def _end(conn, task, status, sequence, refs):
    row = conn.execute(
        "UPDATE tasks SET status=%s,revision=revision+1,authority_source_seq=%s,updated_at=now(),"
        "deleted_at=CASE WHEN %s='deleted' THEN now() ELSE NULL END WHERE task_id=%s RETURNING *",
        (status, sequence, status, task["task_id"]),
    ).fetchone()
    conn.execute(
        "UPDATE task_slots SET current_task_id=NULL,generation=generation+1,revision=revision+1,authority_source_seq=%s "
        "WHERE user_id=%s AND company_id=%s AND current_task_id=%s",
        (sequence, task["user_id"], task["company_id"], task["task_id"]),
    )
    _revision(conn, row, status, refs, sequence)
    _close_pending(conn, task, status)
    return row


def change_task(
    db,
    auth: Identity,
    *,
    company_id: str,
    action: str,
    request_id: str,
    intent=None,
    period=None,
    target_id=None,
    expected_revision=None,
    unresolved=None,
    source_refs=None,
    execution: Execution | None = None,
) -> dict:
    if action not in ("start", "update", "replace", "cancel", "delete"):
        raise AppError("INVALID_ARGUMENT", "不支持的任务操作。")
    if period:
        month_bounds(period)
    if intent and intent not in INTENTS:
        raise AppError("INVALID_ARGUMENT", "任务类型不在首版支持范围。")
    if intent == "complete_purposes" and period and period != "2026-09":
        raise AppError("PERIOD_UNSUPPORTED", "初赛用途检查范围为 2026 年 9 月。", 422)
    body = {
        "company_id": company_id,
        "intent": intent,
        "period": period,
        "target_id": target_id,
        "expected_revision": expected_revision,
        "unresolved": unresolved or {},
    }
    with db.transaction() as conn:
        assert_access(conn, auth, company_id)
        existing, change = begin_change(
            conn, auth, "task", action, body, request_id, execution, source_refs or []
        )
        if change is None:
            current = owned(conn, "tasks", "task_id", str(existing["task_id"]), auth)
            epoch = conn.execute(
                "SELECT memory_epoch FROM users WHERE user_id=%s", (auth.user_id,)
            ).fetchone()["memory_epoch"]
            return {**current, "memory_epoch": epoch, "replayed": True}
        old = (
            owned(conn, "tasks", "task_id", target_id, auth)
            if target_id
            else current_in(conn, auth, company_id)
        )
        companies = sorted({company_id, old["company_id"]} if old else {company_id})
        conn.execute(
            "SELECT * FROM company_heads WHERE company_id=ANY(%s) ORDER BY company_id FOR UPDATE",
            (companies,),
        )
        slots = {company: _slot(conn, auth, company) for company in companies}
        slot = slots[company_id]
        if old:
            old = owned(conn, "tasks", "task_id", str(old["task_id"]), auth, lock=True)
            assert_newer(change["source_seq"], old)
            if action == "start":
                if (
                    old["intent"] == intent
                    and old["period"] == period
                    and old["status"] in UNFINISHED
                ):
                    return finish_change(conn, auth, change, old)
                raise AppError(
                    "NEEDS_CLARIFICATION", "该企业已有未完成任务，是否替换为新的目标？", 422
                )
            if expected_revision != old["revision"]:
                raise AppError("VERSION_CONFLICT", "任务已变化，请先查看当前任务。", 409)
            if action in ("cancel", "delete"):
                if old["company_id"] != company_id:
                    raise AppError("RESOURCE_NOT_FOUND", "任务不属于该企业。", 404)
                result = _end(
                    conn,
                    old,
                    "deleted" if action == "delete" else "cancelled",
                    change["source_seq"],
                    change["refs"],
                )
                if action in ("cancel", "delete"):
                    revisions = conn.execute(
                        "SELECT source_refs FROM task_revisions WHERE task_id=%s", (old["task_id"],)
                    ).fetchall()
                    for revision in revisions:
                        suppress_old_history(
                            conn,
                            auth,
                            revision["source_refs"],
                            kind="task",
                            target_id=old["task_id"],
                            before_seq=change["source_seq"],
                        )
                return finish_change(conn, auth, change, result)
            if old["status"] not in UNFINISHED:
                raise AppError(
                    "SOURCE_INVALIDATED", "已结束任务不能被旧更新恢复，请明确新建任务。", 409
                )
            if action == "update":
                if old["company_id"] != company_id:
                    raise AppError("NEEDS_CLARIFICATION", "跨企业更换目标应使用明确替换操作。", 422)
                new_period = period if period is not None else old["period"]
                result = conn.execute(
                    "UPDATE tasks SET period=%s,intent=%s,status=%s,unresolved_json=%s,revision=revision+1,"
                    "authority_source_seq=%s,last_check_ref=NULL,checked_data_revision=NULL,checked_rules_epoch=NULL,updated_at=now() "
                    "WHERE task_id=%s RETURNING *",
                    (
                        new_period,
                        intent or old["intent"],
                        "active" if new_period else "waiting_user",
                        jsonb(unresolved or {}),
                        change["source_seq"],
                        old["task_id"],
                    ),
                ).fetchone()
                conn.execute(
                    "UPDATE task_slots SET revision=revision+1,authority_source_seq=%s WHERE user_id=%s AND company_id=%s",
                    (change["source_seq"], auth.user_id, company_id),
                )
                _revision(conn, result, "updated", change["refs"], change["source_seq"])
                return finish_change(conn, auth, change, result)
            if action == "replace":
                if old["company_id"] != company_id and slot["current_task_id"]:
                    raise AppError(
                        "NEEDS_CLARIFICATION", "目标企业也有未完成任务，需要先明确如何处理。", 422
                    )
                _end(conn, old, "replaced", change["source_seq"], change["refs"])
                slot = _slot(conn, auth, company_id)
        elif action not in ("start", "replace"):
            raise AppError("RESOURCE_NOT_FOUND", "没有可更新的任务。", 404)
        if slot["current_task_id"]:
            raise AppError("NEEDS_CLARIFICATION", "该企业已有未完成任务。", 422)
        if not old:
            assert_newer(change["source_seq"], slot)
        if not intent:
            raise AppError("INVALID_ARGUMENT", "新任务需要明确目标类型。")
        result = conn.execute(
            "INSERT INTO tasks(task_id,user_id,company_id,period,intent,status,generation,authority_source_seq,unresolved_json) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                uid(),
                auth.user_id,
                company_id,
                period,
                intent,
                "active" if period else "waiting_user",
                slot["generation"] + 1,
                change["source_seq"],
                jsonb(unresolved or {}),
            ),
        ).fetchone()
        conn.execute(
            "UPDATE task_slots SET current_task_id=%s,generation=%s,revision=revision+1,authority_source_seq=%s "
            "WHERE user_id=%s AND company_id=%s",
            (
                result["task_id"],
                result["generation"],
                change["source_seq"],
                auth.user_id,
                company_id,
            ),
        )
        _revision(conn, result, "created", change["refs"], change["source_seq"])
        return finish_change(conn, auth, change, result)


def apply_result(
    db,
    auth: Identity,
    task: dict,
    result: dict,
    result_source_id: str,
    source_seq: int,
    execution: Execution | None = None,
) -> dict:
    """Compare the exact check/query snapshot before accepting progress or completion."""
    with db.transaction() as conn:
        if execution:
            from app.execution import guard

            guard(conn, execution)
        else:
            conn.execute("SELECT * FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
        assert_access(conn, auth, task["company_id"])
        head = conn.execute(
            "SELECT * FROM company_heads WHERE company_id=%s FOR UPDATE", (task["company_id"],)
        ).fetchone()
        slot = _slot(conn, auth, task["company_id"])
        current = owned(conn, "tasks", "task_id", str(task["task_id"]), auth, lock=True)
        if (
            str(slot["current_task_id"]) != str(task["task_id"])
            or current["revision"] != task["revision"]
            or current["generation"] != task["generation"]
            or source_seq < current["authority_source_seq"]
        ):
            raise AppError("STALE_CONTEXT", "任务已变化，旧结果不能更新进度。", 409)
        if (
            result.get("company_id") != current["company_id"]
            or result.get("period") != current["period"]
        ):
            raise AppError("STALE_CONTEXT", "结果与任务范围不一致。", 409)
        if any(result.get(k) != head[k] for k in ("data_revision", "rules_epoch")):
            raise AppError("STALE_CONTEXT", "检查后资料或规则已变化，请重新检查。", 409)
        expected_kind = {
            "complete_purposes": "purpose_check",
            "query_month": "monthly_summary",
            "explain_rules": "rules",
        }[current["intent"]]
        if result.get("kind") != expected_kind:
            raise AppError("INVALID_ARGUMENT", "该结果不能作为当前任务的完成依据。")
        completed = (
            result.get("missing_count") == 0 if current["intent"] == "complete_purposes" else True
        )
        status = "completed" if completed else "waiting_user"
        unresolved = {"missing_count": result.get("missing_count"), "needs_refresh": False}
        updated = conn.execute(
            "UPDATE tasks SET status=%s,revision=revision+1,authority_source_seq=%s,unresolved_json=%s,"
            "last_check_ref=%s,checked_data_revision=%s,checked_rules_epoch=%s,updated_at=now() WHERE task_id=%s RETURNING *",
            (
                status,
                source_seq,
                jsonb(unresolved),
                result_source_id,
                head["data_revision"],
                head["rules_epoch"],
                current["task_id"],
            ),
        ).fetchone()
        conn.execute(
            "UPDATE task_slots SET current_task_id=%s,revision=revision+1,authority_source_seq=%s WHERE user_id=%s AND company_id=%s",
            (
                None if completed else current["task_id"],
                source_seq,
                auth.user_id,
                task["company_id"],
            ),
        )
        _revision(conn, updated, "checked", [{"source_id": result_source_id}], source_seq)
        if completed:
            _close_pending(conn, current, "resolved")
        epoch = conn.execute(
            "UPDATE users SET memory_epoch=memory_epoch+1 WHERE user_id=%s RETURNING memory_epoch",
            (auth.user_id,),
        ).fetchone()["memory_epoch"]
        conn.execute("UPDATE summaries SET status='invalidated' WHERE user_id=%s", (auth.user_id,))
        return {**updated, "memory_epoch": epoch}
