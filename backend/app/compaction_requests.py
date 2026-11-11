"""Durable control requests for manual session compaction."""

from app.admission import check
from app.errors import AppError
from app.events import emit
from app.identity import owned
from app.sources import add_source, advance_progress, assess, next_sequence
from app.values import digest, jsonb, safe_json, uid


def model_fingerprint(settings):
    return digest(
        [
            settings.model_base_url,
            settings.model_id,
            settings.model_context_limit,
            settings.model_capacity_source,
            settings.session_context_window,
            settings.input_budget,
            settings.output_reserve,
            settings.model_extra_body,
        ]
    )


def accept(db, auth, session_id, request_id, settings):
    fingerprint = digest(["compact", session_id])
    with db.transaction() as conn:
        conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
        session = owned(conn, "sessions", "session_id", session_id, auth, lock=True)
        if session["archived_at"]:
            raise AppError("SESSION_ARCHIVED", "请先取消归档，再压缩此会话。", 409)
        existing = conn.execute(
            "SELECT * FROM turns WHERE user_id=%s AND client_request_id=%s",
            (auth.user_id, request_id),
        ).fetchone()
        if existing:
            if existing["request_hash"] != fingerprint or existing["request_kind"] != "compact":
                raise AppError("IDEMPOTENCY_CONFLICT", "相同请求编号的内容不同。", 409)
            return safe_json(existing)
        existing = conn.execute(
            "SELECT * FROM turns WHERE session_id=%s AND user_id=%s "
            "AND request_kind='compact' AND status IN ('queued','running') ORDER BY source_seq LIMIT 1",
            (session_id, auth.user_id),
        ).fetchone()
        if existing:
            return safe_json(existing)
        check(conn, auth.user_id, settings)
        sequence, turn_id = next_sequence(conn, auth), uid()
        conn.execute(
            "INSERT INTO turns(turn_id,session_id,user_id,client_request_id,request_hash,"
            "source_seq,text,request_kind) VALUES(%s,%s,%s,%s,%s,%s,'/compact','compact')",
            (turn_id, session_id, auth.user_id, request_id, fingerprint, sequence),
        )
        source = add_source(
            conn,
            auth,
            {"kind": "compact"},
            role="control",
            kind="compact",
            turn_id=turn_id,
            source_seq=sequence,
            companies=[session["selected_company_id"]] if session["selected_company_id"] else [],
        )
        conn.execute(
            "INSERT INTO work_items(work_id,user_id,source_id,kind) VALUES(%s,%s,%s,'turn')",
            (uid(), auth.user_id, source["source_id"]),
        )
        conn.execute(
            "INSERT INTO compaction_requests(turn_id,user_id,session_id,selected_company_id,model_fingerprint) "
            "VALUES(%s,%s,%s,%s,%s)",
            (
                turn_id,
                auth.user_id,
                session_id,
                session["selected_company_id"],
                model_fingerprint(settings),
            ),
        )
        # This control source is never a user preference or conversation message.
        assess(conn, str(source["source_id"]), "no_change")
        advance_progress(conn, auth)
        emit(conn, turn_id, None, "accepted", {"turn_id": turn_id, "status": "queued"})
        return safe_json(
            conn.execute("SELECT * FROM turns WHERE turn_id=%s", (turn_id,)).fetchone()
        )


def read(db, auth, session_id, operation_id):
    with db.transaction(readonly=True) as conn:
        owned(conn, "sessions", "session_id", session_id, auth)
        row = conn.execute(
            "SELECT t.turn_id,t.session_id,t.status,t.error,t.created_at,t.updated_at,c.result "
            "FROM turns t JOIN compaction_requests c USING(turn_id) "
            "WHERE t.turn_id=%s AND t.user_id=%s AND t.session_id=%s",
            (operation_id, auth.user_id, session_id),
        ).fetchone()
        if not row:
            raise AppError("RESOURCE_NOT_FOUND", "压缩任务不存在或无权访问。", 404)
        owned(conn, "turns", "turn_id", operation_id, auth)
        return safe_json(row)


def finish_in(conn, execution, result):
    """Publish the control receipt in the same transaction as the summary."""
    conn.execute(
        "UPDATE compaction_requests SET result=%s WHERE turn_id=%s",
        (jsonb(result), execution.turn_id),
    )
    conn.execute(
        "UPDATE turns SET status='completed',updated_at=now() WHERE turn_id=%s",
        (execution.turn_id,),
    )
    conn.execute(
        "UPDATE runs SET state='completed',finished_at=now() WHERE run_id=%s",
        (execution.run_id,),
    )
    conn.execute(
        "UPDATE work_items SET state='done',lease_until=NULL WHERE work_id=%s AND fencing_token=%s",
        (execution.work_id, execution.fencing_token),
    )
    emit(conn, execution.turn_id, execution.run_id, "final", {"status": "completed", **result})
