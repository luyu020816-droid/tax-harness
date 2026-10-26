from app.errors import AppError
from app.events import emit
from app.identity import Identity, assert_access, owned
from app.sources import add_source, advance_progress, assess, next_sequence
from app.values import digest, jsonb, safe_json, uid


def mentioned_companies(conn, text: str) -> list[str]:
    return [
        row["company_id"]
        for row in conn.execute(
            "SELECT company_id,aliases FROM companies ORDER BY company_id"
        ).fetchall()
        if any(alias and alias in text for alias in row["aliases"])
    ]


def accept_turn(db, auth: Identity, session_id: str, client_request_id: str, text: str) -> dict:
    if not text.strip() or len(text) > 16000 or len(client_request_id) > 200:
        raise AppError("INVALID_ARGUMENT", "消息不能为空且最多 16000 字符，请求编号最多 200 字符。")
    fingerprint = digest([session_id, text])
    with db.transaction() as conn:
        conn.execute("SELECT * FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
        owned(conn, "sessions", "session_id", session_id, auth)
        previous = conn.execute(
            "SELECT * FROM turns WHERE user_id=%s AND client_request_id=%s",
            (auth.user_id, client_request_id),
        ).fetchone()
        if previous:
            if previous["request_hash"] != fingerprint:
                raise AppError(
                    "IDEMPOTENCY_CONFLICT", "相同请求编号的内容不同，已拒绝重复受理。", 409
                )
            return safe_json(previous)
        sequence = next_sequence(conn, auth)
        turn_id = uid()
        companies = mentioned_companies(conn, text)
        # Unauthorised company names can be part of a question; do not attach private data.
        accessible = []
        for company in companies:
            try:
                assert_access(conn, auth, company)
                accessible.append(company)
            except AppError:
                pass
        conn.execute(
            "INSERT INTO turns(turn_id,session_id,user_id,client_request_id,request_hash,source_seq,text) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (turn_id, session_id, auth.user_id, client_request_id, fingerprint, sequence, text),
        )
        source = add_source(
            conn, auth, text, turn_id=turn_id, source_seq=sequence, companies=accessible
        )
        conn.execute(
            "INSERT INTO work_items(work_id,user_id,source_id,kind) VALUES(%s,%s,%s,'turn')",
            (uid(), auth.user_id, source["source_id"]),
        )
        emit(conn, turn_id, None, "accepted", {"turn_id": turn_id, "status": "queued"})
        return safe_json(
            conn.execute("SELECT * FROM turns WHERE turn_id=%s", (turn_id,)).fetchone()
        )


def read_turn(db, auth: Identity, turn_id: str) -> dict:
    with db.transaction(readonly=True) as conn:
        turn = owned(conn, "turns", "turn_id", turn_id, auth)
        _assert_turn_access(conn, auth, turn_id)
        runs = conn.execute(
            "SELECT run_id,generation,state,started_at,finished_at FROM runs WHERE turn_id=%s ORDER BY generation",
            (turn_id,),
        ).fetchall()
        operations = conn.execute(
            "SELECT o.* FROM operations o JOIN sources s ON s.source_id=o.source_id WHERE s.turn_id=%s AND o.user_id=%s ORDER BY o.committed_at",
            (turn_id, auth.user_id),
        ).fetchall()
        selected = conn.execute(
            "SELECT selected_company_id FROM sessions WHERE session_id=%s", (turn["session_id"],)
        ).fetchone()["selected_company_id"]
        return safe_json(
            {**turn, "runs": runs, "operations": operations, "selected_company_id": selected}
        )


def list_pending(db, auth: Identity) -> list[dict]:
    with db.transaction(readonly=True) as conn:
        return safe_json(
            conn.execute(
                "SELECT turn_id,source_seq,pending_clarification,updated_at FROM turns WHERE user_id=%s AND status='waiting_user' AND pending_clarification->>'state'='waiting_user' ORDER BY source_seq DESC",
                (auth.user_id,),
            ).fetchall()
        )


def cancel_turn(
    db,
    auth: Identity,
    turn_id: str,
    request_id: str,
    expected_revision=None,
    *,
    delete_pending=False,
) -> dict:
    fingerprint = digest([turn_id, expected_revision, delete_pending])
    with db.transaction() as conn:
        conn.execute("SELECT * FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
        turn = owned(conn, "turns", "turn_id", turn_id, auth, lock=True)
        existing = conn.execute(
            "SELECT * FROM memory_change_ops WHERE user_id=%s AND request_id=%s",
            (auth.user_id, request_id),
        ).fetchone()
        if existing:
            if existing["request_hash"] != fingerprint:
                raise AppError("IDEMPOTENCY_CONFLICT", "取消请求内容发生冲突。", 409)
            return existing["result_ref"]
        pending = turn["pending_clarification"]
        if pending and turn["status"] == "waiting_user":
            if pending["revision"] != expected_revision:
                raise AppError("VERSION_CONFLICT", "待澄清事项已变化，请重新查看。", 409)
            if pending.get("task_id"):
                raise AppError(
                    "TASK_CONTROL_REQUIRED", "该追问属于正式任务，请在任务管理中取消或删除。", 409
                )
        elif delete_pending:
            raise AppError("VERSION_CONFLICT", "该事项已经结束，不能再次删除关联。", 409)
        if turn["status"] in (
            "completed",
            "cancelled",
            "failed",
            "interrupted",
            "resolved",
            "deleted",
        ):
            return {"turn_id": turn_id, "status": turn["status"], "already_finished": True}
        sequence = next_sequence(conn, auth)
        source = add_source(
            conn,
            auth,
            {"action": "delete_pending" if delete_pending else "cancel", "turn_id": turn_id},
            role="user",
            kind="user_action",
            source_seq=sequence,
        )
        status = "deleted" if delete_pending else "cancelled"
        if pending:
            pending = {
                **pending,
                "state": status,
                "generation": pending["generation"] + 1,
                "revision": pending["revision"] + 1,
                "authority_source_seq": sequence,
            }
        conn.execute(
            "UPDATE turns SET status=%s,pending_clarification=%s,active_run_generation=active_run_generation+1,updated_at=now() WHERE turn_id=%s",
            (status, jsonb(pending) if pending else None, turn_id),
        )
        conn.execute(
            "UPDATE work_items w SET state='cancelled',fencing_token=fencing_token+1,lease_until=NULL FROM sources s WHERE w.source_id=s.source_id AND s.turn_id=%s",
            (turn_id,),
        )
        conn.execute(
            "UPDATE runs SET state='cancelled',finished_at=now() WHERE turn_id=%s AND finished_at IS NULL",
            (turn_id,),
        )
        epoch = conn.execute(
            "UPDATE users SET memory_epoch=memory_epoch+1 WHERE user_id=%s RETURNING memory_epoch",
            (auth.user_id,),
        ).fetchone()["memory_epoch"]
        if pending:
            conn.execute(
                "UPDATE sources SET recall_state='suppressed' WHERE turn_id=%s AND user_id=%s",
                (turn_id, auth.user_id),
            )
            conn.execute(
                "UPDATE summaries SET status='invalidated' WHERE user_id=%s AND status='active'",
                (auth.user_id,),
            )
        result = {"turn_id": turn_id, "status": status, "memory_epoch": epoch}
        emit(conn, turn_id, None, "cancelled", result)
        conn.execute(
            "INSERT INTO memory_change_ops(change_id,user_id,request_id,request_hash,target_type,target_id,action,state,result_ref) VALUES(%s,%s,%s,%s,'pending',%s,%s,'committed',%s)",
            (uid(), auth.user_id, request_id, fingerprint, turn_id, status, jsonb(result)),
        )
        assess(conn, str(source["source_id"]), "accepted", [result])
        advance_progress(conn, auth)
        return result


def list_sessions(db, auth: Identity) -> list[dict]:
    with db.transaction(readonly=True) as conn:
        return safe_json(
            conn.execute(
                "SELECT s.*,(SELECT t.text FROM turns t WHERE t.session_id=s.session_id ORDER BY source_seq LIMIT 1) AS title FROM sessions s WHERE user_id=%s ORDER BY updated_at DESC LIMIT 100",
                (auth.user_id,),
            ).fetchall()
        )


def session_messages(db, auth: Identity, session_id: str, before_seq=None) -> dict:
    with db.transaction(readonly=True) as conn:
        owned(conn, "sessions", "session_id", session_id, auth)
        rows = conn.execute(
            "SELECT * FROM turns WHERE session_id=%s AND user_id=%s AND (%s::bigint IS NULL OR source_seq<%s) ORDER BY source_seq DESC LIMIT 21",
            (session_id, auth.user_id, before_seq, before_seq),
        ).fetchall()
        for row in rows[:20]:
            _assert_turn_access(conn, auth, str(row["turn_id"]))
        return safe_json(
            {
                "turns": list(reversed(rows[:20])),
                "has_more": len(rows) > 20,
                "next_cursor": rows[19]["source_seq"] if len(rows) > 20 else None,
            }
        )


def _assert_turn_access(conn, auth, turn_id):
    scopes = conn.execute(
        "SELECT DISTINCT ss.company_id FROM source_scopes ss JOIN sources s USING(source_id) WHERE s.turn_id=%s",
        (turn_id,),
    ).fetchall()
    for scope in scopes:
        assert_access(conn, auth, scope["company_id"])
