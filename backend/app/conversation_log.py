"""Exact source reads from the durable conversation log, with current authorization."""

from app.errors import AppError
from app.identity import owned
from app.sources import read_source

VERSION = "conversation-log-v1"


def index_source(conn, source):
    if source["role"] not in ("user", "assistant"):
        return
    from app.retrieval_engine import LEXICAL_VERSION, terms

    conn.execute(
        "INSERT INTO conversation_terms(source_id,source_hash,lexical_version,lexical_index) "
        "VALUES(%s,%s,%s,to_tsvector('simple',%s)) ON CONFLICT DO NOTHING",
        (
            source["source_id"],
            source["content_hash"],
            LEXICAL_VERSION,
            " ".join(terms(source["content"])),
        ),
    )


def snapshot(conn, auth, session_id):
    owned(conn, "sessions", "session_id", session_id, auth)
    return conn.execute(
        "SELECT coalesce(max(event_id),0) n FROM conversation_events WHERE session_id=%s AND user_id=%s",
        (session_id, auth.user_id),
    ).fetchone()["n"]


def read(conn, auth, source_id):
    row = conn.execute(
        "SELECT t.session_id,t.source_seq FROM sources s JOIN turns t USING(turn_id) "
        "WHERE s.source_id=%s AND s.user_id=%s",
        (source_id, auth.user_id),
    ).fetchone()
    if not row:
        raise AppError("RESOURCE_NOT_FOUND", "历史记录不存在或不可访问。", 404)
    owned(conn, "sessions", "session_id", str(row["session_id"]), auth)
    source = read_source(conn, auth, source_id)
    if source["role"] == "assistant":
        from app.knowledge_retrieval import read_evidence_group_in, validate_retrievals

        for record in conn.execute(
            "SELECT c.manifest FROM context_snapshots c JOIN runs r USING(run_id) WHERE r.final_source_id=%s",
            (source_id,),
        ).fetchall():
            manifest = record["manifest"]
            validate_retrievals(conn, auth, manifest.get("retrieval_refs", []))
            for dependency in manifest.get("knowledge_dependencies", []):
                read_evidence_group_in(conn, auth, dependency["citation_id"])
    return {**source, "session_id": str(row["session_id"])}
