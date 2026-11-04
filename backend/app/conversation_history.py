"""Independent lexical/date history search and exact, currently-authorized reads."""

from app.conversation_log import read
from app.errors import AppError
from app.history_retrieval import date_bound
from app.resource_access import SESSION_VISIBLE
from app.retrieval_engine import terms
from app.values import digest, jsonb, safe_json, uid


def _evidence(conn, auth, query_id, source, start=0, size=1200):
    if source["role"] not in ("user", "assistant"):
        raise AppError("ACCESS_DENIED", "历史搜索不返回工具正文，请通过当前业务工具核验。", 403)
    if not 0 <= start < max(1, len(source["content"])):
        raise AppError("INVALID_ARGUMENT", "历史原文位置无效。", 422)
    end = min(start + size, len(source["content"]))
    excerpt = source["content"][start:end]
    citation = uid()
    conn.execute(
        "INSERT INTO history_evidence(citation_id,query_id,source_id,source_revision,char_start,char_end,excerpt,excerpt_hash,rank) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,(SELECT coalesce(max(rank),0)+1 FROM history_evidence WHERE query_id=%s))",
        (
            citation,
            query_id,
            source["source_id"],
            source["revision"],
            start,
            end,
            excerpt,
            digest(excerpt),
            query_id,
        ),
    )
    return _view(source, citation, excerpt, start, end)


def _view(source, citation, excerpt, start, end):
    return safe_json(
        {
            "kind": "conversation_evidence",
            "citation_id": citation,
            "source_id": source["source_id"],
            "session_id": source["session_id"],
            "revision": source["revision"],
            "role": source["role"],
            "created_at": source["created_at"],
            "content": excerpt,
            "excerpt": excerpt,
            "excerpt_hash": digest(excerpt),
            "locator": {"char_start": start, "char_end": end},
            "next_char_start": end if end < len(source["content"]) else None,
            "historical_only": True,
        }
    )


def evidence_in(conn, auth, citation_id):
    row = conn.execute(
        "SELECT e.*,q.query_plan FROM history_evidence e JOIN history_queries q USING(query_id) WHERE e.citation_id=%s AND q.user_id=%s",
        (citation_id, auth.user_id),
    ).fetchone()
    if not row or row["query_plan"].get("kind") != "conversation":
        raise AppError("RESOURCE_NOT_FOUND", "历史引用不存在或无权访问。", 404)
    source = read(conn, auth, str(row["source_id"]))
    excerpt = source["content"][row["char_start"] : row["char_end"]]
    if source["revision"] != row["source_revision"] or digest(excerpt) != row["excerpt_hash"]:
        raise AppError("SOURCE_INVALIDATED", "历史引用已失效。", 409)
    return _view(source, str(citation_id), excerpt, row["char_start"], row["char_end"])


def search(
    db,
    execution,
    *,
    query="",
    session_id=None,
    date_from=None,
    date_to=None,
    cursor=None,
    page_size=5,
):
    begin, end = date_bound(date_from), date_bound(date_to)
    if begin and end and begin >= end:
        raise AppError("INVALID_ARGUMENT", "历史开始日期必须早于结束日期。", 422)
    normalized = {
        "page_size": page_size,
        "query": query.strip(),
        "session_id": session_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    with db.transaction() as conn:
        # Persistent cursor is bound to this request, caller and corpus snapshot.
        before, after = execution.source_seq, 0
        through = conn.execute(
            "SELECT coalesce(max(event_id),0) n FROM conversation_events WHERE user_id=%s",
            (execution.auth.user_id,),
        ).fetchone()["n"]
        if cursor:
            old = conn.execute(
                "SELECT * FROM history_queries WHERE query_id=%s AND user_id=%s",
                (cursor, execution.auth.user_id),
            ).fetchone()
            if not old or old["query_plan"].get("arguments") != normalized:
                raise AppError("INVALID_ARGUMENT", "历史游标与查询范围不一致。", 422)
            before = old["scope_snapshot"]["before_source_seq"]
            after = old["pagination"]["after_event_id"]
            through = old["scope_snapshot"]["through_event_id"]
        conditions = [
            SESSION_VISIBLE,
            "x.recall_state='active'",
            "x.role IN ('user','assistant')",
            "t.source_seq<%(before)s",
            "e.event_id>%(after)s",
            "e.event_id<=%(through)s",
        ]
        params = {
            "user": execution.auth.user_id,
            "before": before,
            "after": after,
            "through": through,
            "page_limit": page_size + 1,
        }
        if session_id:
            conditions.append("s.session_id=%(session)s")
            params["session"] = session_id
        if begin:
            conditions.append("x.created_at>=%(begin)s")
            params["begin"] = begin
        if end:
            conditions.append("x.created_at<%(end)s")
            params["end"] = end
        if getattr(execution, "source_id", None):
            from app.identity import assert_access
            from app.turns import mentioned_companies

            current = conn.execute(
                "SELECT content FROM sources WHERE source_id=%s AND user_id=%s",
                (execution.source_id, execution.auth.user_id),
            ).fetchone()
            companies = mentioned_companies(conn, current["content"]) if current else []
            for company in companies:
                assert_access(conn, execution.auth, company)
            if companies:
                conditions.append(
                    "NOT EXISTS(SELECT 1 FROM source_scopes z WHERE z.source_id=x.source_id AND NOT(z.company_id=ANY(%(companies)s::text[])))"
                )
                params["companies"] = companies
        tokens = terms(query)[:16]
        if query.strip():
            # Literal phrase OR all segmented terms; no user-supplied regex/SQL syntax.
            params["literal"] = query.strip().lower()
            params["terms"] = " ".join(tokens)
            conditions.append(
                "(strpos(lower(x.content),%(literal)s)>0 OR i.lexical_index @@ plainto_tsquery('simple',%(terms)s))"
            )
        rows = conn.execute(
            "SELECT e.event_id,x.source_id FROM conversation_events e JOIN sources x ON x.source_id=e.source_id "
            "JOIN turns t ON t.turn_id=e.turn_id JOIN sessions s ON s.session_id=e.session_id "
            "LEFT JOIN conversation_terms i ON i.source_id=x.source_id AND i.source_hash=x.content_hash WHERE "
            + " AND ".join(conditions)
            + " ORDER BY e.event_id LIMIT %(page_limit)s",
            params,
        ).fetchall()
        query_id = uid()
        conn.execute(
            "INSERT INTO history_queries(query_id,user_id,query_plan,scope_snapshot,status,retrieval_mode,coverage,candidates,timings,pagination) "
            "VALUES(%s,%s,%s,%s,'ok','conversation_lexical',%s,'[]','{}',%s)",
            (
                query_id,
                execution.auth.user_id,
                jsonb({"kind": "conversation", "arguments": normalized}),
                jsonb({"before_source_seq": before, "through_event_id": through}),
                jsonb({"kind": "authorized_lexical", "semantic": False}),
                jsonb(
                    {
                        "after_event_id": rows[page_size - 1]["event_id"]
                        if len(rows) > page_size
                        else (rows[-1]["event_id"] if rows else after)
                    }
                ),
            ),
        )
        matches = []
        for row in rows[:page_size]:
            source = read(conn, execution.auth, str(row["source_id"]))
            content = source["content"].lower()
            position = content.find(query.strip().lower()) if query.strip() else 0
            if position < 0:
                offsets = [content.find(token) for token in tokens if content.find(token) >= 0]
                position = min(offsets) if offsets else 0
            start = max(0, position - 60)
            item = _evidence(conn, execution.auth, query_id, source, start=start, size=240)
            item["previous_char_start"] = max(0, start - 1200) if start else None
            matches.append(item)
        return {
            "kind": "conversation_search",
            "query_id": query_id,
            "matches": matches,
            "next_cursor": query_id if len(rows) > page_size else None,
            "status": "ok" if matches else "not_found",
            "mode": "lexical",
            "semantic_search": False,
            "historical_only": True,
        }


def read_page(db, execution, *, source_id, char_start=0):
    with db.transaction() as conn:
        source = read(conn, execution.auth, source_id)
        if source["source_seq"] >= execution.source_seq:
            raise AppError("SOURCE_INVALIDATED", "不能读取本轮或之后的历史记录。", 409)
        query_id = uid()
        conn.execute(
            "INSERT INTO history_queries(query_id,user_id,query_plan,scope_snapshot,status,retrieval_mode,coverage,candidates,timings,pagination) "
            "VALUES(%s,%s,%s,%s,'ok','conversation_exact','{}','[]','{}','{}')",
            (
                query_id,
                execution.auth.user_id,
                jsonb({"kind": "conversation"}),
                jsonb({"before_source_seq": execution.source_seq}),
            ),
        )
        return _evidence(conn, execution.auth, query_id, source, char_start)
