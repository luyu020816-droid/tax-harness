import re

from app.errors import AppError
from app.execution import Execution
from app.identity import Identity, assert_access, owned
from app.memory_changes import assert_newer, begin_change, finish_change, suppress_old_history
from app.values import jsonb, uid


def list_preferences(db, auth: Identity, company_id: str | None = None) -> list[dict]:
    with db.transaction(readonly=True) as conn:
        if company_id:
            assert_access(conn, auth, company_id)
        rows = conn.execute(
            "SELECT * FROM preferences WHERE user_id=%s AND status='active' "
            "AND (scope_key='@user' OR scope_key=%s) ORDER BY scope_key,stable_key",
            (auth.user_id, company_id),
        ).fetchall()
        return [
            {**row, "source_refs": [{"source_id": ref["source_id"]} for ref in row["source_refs"]]}
            for row in rows
        ]


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
            epoch = conn.execute(
                "SELECT memory_epoch FROM users WHERE user_id=%s", (auth.user_id,)
            ).fetchone()["memory_epoch"]
            return {
                **current,
                "source_refs": [{"source_id": ref["source_id"]} for ref in current["source_refs"]],
                "memory_epoch": epoch,
                "replayed": True,
            }
        if not old and key:
            old = conn.execute(
                "SELECT * FROM preferences WHERE user_id=%s AND scope_key=%s AND stable_key=%s FOR UPDATE",
                (auth.user_id, scope, key),
            ).fetchone()
        if old:
            old = conn.execute(
                "SELECT * FROM preferences WHERE preference_id=%s FOR UPDATE",
                (old["preference_id"],),
            ).fetchone()
            assert_newer(change["source_seq"], old)
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
                obsolete_value=old["value"],
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
                    key,
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
