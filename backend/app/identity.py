import hashlib
import secrets
from dataclasses import dataclass, field

from app.errors import AppError
from app.events import interrupt_session
from app.values import uid


@dataclass(frozen=True)
class Identity:
    user_id: str
    token_hash: str | None = field(default=None, repr=False, compare=False)


def assert_access(conn, auth: Identity, company_id: str, *, write: bool = False) -> dict:
    row = conn.execute(
        "SELECT g.* FROM user_company_grants g JOIN users u USING(user_id) WHERE g.user_id=%s AND g.company_id=%s AND u.is_active",
        (auth.user_id, company_id),
    ).fetchone()
    if not row or not row["can_read"] or (write and not row["can_update"]):
        raise AppError("ACCESS_DENIED", "当前用户无权执行这项企业操作。", 403)
    return row


def assert_import_access(conn, auth: Identity, company_id: str) -> dict:
    profile = conn.execute("SELECT profile FROM dataset_metadata WHERE singleton_id=1").fetchone()
    if not profile or profile["profile"] != "extended":
        raise AppError("IMPORT_DISABLED", "比赛基线不接受追加，请使用独立扩展数据集。", 403)
    grant = assert_access(conn, auth, company_id)
    if not grant["can_import_invoices"]:
        raise AppError("IMPORT_ACCESS_DENIED", "当前用户没有该企业的发票导入权限。", 403)
    return grant


def owned(conn, table: str, key: str, resource_id: str, auth: Identity, *, lock=False) -> dict:
    # Identifiers are internal constants; request values remain bound parameters.
    allowed = {
        "sessions": "session_id",
        "turns": "turn_id",
        "tasks": "task_id",
        "preferences": "preference_id",
        "result_views": "view_id",
    }
    if allowed.get(table) != key:
        raise ValueError("Unsupported resource")
    from psycopg import sql

    query = sql.SQL("SELECT * FROM {} WHERE {}=%s AND user_id=%s{}").format(
        sql.Identifier(table), sql.Identifier(key), sql.SQL(" FOR UPDATE" if lock else "")
    )
    row = conn.execute(query, (resource_id, auth.user_id)).fetchone()
    if not row:
        raise AppError("RESOURCE_NOT_FOUND", "记录不存在或无权访问。", 404)
    if table == "sessions" and row.get("deleted_at"):
        raise AppError("RESOURCE_NOT_FOUND", "会话已删除。", 404)
    from app.resource_access import session_allowed, turn_allowed

    if table == "sessions" and not session_allowed(conn, auth.user_id, resource_id):
        raise AppError("RESOURCE_NOT_FOUND", "会话不存在或当前无权访问。", 404)
    if table == "turns":
        session = conn.execute(
            "SELECT deleted_at FROM sessions WHERE session_id=%s", (row["session_id"],)
        ).fetchone()
        if not session or session["deleted_at"]:
            raise AppError("RESOURCE_NOT_FOUND", "会话已删除。", 404)
        if not turn_allowed(conn, auth.user_id, resource_id):
            raise AppError("ACCESS_DENIED", "当前企业权限已变化，无法访问此记录。", 403)
    if row.get("company_id"):
        assert_access(conn, auth, row["company_id"])
    return row


def login(db, user_id: str, *, ttl_seconds=43200, previous_token=None) -> str:
    token = secrets.token_urlsafe(40)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db.transaction() as conn:
        user = conn.execute(
            "SELECT auth_epoch FROM users WHERE user_id=%s AND is_active FOR UPDATE", (user_id,)
        ).fetchone()
        if not user:
            raise AppError("INVALID_ARGUMENT", "请选择比赛提供的模拟用户。")
        if previous_token:
            conn.execute(
                "UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE token_hash=%s AND revoked_at IS NULL",
                (hashlib.sha256(previous_token.encode()).hexdigest(),),
            )
        conn.execute(
            "INSERT INTO login_sessions(token_hash,user_id,expires_at,auth_epoch) VALUES(%s,%s,clock_timestamp()+%s*interval '1 second',%s)",
            (token_hash, user_id, ttl_seconds, user["auth_epoch"]),
        )
    return token


def authenticate(db, token: str | None) -> Identity:
    if not token:
        raise AppError("UNAUTHENTICATED", "请先选择模拟用户。", 401)
    with db.transaction(readonly=True) as conn:
        row = conn.execute(
            "SELECT l.user_id FROM login_sessions l JOIN users u USING(user_id) WHERE l.token_hash=%s AND l.expires_at>clock_timestamp() AND l.revoked_at IS NULL AND l.auth_epoch=u.auth_epoch AND u.is_active",
            (hashlib.sha256(token.encode()).hexdigest(),),
        ).fetchone()
        if not row:
            raise AppError("UNAUTHENTICATED", "登录已失效，请重新选择用户。", 401)
        return Identity(row["user_id"], hashlib.sha256(token.encode()).hexdigest())


def assert_login_active(conn, auth: Identity):
    if not auth.token_hash:
        raise AppError("UNAUTHENTICATED", "需要有效的登录会话。", 401)
    if not conn.execute(
        "SELECT 1 FROM login_sessions l JOIN users u USING(user_id) WHERE l.token_hash=%s AND l.user_id=%s AND l.expires_at>clock_timestamp() AND l.revoked_at IS NULL AND l.auth_epoch=u.auth_epoch AND u.is_active",
        (auth.token_hash, auth.user_id),
    ).fetchone():
        raise AppError("UNAUTHENTICATED", "登录已过期或已退出。", 401)


def logout(db, token):
    if token:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE token_hash=%s AND revoked_at IS NULL",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )


def logout_all(db, auth):
    with db.transaction() as conn:
        conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
        assert_login_active(conn, auth)
        conn.execute("UPDATE users SET auth_epoch=auth_epoch+1 WHERE user_id=%s", (auth.user_id,))
        conn.execute(
            "UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE user_id=%s AND revoked_at IS NULL",
            (auth.user_id,),
        )


def me(db, auth: Identity) -> dict:
    with db.transaction(readonly=True) as conn:
        user = conn.execute(
            "SELECT user_id,display_name FROM users WHERE user_id=%s", (auth.user_id,)
        ).fetchone()
        grants = conn.execute(
            "SELECT c.*,g.can_update,g.can_import_invoices,g.can_upload_knowledge FROM companies c JOIN user_company_grants g USING(company_id) "
            "WHERE g.user_id=%s AND g.can_read ORDER BY c.company_id",
            (auth.user_id,),
        ).fetchall()
        profile = conn.execute(
            "SELECT profile FROM dataset_metadata WHERE singleton_id=1"
        ).fetchone()["profile"]
        return {**user, "companies": grants, "dataset_profile": profile}


def create_session(db, auth: Identity, company_id: str | None) -> dict:
    with db.transaction() as conn:
        if company_id:
            assert_access(conn, auth, company_id)
        return conn.execute(
            "INSERT INTO sessions(session_id,user_id,selected_company_id) VALUES(%s,%s,%s) RETURNING *",
            (uid(), auth.user_id, company_id),
        ).fetchone()


def switch_empty_session_company(
    db, auth: Identity, session_id: str, company_id: str | None
) -> dict:
    """Reuse an empty conversation without moving existing messages to another company."""
    with db.transaction() as conn:
        # Turn admission uses this same lock, so an accepted turn cannot race the check.
        conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
        owned(conn, "sessions", "session_id", session_id, auth, lock=True)
        if company_id:
            assert_access(conn, auth, company_id)
        if conn.execute(
            "SELECT 1 FROM turns WHERE session_id=%s LIMIT 1", (session_id,)
        ).fetchone():
            raise AppError("SESSION_NOT_EMPTY", "当前会话已有内容，需新建会话。", 409)
        return conn.execute(
            "UPDATE sessions SET selected_company_id=%s,updated_at=now() WHERE session_id=%s RETURNING *",
            (company_id, session_id),
        ).fetchone()


def switch_company(db, auth: Identity, session_id: str, company_id: str | None) -> dict:
    with db.transaction() as conn:
        conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
        owned(conn, "sessions", "session_id", session_id, auth, lock=True)
        if company_id:
            assert_access(conn, auth, company_id)
        interrupt_session(conn, auth.user_id, session_id)
        conn.execute(
            "UPDATE users SET memory_epoch=memory_epoch+1 WHERE user_id=%s", (auth.user_id,)
        )
        conn.execute(
            "UPDATE result_views SET status='invalidated' WHERE session_id=%s", (session_id,)
        )
        return conn.execute(
            "UPDATE sessions SET selected_company_id=%s,updated_at=now() WHERE session_id=%s RETURNING *",
            (company_id, session_id),
        ).fetchone()
