import re

from app import preference_access
from app.errors import AppError
from app.execution import Execution
from app.identity import Identity, assert_access, owned
from app.memory_changes import assert_newer, begin_change, finish_change, suppress_old_history
from app.preference_rules import (
    applies_to_request,
    preference_evidence,
    preference_identity,
    report_scope,
    value_identity,
)
from app.sources import read_source
from app.values import jsonb, uid


def history(db, auth, preference_id, *, before_revision=None, limit=20):
    """Explicit audit view, including tombstones; never used for model recall."""
    from app.values import safe_json

    with db.transaction(readonly=True) as conn:
        pref = owned(conn, "preferences", "preference_id", preference_id, auth)
        if pref["scope_key"] != "@user":
            assert_access(conn, auth, pref["scope_key"])
        rows = conn.execute(
            "SELECT * FROM preference_revisions WHERE preference_id=%s "
            "AND (%s::integer IS NULL OR revision<%s) ORDER BY revision DESC LIMIT %s",
            (preference_id, before_revision, before_revision, limit + 1),
        ).fetchall()
        for row in rows[:limit]:
            try:
                preference_access.sources_in(
                    conn,
                    auth,
                    {**row, "scope_key": pref["scope_key"]},
                    pref["scope_key"] if pref["scope_key"] != "@user" else None,
                )
            except AppError as exc:
                if not preference_access.invalid_source(exc):
                    raise
                row["value"], row["source_refs"] = None, []
                row["redacted"] = True
        return safe_json(
            {
                "preference_id": preference_id,
                "current_status": pref["status"],
                "revisions": rows[:limit],
                "next_revision": rows[limit - 1]["revision"] if len(rows) > limit else None,
            }
        )


def visible_in(
    conn, auth, company_id, *, turn_id=None, binding=None, management=False,
    request_text=None, excluded=None,
):
    if company_id:
        assert_access(conn, auth, company_id)
    if not preference_access.can_read(conn, turn_id=turn_id, binding=binding):
        return []
    rows = conn.execute(
        "SELECT * FROM preferences WHERE user_id=%s AND status='active' "
        "AND (scope_key='@user' OR scope_key=%s) ORDER BY scope_key,stable_key",
        (auth.user_id, company_id),
    ).fetchall()
    catalogue = preference_access.company_names(conn) if rows else {}
    by_identity = {}
    for preference in rows:
        try:
            evidence = preference_access.sources_in(
                conn, auth, preference, company_id, catalogue=catalogue
            )
        except AppError as exc:
            if not preference_access.invalid_source(exc):
                raise
            continue
        report = report_scope(preference["value"])
        if not applies_to_request(report, request_text):
            if excluded is not None:
                excluded.append({
                    "id": str(preference["preference_id"]),
                    "reason": "report_scope_not_requested",
                })
            continue
        identity = (
            preference["scope_key"],
            preference_identity(preference["stable_key"], preference["value"]),
        )
        by_identity.setdefault(identity, []).append((preference, evidence))
    visible = []
    for group in by_identity.values():
        if len(group) == 1:
            visible.extend(group)
        elif management:
            visible.extend(({**row, "needs_resolution": True}, evidence) for row, evidence in group)
        elif len({value_identity(row["value"]) for row, _ in group}) == 1:
            visible.append(max(group, key=lambda item: item[0]["authority_source_seq"]))
    return visible


def list_preferences(
    db,
    auth: Identity,
    company_id: str | None = None,
    *,
    turn_id=None,
    management=False,
) -> list[dict]:
    with db.transaction(readonly=True) as conn:
        return [
            {**row, "source_refs": [{"source_id": ref["source_id"]} for ref in row["source_refs"]]}
            for row, _ in visible_in(conn, auth, company_id, turn_id=turn_id, management=management)
        ]


def tool_company(db, execution, scope):
    if not scope or scope == "@user":
        return None
    with db.transaction(readonly=True) as conn:
        company = owned(conn, "sessions", "session_id", execution.session_id, execution.auth)
        if company["selected_company_id"] != scope:
            raise preference_access.scope_error()
    return scope


def change_preference(
    db,
    auth: Identity,
    *,
    action: str,
    scope="@user",
    key=None,
    value=None,
    target_id=None,
    expected_revision=None,
    request_id: str,
    source_refs=None,
    execution: Execution | None = None,
) -> dict:
    if action not in ("remember", "update", "delete"):
        raise AppError("INVALID_ARGUMENT", "偏好操作须为 remember、update 或 delete。")
    if action != "delete" and (
        not isinstance(value, str) or not value.strip() or len(value) > 1000
    ):
        raise AppError("INVALID_ARGUMENT", "偏好须为非空文本，最多 1000 字符。")
    if key and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
        raise AppError("INVALID_ARGUMENT", "偏好键须为最多 64 位的小写字母、数字与下划线。")
    body = {
        "scope": scope,
        "key": key,
        "value": value,
        "target_id": target_id,
        "expected_revision": expected_revision,
    }
    with db.transaction() as conn:
        if scope != "@user":
            assert_access(conn, auth, scope)
        old = owned(conn, "preferences", "preference_id", target_id, auth) if target_id else None
        if old and old["scope_key"] != scope:
            raise AppError("RESOURCE_NOT_FOUND", "偏好不属于该作用范围。", 404)
        existing, change = begin_change(
            conn, auth, "preference", action, body, request_id, execution, source_refs or []
        )
        if change is None:
            current = owned(
                conn, "preferences", "preference_id", str(existing["preference_id"]), auth
            )
            if current["status"] == "active":
                preference_access.sources_in(
                    conn, auth, current, scope if scope != "@user" else None
                )
            epoch = conn.execute(
                "SELECT memory_epoch FROM users WHERE user_id=%s", (auth.user_id,)
            ).fetchone()["memory_epoch"]
            return {
                **current,
                "source_refs": [{"source_id": ref["source_id"]} for ref in current["source_refs"]],
                "memory_epoch": epoch,
                "replayed": True,
            }
        signature = None
        correction = False
        if execution:
            source = read_source(conn, auth, execution.source_id)
            quotes = [
                ref["quote"]
                for ref in change["refs"]
                if str(ref["source_id"]) == execution.source_id
            ]
            catalogue = preference_access.company_names(conn)
            signature, correction, literal = preference_evidence(
                source["content"],
                quotes,
                action,
                value,
                company_names=catalogue.get(scope, ()),
            )
            preference_access.authorize_scope(source, scope, value, signature, catalogue)
            if signature and signature[1] != "default":
                value = literal
            if scope == "@user" and signature:
                change["refs"] = preference_access.personal_authorization(
                    conn, auth, source, change, value, signature
                )
        elif scope == "@user" and value is not None:
            preference_access.authorize_scope(
                {"content": value, "scope_kind": "user_private"},
                scope,
                value,
                None,
                preference_access.company_names(conn),
            )
        identity = preference_identity(
            key or (old["stable_key"] if old else None), value, signature
        )
        candidates = conn.execute(
            "SELECT * FROM preferences WHERE user_id=%s AND scope_key=%s AND status='active' "
            "ORDER BY preference_id FOR UPDATE",
            (auth.user_id, scope),
        ).fetchall()
        if old:
            if (
                execution
                and action != "delete"
                and identity != preference_identity(old["stable_key"], old["value"])
            ):
                raise AppError("NEEDS_CLARIFICATION", "更正内容与选中的偏好不匹配。", 422)
            identity = preference_identity(old["stable_key"], old["value"])
        matching = [
            row
            for row in candidates
            if preference_identity(row["stable_key"], row["value"]) == identity
        ]
        if (len(matching) > 1 and (execution or not target_id)) or (
            execution and action == "delete" and len(candidates) != 1
        ):
            raise AppError(
                "PREFERENCE_CONFLICT", "请在记忆管理里选择并处理要更正或删除的偏好。", 409
            )
        if not old and identity:
            old = (
                matching[0]
                if matching
                else conn.execute(
                    "SELECT * FROM preferences WHERE user_id=%s AND scope_key=%s AND stable_key=%s "
                    "FOR UPDATE",
                    (auth.user_id, scope, identity),
                ).fetchone()
            )
        if not old and key and key != identity:
            old = conn.execute(
                "SELECT * FROM preferences WHERE user_id=%s AND scope_key=%s AND stable_key=%s "
                "FOR UPDATE",
                (auth.user_id, scope, key),
            ).fetchone()
        if old:
            old = conn.execute(
                "SELECT * FROM preferences WHERE preference_id=%s FOR UPDATE",
                (old["preference_id"],),
            ).fetchone()
            assert_newer(change["source_seq"], old)
            if (
                execution
                and correction
                and not target_id
                and expected_revision is None
                and len(matching) == 1
            ):
                expected_revision = old["revision"]
            if expected_revision != old["revision"]:
                raise AppError("VERSION_CONFLICT", "偏好版本已变化，请先查看当前值。", 409)
            if old["status"] == "deleted" and action != "remember":
                raise AppError(
                    "SOURCE_INVALIDATED", "该偏好已删除；重新建立需要明确的记住指令。", 409
                )
            suppress_old_history(
                conn,
                auth,
                old["source_refs"],
                kind="preference",
                target_id=old["preference_id"],
                before_seq=change["source_seq"],
            )
            result = conn.execute(
                "UPDATE preferences SET value=%s,status=%s,revision=revision+1,"
                "generation=generation+%s,authority_source_seq=%s,source_refs=%s,"
                "deleted_at=CASE WHEN %s='delete' THEN now() ELSE NULL END WHERE preference_id=%s RETURNING *",
                (
                    None if action == "delete" else value,
                    "deleted" if action == "delete" else "active",
                    1 if action == "delete" or old["status"] == "deleted" else 0,
                    change["source_seq"],
                    jsonb(change["refs"]),
                    action,
                    old["preference_id"],
                ),
            ).fetchone()
        else:
            if action != "remember" or not key:
                raise AppError("RESOURCE_NOT_FOUND", "没有可修改的偏好。", 404)
            result = conn.execute(
                "INSERT INTO preferences(preference_id,user_id,scope_key,stable_key,value,status,authority_source_seq,source_refs) "
                "VALUES(%s,%s,%s,%s,%s,'active',%s,%s) RETURNING *",
                (
                    uid(),
                    auth.user_id,
                    scope,
                    identity,
                    value,
                    change["source_seq"],
                    jsonb(change["refs"]),
                ),
            ).fetchone()
        conn.execute(
            "INSERT INTO preference_revisions VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                result["preference_id"],
                result["generation"],
                result["revision"],
                result["value"],
                result["status"],
                jsonb(result["source_refs"]),
                change["source_seq"],
            ),
        )
        for ref in change["refs"]:
            conn.execute(
                "INSERT INTO memory_dependencies(dependent_type,dependent_id,dependent_revision,source_type,source_id,source_revision,source_generation) "
                "VALUES('preference',%s,%s,'source',%s,1,%s)",
                (
                    result["preference_id"],
                    result["revision"],
                    ref["source_id"],
                    result["generation"],
                ),
            )
        return finish_change(conn, auth, change, result)
