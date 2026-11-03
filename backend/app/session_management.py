"""Conversation organization only; never deletes documents or business facts."""

from app.errors import AppError
from app.identity import owned
from app.resource_access import SESSION_TITLE, SESSION_VISIBLE
from app.values import safe_json


def history(db, auth, archived=False, offset=0):
    with db.transaction(readonly=True) as conn:
        rows = conn.execute(
            "SELECT s.*, "
            + SESSION_TITLE
            + " AS title FROM sessions s WHERE "
            + SESSION_VISIBLE
            + " AND (s.archived_at IS NOT NULL)=%(archived)s ORDER BY s.pinned_at DESC NULLS LAST,s.updated_at DESC,s.session_id DESC LIMIT 51 OFFSET %(offset)s",
            {"user": auth.user_id, "archived": archived, "offset": offset},
        ).fetchall()
        return safe_json(
            {
                "sessions": rows[:50],
                "has_more": len(rows) > 50,
                "next_offset": offset + 50 if len(rows) > 50 else None,
            }
        )


def organize(db, auth, session_id, action, expected_revision):
    with db.transaction() as conn:
        conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
        row = owned(conn, "sessions", "session_id", session_id, auth, lock=True)
        if row["organization_revision"] != expected_revision:
            raise AppError("VERSION_CONFLICT", "会话已更新，请刷新后重试。", 409)
        if (
            action in ("archive", "delete")
            and conn.execute(
                "SELECT 1 FROM turns WHERE session_id=%s AND status IN ('queued','running') LIMIT 1",
                (session_id,),
            ).fetchone()
        ):
            raise AppError("SESSION_BUSY", "此会话仍有任务运行，请停止任务后再操作。", 409)
        column = {
            "pin": "pinned_at",
            "unpin": "pinned_at",
            "archive": "archived_at",
            "unarchive": "archived_at",
            "delete": "deleted_at",
        }.get(action)
        if not column:
            raise AppError("INVALID_ARGUMENT", "不支持的会话操作。", 422)
        from psycopg import sql

        value = "NULL" if action in ("unpin", "unarchive") else "clock_timestamp()"
        conn.execute(
            sql.SQL(
                "UPDATE sessions SET {}={},organization_revision=organization_revision+1 WHERE session_id=%s"
            ).format(sql.Identifier(column), sql.SQL(value)),
            (session_id,),
        )
        if action == "delete":
            conn.execute(
                "UPDATE summaries SET status='invalidated' WHERE session_id=%s AND status='active'",
                (session_id,),
            )
        return safe_json(
            conn.execute("SELECT * FROM sessions WHERE session_id=%s", (session_id,)).fetchone()
        )
