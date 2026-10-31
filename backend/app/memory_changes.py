from app.errors import AppError
from app.execution import Execution, guard
from app.identity import Identity
from app.sources import add_source, advance_progress, assess, next_sequence, validate_refs
from app.values import digest, jsonb, uid


def begin_change(
    conn,
    auth: Identity,
    kind: str,
    action: str,
    body: dict,
    request_id: str,
    execution: Execution | None,
    refs: list[dict],
) -> tuple:
    conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
    fingerprint = digest([kind, action, body])
    existing = conn.execute(
        "SELECT * FROM memory_change_ops WHERE user_id=%s AND request_id=%s",
        (auth.user_id, request_id),
    ).fetchone()
    if existing:
        if existing["request_hash"] != fingerprint:
            raise AppError("IDEMPOTENCY_CONFLICT", "同一操作编号不能用于不同内容。", 409)
        return {**existing["result_ref"], "replayed": True}, None
    if execution:
        guard(conn, execution)
        validate_refs(
            conn,
            auth,
            refs,
            company_id=body.get("company_id")
            or (body.get("scope") if body.get("scope") != "@user" else None),
        )
        if execution.source_id not in [str(r["source_id"]) for r in refs]:
            raise AppError("NEEDS_CLARIFICATION", "需要本轮用户明确指令，不能仅凭旧记忆修改。", 422)
        source_id, sequence = execution.source_id, execution.source_seq
    else:
        sequence = next_sequence(conn, auth)
        scope = body.get("company_id") or body.get("scope")
        source = add_source(
            conn,
            auth,
            {"action": action, **body},
            role="user",
            kind="user_action",
            source_seq=sequence,
            companies=[scope] if scope and scope != "@user" else [],
        )
        source_id = str(source["source_id"])
        refs = [{"source_id": source_id, "quote": source["content"]}]
    change_id = uid()
    conn.execute(
        "INSERT INTO memory_change_ops(change_id,user_id,request_id,request_hash,target_type,target_id,expected_revision,action,state) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'prepared')",
        (
            change_id,
            auth.user_id,
            request_id,
            fingerprint,
            kind,
            body.get("target_id"),
            body.get("expected_revision"),
            action,
        ),
    )
    return None, {
        "change_id": change_id,
        "source_id": source_id,
        "source_seq": sequence,
        "refs": refs,
    }


def finish_change(conn, auth: Identity, change: dict, result: dict) -> dict:
    epoch = conn.execute(
        "UPDATE users SET memory_epoch=memory_epoch+1 WHERE user_id=%s RETURNING memory_epoch",
        (auth.user_id,),
    ).fetchone()["memory_epoch"]
    conn.execute(
        "UPDATE summaries SET status='invalidated' WHERE user_id=%s AND status='active'",
        (auth.user_id,),
    )
    result = {**result, "memory_epoch": epoch, "replayed": False}
    conn.execute(
        "UPDATE memory_change_ops SET state='committed',result_ref=%s WHERE change_id=%s",
        (jsonb(result), change["change_id"]),
    )
    assess(conn, change["source_id"], "accepted", [result])
    advance_progress(conn, auth)
    return result


def suppress_old_history(
    conn,
    auth: Identity,
    refs: list[dict],
    *,
    kind=None,
    target_id=None,
    before_seq=None,
) -> None:
    ids = [str(r["source_id"]) for r in refs]
    if ids:
        # Suppress automatic historical use, while keeping immutable evidence for other valid facts.
        conn.execute(
            "UPDATE sources SET recall_state='suppressed' WHERE user_id=%s AND source_id=ANY(%s::uuid[])",
            (auth.user_id, ids),
        )
    # Invalidate provenance and actual dependents, never all messages containing
    # the same words. A preference value may also be an unrelated business name
    # in another company; substring equality is not a dependency relationship.
    if kind and target_id and before_seq:
        conn.execute(
            "UPDATE sources SET recall_state='suppressed' WHERE user_id=%s AND role<>'user' AND source_seq<%s "
            "AND turn_id IN (SELECT r.turn_id FROM runs r JOIN context_snapshots c USING(run_id) "
            "WHERE c.manifest->'dependencies' @> %s::jsonb)",
            (auth.user_id, before_seq, jsonb([{"kind": kind, "id": str(target_id)}])),
        )


def assert_newer(sequence: int, current: dict) -> None:
    if sequence <= current["authority_source_seq"]:
        raise AppError("SOURCE_INVALIDATED", "旧来源不能覆盖较新的更正、删除或任务状态。", 409)
