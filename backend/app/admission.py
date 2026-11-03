"""Cross-process queue admission; checked in the same transaction as enqueue."""

from app.config import Settings
from app.errors import AppError


def check(conn, user_id, settings=None):
    settings = settings or Settings()
    # Callers already hold their user row; no model/inference work under this lock.
    conn.execute("SELECT pg_advisory_xact_lock(8123419415)")
    row = conn.execute(
        "SELECT count(*) FILTER(WHERE user_id=%s AND status IN ('queued','running')) AS own_pending,"
        "count(*) FILTER(WHERE status IN ('queued','running')) AS global_pending,"
        "count(*) FILTER(WHERE user_id=%s AND created_at>clock_timestamp()-interval '1 minute') AS recent "
        "FROM turns WHERE status IN ('queued','running') OR created_at>clock_timestamp()-interval '1 minute'",
        (user_id, user_id),
    ).fetchone()
    retries = conn.execute(
        "SELECT count(*) n FROM memory_change_ops WHERE user_id=%s AND target_type='turn' AND action='retry' AND created_at>clock_timestamp()-interval '1 minute'",
        (user_id,),
    ).fetchone()["n"]
    if (
        row["own_pending"] >= settings.max_pending_per_user
        or row["global_pending"] >= settings.max_pending_global
        or row["recent"] + retries >= settings.requests_per_minute
    ):
        raise AppError(
            "REQUEST_LIMIT_REACHED",
            "当前请求或排队任务过多，请稍后重试。",
            429,
            retryable=True,
            details={"retry_after_seconds": 5},
        )
