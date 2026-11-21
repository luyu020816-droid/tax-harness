from app.errors import AppError
from app.execution import Execution
from app.identity import Identity, assert_access, owned
from app.memory_changes import (
    assert_newer,
    begin_change,
    bump_task_memory,
    finish_change,
    suppress_old_history,
)
from app.sources import read_source
from app.task_commands import (
    authorize_task_addition,
    authorize_task_change,
    authorize_task_clear,
    authorize_task_update,
)
from app.values import jsonb, month_bounds, safe_json, uid

UNFINISHED = ("active", "waiting_user")
INTENTS = {"query_month", "explain_rules", "complete_purposes"}
_OMITTED_PERIOD = object()
_CHECK_FIELDS = {"missing_count", "needs_refresh", "last_operation_id"}


def stale_check(fields):
    return {
        **{key: value for key, value in fields.items() if key not in _CHECK_FIELDS},
        "needs_refresh": True,
    }


def task_summary(task):
    fields = ("task_id", "company_id", "period", "intent", "status", "revision", "generation")
    return {key: task[key] for key in fields}


def current_in(conn, auth: Identity, company_id: str) -> dict | None:
    assert_access(conn, auth, company_id)
    task = conn.execute(
        "SELECT t.*,coalesce(t.checked_data_revision=h.data_revision "
        "AND t.checked_rules_epoch=h.rules_epoch,false) AS snapshot_current "
        "FROM task_slots s JOIN tasks t ON t.task_id=s.current_task_id "
        "JOIN company_heads h ON h.company_id=t.company_id "
        "WHERE s.user_id=%s AND s.company_id=%s AND t.status IN ('active','waiting_user')",
        (auth.user_id, company_id),
    ).fetchone()
    if task and not task["snapshot_current"]:
        task["unresolved_json"] = stale_check(task["unresolved_json"])
    return task


def active_in(conn, auth: Identity, company_id: str | None) -> list[dict]:
    if company_id:
        assert_access(conn, auth, company_id)
    return conn.execute(
        "SELECT t.* FROM tasks t JOIN user_company_grants g USING(user_id,company_id) "
        "WHERE t.user_id=%s AND g.can_read AND t.status IN ('active','waiting_user') "
        "AND (%s::text IS NULL OR t.company_id=%s) ORDER BY t.authority_source_seq DESC,t.task_id",
        (auth.user_id, company_id, company_id),
    ).fetchall()


def matching_in(conn, auth, company_id, intent, period, *, allow_unset=False):
    active = [task for task in active_in(conn, auth, company_id) if task["intent"] == intent]
    for task in active:
        if task["period"] == period:
            return task
    if allow_unset and len(active) == 1 and (active[0]["period"] is None or period is None):
        return active[0]
    return None


def list_tasks(db, auth: Identity, company_id=None) -> list[dict]:
    with db.transaction(readonly=True) as conn:
        if company_id:
            assert_access(conn, auth, company_id)
        rows = conn.execute(
            "SELECT t.*,coalesce(t.checked_data_revision=h.data_revision AND t.checked_rules_epoch=h.rules_epoch,false) AS snapshot_current FROM tasks t JOIN user_company_grants g USING(user_id,company_id) JOIN company_heads h USING(company_id) "
            "WHERE t.user_id=%s AND g.can_read AND t.status<>'deleted' "
            "AND (%s::text IS NULL OR t.company_id=%s) ORDER BY t.updated_at DESC LIMIT 100",
            (auth.user_id, company_id, company_id),
        ).fetchall()
        for task in rows:
            if not task["snapshot_current"]:
                task["unresolved_json"] = stale_check(task["unresolved_json"])
        return rows


def recent_cancelled_purpose_task(conn, execution, company_id):
    return conn.execute(
        "SELECT t.* FROM tasks t JOIN turns u "
        "ON u.user_id=t.user_id AND u.source_seq=t.authority_source_seq "
        "WHERE t.user_id=%s AND t.company_id=%s AND u.session_id=%s "
        "AND u.source_seq<%s AND t.intent='complete_purposes' AND t.status='cancelled' "
        "ORDER BY t.authority_source_seq DESC LIMIT 1",
        (execution.auth.user_id, company_id, execution.session_id, execution.source_seq),
    ).fetchone()


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


def _revoke_task_plans(conn, task):
    conn.execute(
        "UPDATE write_plans SET status='revoked',revision=revision+1 "
        "WHERE task_id=%s AND task_generation=%s AND status IN ('authorized','needs_confirmation')",
        (task["task_id"], task["generation"]),
    )


def _close_pending(conn, task, status):
    # A task result covers one intent. The reply verifies the full checklist.
    guarded = jsonb([{"kind": "request_progress", "items": [{"status": "waiting_user"}]}])
    conn.execute(
        "UPDATE turns SET status=%s,pending_clarification=jsonb_set(pending_clarification,'{state}',to_jsonb(%s::text)),updated_at=now() "
        "WHERE user_id=%s AND status='waiting_user' AND pending_clarification->>'task_id'=%s "
        "AND pending_clarification->>'task_generation'=%s "
        "AND (%s<>'resolved' OR NOT final_cards @> %s)",
        (
            status,
            status,
            task["user_id"],
            str(task["task_id"]),
            str(task["generation"]),
            status,
            guarded,
        ),
    )
    conn.execute(
        "UPDATE result_views v SET status='invalidated' WHERE v.user_id=%s AND v.company_id=%s "
        "AND NOT EXISTS(SELECT 1 FROM tasks sibling WHERE sibling.user_id=v.user_id "
        "AND sibling.company_id=v.company_id AND sibling.status IN ('active','waiting_user') "
        "AND (sibling.period=v.period OR v.period IS NULL)) "
        "AND (v.period IS NOT DISTINCT FROM %s OR NOT EXISTS(SELECT 1 FROM tasks sibling "
        "WHERE sibling.user_id=v.user_id AND sibling.company_id=v.company_id "
        "AND sibling.status IN ('active','waiting_user')))",
        (task["user_id"], task["company_id"], task["period"]),
    )


def _advance_after_close(conn, task, sequence):
    conn.execute(
        "UPDATE task_slots s SET current_task_id=CASE WHEN current_task_id=%s THEN ("
        "SELECT t.task_id FROM tasks t WHERE t.user_id=s.user_id AND t.company_id=s.company_id "
        "AND t.status IN ('active','waiting_user') ORDER BY t.authority_source_seq DESC,t.task_id LIMIT 1"
        ") ELSE current_task_id END,generation=generation+1,revision=revision+1,"
        "authority_source_seq=greatest(authority_source_seq,%s) WHERE user_id=%s AND company_id=%s",
        (task["task_id"], sequence, task["user_id"], task["company_id"]),
    )


def _end(conn, task, status, sequence, refs):
    row = conn.execute(
        "UPDATE tasks SET status=%s,revision=revision+1,authority_source_seq=%s,updated_at=now(),"
        "deleted_at=CASE WHEN %s='deleted' THEN now() ELSE NULL END WHERE task_id=%s RETURNING *",
        (status, sequence, status, task["task_id"]),
    ).fetchone()
    _advance_after_close(conn, task, sequence)
    _revision(conn, row, status, refs, sequence)
    _revoke_task_plans(conn, task)
    _close_pending(conn, task, status)
    return row


def _company_aliases(conn, *company_ids):
    return {
        row["company_id"]: [row["company_id"], row["name"], *row["aliases"]]
        for row in conn.execute(
            "SELECT company_id,name,aliases FROM companies WHERE company_id=ANY(%s)",
            (list(company_ids),),
        )
    }


def _require_task_command(conn, execution, action, task, body, candidates):
    company_id = body["company_id"]
    aliases = _company_aliases(conn, company_id, task["company_id"])
    text = read_source(conn, execution.auth, execution.source_id)["content"]
    names = aliases[task["company_id"]]
    matched = []
    for candidate in candidates:
        if action == "update":
            authorized = authorize_task_update(text, candidate, names, body)
        else:
            authorized = authorize_task_change(
                text,
                action,
                candidate,
                names,
                replacement=body if action == "replace" else None,
                replacement_aliases=aliases[company_id],
            )
        if authorized:
            matched.append(candidate["task_id"])
    if matched != [task["task_id"]]:
        raise AppError(
            "NEEDS_CLARIFICATION",
            "待办保留原状。请明确要操作的任务、月份和办理目标。",
            422,
            details={
                "task_candidates": safe_json([task_summary(candidate) for candidate in candidates])
            },
        )


def change_task(
    db,
    auth: Identity,
    *,
    company_id: str,
    action: str,
    request_id: str,
    intent=None,
    period=_OMITTED_PERIOD,
    target_id=None,
    expected_revision=None,
    unresolved=None,
    clear_unresolved=(),
    source_refs=None,
    execution: Execution | None = None,
) -> dict:
    if action not in ("start", "update", "replace", "cancel", "delete"):
        raise AppError("INVALID_ARGUMENT", "不支持的任务操作。")
    clear_period = action == "update" and period is None
    period = None if period is _OMITTED_PERIOD else period
    if period:
        month_bounds(period)
    if intent and intent not in INTENTS:
        raise AppError("INVALID_ARGUMENT", "任务类型不在首版支持范围。")
    if intent == "complete_purposes" and period and period != "2026-09":
        raise AppError("PERIOD_UNSUPPORTED", "初赛用途检查范围为 2026 年 9 月。", 422)
    if _CHECK_FIELDS.intersection(unresolved or {}) or _CHECK_FIELDS.intersection(clear_unresolved):
        raise AppError("INVALID_ARGUMENT", "业务检查状态只能由实际查询更新。", 422)
    if clear_unresolved and (
        action != "update" or set(clear_unresolved).intersection(unresolved or {})
    ):
        raise AppError("INVALID_ARGUMENT", "只能在更新时清除已点名且未同时设置的字段。", 422)
    body = {
        "company_id": company_id,
        "intent": intent,
        "period": period,
        "target_id": target_id,
        "expected_revision": expected_revision,
        "unresolved": unresolved or {},
    }
    if clear_unresolved:
        body["clear_unresolved"] = sorted(set(clear_unresolved))
    if clear_period:
        body["clear_period"] = True
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
        active = active_in(conn, auth, company_id)
        if action == "start" and active:
            existing_task = next(
                (task for task in active if (task["intent"], task["period"]) == (intent, period)),
                None,
            )
            if existing_task:
                assert_newer(change["source_seq"], existing_task)
                return finish_change(conn, auth, change, existing_task)
            aliases = _company_aliases(conn, company_id)[company_id]
            if not execution or not authorize_task_addition(
                read_source(conn, auth, execution.source_id)["content"], body, aliases
            ):
                raise AppError(
                    "NEEDS_CLARIFICATION", "该企业已有未完成任务，请明确另建或替换哪项任务。", 422
                )
            old = None
        if old:
            old = owned(conn, "tasks", "task_id", str(old["task_id"]), auth, lock=True)
            assert_newer(change["source_seq"], old)
            if action == "start":
                raise AppError("INVALID_ARGUMENT", "新任务无需指定已有任务编号。", 422)
            if expected_revision != old["revision"]:
                raise AppError("VERSION_CONFLICT", "任务已变化，请先查看当前任务。", 409)
            if execution:
                candidates = (
                    active
                    if old["company_id"] == company_id
                    else active_in(conn, auth, old["company_id"])
                )
                if old["status"] not in UNFINISHED:
                    raise AppError("STALE_CONTEXT", "待办已变更，请重新查看当前事项。", 409)
                if action in ("cancel", "delete", "replace") or len(candidates) > 1:
                    _require_task_command(conn, execution, action, old, body, candidates)
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
                revisions = conn.execute(
                    "SELECT source_refs FROM task_revisions WHERE task_id=%s AND source_seq<%s",
                    (old["task_id"], change["source_seq"]),
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
                if execution and len(active) == 1 and (clear_unresolved or clear_period):
                    text = read_source(conn, auth, execution.source_id)["content"]
                    if not authorize_task_clear(
                        text, [*clear_unresolved, *(["period"] if clear_period else [])]
                    ):
                        raise AppError("NEEDS_CLARIFICATION", "请明确要清除的用户字段。", 422)
                new_period = period if period is not None or clear_period else old["period"]
                new_intent = intent or old["intent"]
                if new_intent == "complete_purposes" and new_period not in (None, "2026-09"):
                    raise AppError("PERIOD_UNSUPPORTED", "初赛用途检查范围为 2026 年 9 月。", 422)
                scope_changed = (new_period, new_intent) != (old["period"], old["intent"])
                if scope_changed and any(
                    t["task_id"] != old["task_id"]
                    and (t["period"], t["intent"]) == (new_period, new_intent)
                    for t in active
                ):
                    raise AppError("VERSION_CONFLICT", "已有同企业、月份和目标的未完成任务。", 409)
                fields = {**old["unresolved_json"], **(unresolved or {})}
                for key in clear_unresolved:
                    fields.pop(key, None)
                if scope_changed:
                    _revoke_task_plans(conn, old)
                    fields = stale_check(fields)
                    missing = fields.get("missing_fields")
                    if new_period and isinstance(missing, list) and "period" in missing:
                        fields["missing_fields"] = [field for field in missing if field != "period"]
                        fields.pop("question", None)
                result = conn.execute(
                    "UPDATE tasks SET period=%s,intent=%s,status=%s,unresolved_json=%s,revision=revision+1,"
                    "authority_source_seq=%s,last_check_ref=%s,checked_data_revision=%s,checked_rules_epoch=%s,updated_at=now() "
                    "WHERE task_id=%s RETURNING *",
                    (
                        new_period,
                        new_intent,
                        ("active" if new_period else "waiting_user")
                        if scope_changed
                        else old["status"],
                        jsonb(fields),
                        change["source_seq"],
                        None if scope_changed else old["last_check_ref"],
                        None if scope_changed else old["checked_data_revision"],
                        None if scope_changed else old["checked_rules_epoch"],
                        old["task_id"],
                    ),
                ).fetchone()
                conn.execute(
                    "UPDATE task_slots SET revision=revision+1,authority_source_seq=greatest(authority_source_seq,%s) "
                    "WHERE user_id=%s AND company_id=%s",
                    (change["source_seq"], auth.user_id, company_id),
                )
                _revision(conn, result, "updated", change["refs"], change["source_seq"])
                return finish_change(conn, auth, change, result)
            if action == "replace":
                if old["company_id"] != company_id:
                    if slot["current_task_id"]:
                        raise AppError(
                            "NEEDS_CLARIFICATION", "目标企业也有未完成任务，需要先明确如何处理。", 422
                        )
                    change["memory_scope"] = (old["company_id"], company_id)
                _end(conn, old, "replaced", change["source_seq"], change["refs"])
                slot = _slot(conn, auth, company_id)
        elif execution and action == "replace":
            raise AppError("NEEDS_CLARIFICATION", "没有可替换的活动任务，请明确要新建的任务。", 422)
        elif action not in ("start", "replace"):
            raise AppError("RESOURCE_NOT_FOUND", "没有可更新的任务。", 404)
        if action == "replace" and any(
            task["task_id"] != (old["task_id"] if old else None)
            and (task["period"], task["intent"]) == (period, intent)
            for task in active
        ):
            raise AppError("VERSION_CONFLICT", "已有同企业、月份和目标的未完成任务。", 409)
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
        _slot(conn, auth, task["company_id"])
        current = owned(conn, "tasks", "task_id", str(task["task_id"]), auth, lock=True)
        if (
            current["status"] not in UNFINISHED
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
        checks_complete = (
            result.get("missing_count") == 0 if current["intent"] == "complete_purposes" else True
        )
        completed = checks_complete and not current["unresolved_json"].get("pending")
        status = "completed" if completed else "waiting_user"
        unresolved = {
            **current["unresolved_json"],
            "missing_count": result.get("missing_count"),
            "needs_refresh": False,
        }
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
        _revision(conn, updated, "checked", [{"source_id": result_source_id}], source_seq)
        if completed:
            _advance_after_close(conn, current, source_seq)
            _revoke_task_plans(conn, current)
            _close_pending(conn, current, "resolved")
        else:
            conn.execute(
                "UPDATE task_slots SET revision=revision+1,authority_source_seq=greatest(authority_source_seq,%s) "
                "WHERE user_id=%s AND company_id=%s",
                (source_seq, auth.user_id, task["company_id"]),
            )
        epoch = bump_task_memory(conn, auth, task["company_id"], result_source_id)
        return {**updated, "memory_epoch": epoch}
