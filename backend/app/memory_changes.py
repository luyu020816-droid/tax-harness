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
    if execution:
        from app.agent_authority import LOCK_ID

        conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (LOCK_ID,))
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
        "memory_scope": (body.get("company_id") or body.get("scope"))
        if kind in ("preference", "task") else None,
    }


def bump_memory(conn, auth, scope=None):
    scopes = [scope] if isinstance(scope, str) else list(scope or ())
    epoch = conn.execute(
        "UPDATE users SET memory_epoch=memory_epoch+1 WHERE user_id=%s RETURNING memory_epoch",
        (auth.user_id,),
    ).fetchone()["memory_epoch"]
    conn.execute(
        "UPDATE summaries SET status='invalidated' WHERE user_id=%s AND status='active' "
        "AND (%s OR scope=ANY(%s::text[]))",
        (auth.user_id, not scopes or "@user" in scopes, scopes),
    )
    return epoch


def bump_task_memory(conn, auth, scope, source_id, *, event="1"):
    epoch = bump_memory(conn, auth, scope)
    if scope:
        conn.execute(
            "INSERT INTO source_assessments(source_id,revision,purpose,processor_version,state,result_refs) "
            "SELECT source_id,revision,'task_memory_scope',%s,'accepted',%s FROM sources "
            "WHERE source_id=%s AND user_id=%s ON CONFLICT DO NOTHING",
            (event, jsonb({"memory_epoch": epoch, "memory_scope": scope}), source_id, auth.user_id),
        )
    return epoch


def compatible_epoch(conn, auth, companies, before, after):
    if before == after:
        return True
    if not companies or before > after:
        return False
    # Only fully accounted changes can be ignored. Unclassified changes retain
    # the global fence, including account grants and legacy operations.
    blocked = sorted({"@user", *companies})
    row = conn.execute(
        "WITH changes AS ("
        "SELECT result_ref AS item FROM memory_change_ops WHERE user_id=%s AND state='committed' "
        "AND result_ref ? 'memory_scope' "
        "UNION ALL SELECT a.result_refs FROM source_assessments a JOIN sources s USING(source_id) "
        "WHERE s.user_id=%s AND a.purpose='task_memory_scope'"
        ") SELECT count(DISTINCT (item->>'memory_epoch')::bigint)=%s "
        "AND coalesce(bool_and(CASE jsonb_typeof(item->'memory_scope') "
        "WHEN 'string' THEN item->>'memory_scope'<>ALL(%s::text[]) "
        "WHEN 'array' THEN jsonb_array_length(item->'memory_scope')>0 "
        "AND NOT (item->'memory_scope' ?| %s::text[]) ELSE false END),false) AS compatible "
        "FROM changes WHERE (item->>'memory_epoch')::bigint>%s AND (item->>'memory_epoch')::bigint<=%s",
        (auth.user_id, auth.user_id, after - before, blocked, blocked, before, after),
    ).fetchone()
    return row["compatible"]


def finish_change(conn, auth: Identity, change: dict, result: dict, *, invalidate_memory=True) -> dict:
    if invalidate_memory:
        scope = change.get("memory_scope")
        epoch = bump_memory(conn, auth, scope)
    else:
        epoch = conn.execute(
            "SELECT memory_epoch FROM users WHERE user_id=%s", (auth.user_id,)
        ).fetchone()["memory_epoch"]
    result = {**result, "memory_epoch": epoch, "replayed": False}
    audit = {**result, "memory_scope": scope} if invalidate_memory and scope else result
    conn.execute(
        "UPDATE memory_change_ops SET state='committed',result_ref=%s WHERE change_id=%s",
        (jsonb(audit), change["change_id"]),
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
    ids = [
        str(ref[key])
        for ref in refs for key in ("source_id", "origin_source_id") if ref.get(key)
    ]
    if ids:
        # Suppress automatic historical use, while keeping immutable evidence for other valid facts.
        conn.execute(
            "UPDATE sources SET recall_state='suppressed' WHERE user_id=%s AND recall_state='active' "
            "AND source_id=ANY(%s::uuid[])",
            (auth.user_id, ids),
        )
    # Invalidate provenance and actual dependents, never all messages containing
    # the same words. A preference value may also be an unrelated business name
    # in another company; substring equality is not a dependency relationship.
    if kind and target_id and before_seq:
        target = jsonb([{"kind": kind, "id": str(target_id)}])
        conn.execute(
            "UPDATE sources s SET recall_state='suppressed' WHERE s.user_id=%s AND s.role<>'user' "
            "AND s.recall_state='active' AND s.source_seq<%s "
            "AND coalesce((SELECT a.result_refs->'dependencies' @> %s::jsonb "
            "FROM source_assessments a WHERE a.source_id=s.source_id AND a.revision=s.revision "
            "AND a.purpose='answer_preferences' AND a.processor_version='1' AND %s='preference'), "
            "s.turn_id IN (SELECT r.turn_id FROM runs r JOIN context_snapshots c USING(run_id) "
            "WHERE c.manifest->'dependencies' @> %s::jsonb))",
            (auth.user_id, before_seq, target, kind, target),
        )


def assert_newer(sequence: int, current: dict) -> None:
    if sequence <= current["authority_source_seq"]:
        raise AppError("SOURCE_INVALIDATED", "旧来源不能覆盖较新的更正、删除或任务状态。", 409)
