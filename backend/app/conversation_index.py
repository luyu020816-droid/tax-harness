"""Rebuildable chat-only index. Raw sources and current access remain authoritative."""

from app.conversation_log import read
from app.errors import AppError
from app.identity import Identity
from app.values import digest, jsonb

CHUNKER = "conversation-char600-overlap60-token1200-v1"
INPUT_FORMAT = "conversation-role-prefix-v1"
ELIGIBLE = """x.recall_state='active' AND s.deleted_at IS NULL
AND t.status IN ('completed','waiting_user','resolved','failed','cancelled','interrupted')
AND (x.role='user' OR (x.role='assistant' AND x.source_id=t.final_response_ref))"""


def available(conn):
    return bool(
        conn.execute("SELECT to_regclass('public.conversation_passages') t").fetchone()["t"]
    )


def embedding_text(role, text):
    return f"历史{'用户' if role == 'user' else '助手'}消息：\n{text}"


def chunks(text, role, count):
    result, start = [], 0
    while start < len(text):
        end = min(start + 600, len(text))
        while count(embedding_text(role, text[start:end])) > 1200:
            end = start + (end - start) // 2
            if end <= start:
                raise AppError("DOCUMENT_SEGMENT_TOO_LARGE", "聊天原文无法在分块预算内索引。", 422)
        result.append((start, end, text[start:end]))
        if end == len(text):
            break
        start = max(start + 1, end - min(60, (end - start) // 4))
    return result


def source_in(conn, source_id):
    row = conn.execute(
        "SELECT x.user_id FROM sources x JOIN turns t USING(turn_id) JOIN sessions s USING(session_id) "
        "WHERE x.source_id=%s AND " + ELIGIBLE,
        (source_id,),
    ).fetchone()
    if not row:
        raise AppError("SOURCE_INVALIDATED", "消息尚未结束或已不适合聊天索引。", 409)
    return read(conn, Identity(row["user_id"]), str(source_id))


def profile(engine):
    inference, manifest = engine.profile()
    if manifest.get("dimension") != 1024:
        raise AppError("DEPENDENCY_UNAVAILABLE", "聊天索引只支持当前1024维安装。", 503)
    spec = {
        "inference_profile_id": inference,
        "inference": manifest,
        "chunker": CHUNKER,
        "input_format": INPUT_FORMAT,
    }
    return digest(spec), spec


def register(conn, engine, *, enabled):
    profile_id, manifest = profile(engine)
    if enabled:
        conn.execute("UPDATE conversation_index_profiles SET enabled=false WHERE enabled")
    conn.execute(
        "INSERT INTO conversation_index_profiles(profile_id,inference_profile_id,manifest,dimension,enabled) "
        "VALUES(%s,%s,%s,1024,%s) ON CONFLICT(profile_id) DO UPDATE SET enabled=excluded.enabled",
        (profile_id, manifest["inference_profile_id"], jsonb(manifest), enabled),
    )
    return profile_id


def readiness(db, settings):
    from app.conversation_retrieval import policy

    with db.transaction(readonly=True) as conn:
        present = available(conn)
        active = (
            conn.execute(
                "SELECT profile_id FROM conversation_index_profiles WHERE enabled"
            ).fetchone()
            if present
            else None
        )
    result = {
        "mode": settings.conversation_semantic_mode,
        "schema_available": present,
        "profile_ready": bool(active),
        "policy_ready": False,
    }
    if active:
        try:
            policy(settings, active["profile_id"])
            result["policy_ready"] = True
        except AppError:
            pass
    return result
