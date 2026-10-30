import hashlib
import secrets
from dataclasses import dataclass

from app.errors import AppError
from app.events import interrupt_session
from app.values import uid


@dataclass(frozen=True)
class Identity:
    user_id: str


def assert_access(conn, auth: Identity, company_id: str, *, write: bool = False) -> dict:
    row = conn.execute(
        "SELECT * FROM user_company_grants WHERE user_id=%s AND company_id=%s",
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
    if row.get("company_id"):
        assert_access(conn, auth, row["company_id"])
    return row


def login(db, user_id: str) -> str:
    token = secrets.token_urlsafe(40)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db.transaction() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE user_id=%s", (user_id,)).fetchone():
            raise AppError("INVALID_ARGUMENT", "请选择比赛提供的模拟用户。")
        conn.execute(
            "INSERT INTO login_sessions(token_hash,user_id,expires_at) VALUES(%s,%s,now()+interval '12 hours')",
            (token_hash, user_id),
        )
    return token


def authenticate(db, token: str | None) -> Identity:
    if not token:
        raise AppError("UNAUTHENTICATED", "请先选择模拟用户。", 401)
    with db.transaction(readonly=True) as conn:
        row = conn.execute(
            "SELECT user_id FROM login_sessions WHERE token_hash=%s AND expires_at>now()",
            (hashlib.sha256(token.encode()).hexdigest(),),
        ).fetchone()
        if not row:
            raise AppError("UNAUTHENTICATED", "登录已失效，请重新选择用户。", 401)
        return Identity(row["user_id"])


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
