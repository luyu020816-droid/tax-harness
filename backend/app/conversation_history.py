"""Independent lexical/date history search and exact, currently-authorized reads."""

import re

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
            "source_length": len(source["content"]),
            "next_char_start": end if end < len(source["content"]) else None,
            "historical_only": True,
        }
    )


def evidence_in(conn, auth, citation_id):
    row = conn.execute(
        "SELECT e.*,q.query_plan,q.scope_snapshot FROM history_evidence e JOIN history_queries q USING(query_id) WHERE e.citation_id=%s AND q.user_id=%s",
        (citation_id, auth.user_id),
    ).fetchone()
    if not row or row["query_plan"].get("kind") != "conversation":
        raise AppError("RESOURCE_NOT_FOUND", "历史引用不存在或无权访问。", 404)
    source = read(conn, auth, str(row["source_id"]))
    excerpt = source["content"][row["char_start"] : row["char_end"]]
    if source["revision"] != row["source_revision"] or digest(excerpt) != row["excerpt_hash"]:
        raise AppError("SOURCE_INVALIDATED", "历史引用已失效。", 409)
    result = _view(source, str(citation_id), excerpt, row["char_start"], row["char_end"])
    result["neighbors"] = neighbors_in(
        conn,
        auth,
        source,
        row["scope_snapshot"].get("before_source_seq", source["source_seq"] + 1),
        row["query_plan"].get("arguments", {}),
    )
    return result


def scope_in(conn, execution, arguments, before, through):
    from app.identity import assert_access
    from app.turns import mentioned_companies

    conditions = [
        SESSION_VISIBLE,
        "x.recall_state='active'",
        "x.role IN ('user','assistant')",
        "t.source_seq<%(before)s",
        "e.event_id<=%(through)s",
    ]
    params = {"user": execution.auth.user_id, "before": before, "through": through}
    for field, clause in [
        ("session_id", "s.session_id=%(session_id)s"),
        ("date_from", "x.created_at>=%(date_from)s"),
        ("date_to", "x.created_at<%(date_to)s"),
    ]:
        if arguments.get(field):
            conditions.append(clause)
            params[field] = (
                date_bound(arguments[field]) if field.startswith("date_") else arguments[field]
            )
    companies = mentioned_companies(conn, arguments.get("query", ""))
    # IDs are metadata, not a requirement that C001 literally occur in prose.
    companies = list(
        dict.fromkeys(
            [
                *companies,
                *[
                    r["company_id"]
                    for r in conn.execute(
                        "SELECT company_id FROM companies WHERE company_id=ANY(%s::text[])",
                        (
                            re.findall(
                                r"(?<![A-Za-z0-9])C\d{3,12}(?![A-Za-z0-9])",
                                arguments.get("query", ""),
                                re.I,
                            ),
                        ),
                    ).fetchall()
                ],
            ]
        )
    )
    if getattr(execution, "source_id", None):
        current = conn.execute(
            "SELECT content FROM sources WHERE source_id=%s AND user_id=%s",
            (execution.source_id, execution.auth.user_id),
        ).fetchone()
        trusted = mentioned_companies(conn, current["content"]) if current else []
        if trusted and companies and not set(companies).issubset(trusted):
            raise AppError("SCOPE_MISMATCH", "历史搜索企业与本轮明确范围不一致。", 409)
        companies = trusted or companies
    for company in companies:
        assert_access(conn, execution.auth, company)
    if companies:
        conditions.append(
            "NOT EXISTS(SELECT 1 FROM source_scopes z WHERE z.source_id=x.source_id AND NOT(z.company_id=ANY(%(companies)s::text[])))"
        )
        params["companies"] = companies
    return conditions, params


def _lexical_search(
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
        conditions, params = scope_in(conn, execution, normalized, before, through)
        conditions.append("e.event_id>%(after)s")
        params.update(after=after, page_limit=page_size + 1)
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
        result = _evidence(conn, execution.auth, query_id, source, char_start)
        result["neighbors"] = neighbors_in(conn, execution.auth, source, execution.source_seq)
        return result


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
    mode="auto",
    settings=None,
    engine=None,
):
    import psycopg

    from app import conversation_retrieval

    if mode not in ("auto", "lexical", "semantic"):
        raise AppError("INVALID_ARGUMENT", "未知聊天检索模式。", 422)
    begin, end = date_bound(date_from), date_bound(date_to)
    if begin and end and begin >= end:
        raise AppError("INVALID_ARGUMENT", "历史开始日期必须早于结束日期。", 422)
    args = {
        "query": query.strip(),
        "session_id": str(session_id) if session_id else None,
        "date_from": date_from,
        "date_to": date_to,
        "page_size": page_size,
    }
    semantic_mode = settings.conversation_semantic_mode if settings else "off"
    if cursor:
        with db.transaction(readonly=True) as conn:
            old = conn.execute(
                "SELECT query_plan FROM history_queries WHERE query_id=%s AND user_id=%s",
                (cursor, execution.auth.user_id),
            ).fetchone()
        if not old:
            raise AppError("INVALID_ARGUMENT", "聊天游标不存在或不属于当前用户。", 422)
        if old and old["query_plan"].get("version") == conversation_retrieval.VERSION:
            return conversation_retrieval.page(
                db, execution, {**args, "mode": mode}, cursor, settings, engine
            )
        return _lexical_search(db, execution, **args, cursor=cursor)
    shadow_id, degraded = None, None
    if query.strip() and mode != "lexical" and semantic_mode != "off":
        try:
            if engine is None:
                raise AppError("DEPENDENCY_UNAVAILABLE", "聊天语义模型尚未接入。", 503)
            shadow_id = conversation_retrieval.search(
                db, execution, {**args, "mode": mode}, settings, engine
            )
            if semantic_mode == "on":
                return conversation_retrieval.page(
                    db, execution, {**args, "mode": mode}, shadow_id, settings, engine
                )
        except (AppError, ImportError, OSError, RuntimeError, ValueError, psycopg.Error) as exc:
            if isinstance(exc, AppError) and exc.code not in (
                "DEPENDENCY_UNAVAILABLE",
                "SELECTION_POLICY_UNAVAILABLE",
            ):
                raise
            degraded = getattr(exc, "code", "DEPENDENCY_UNAVAILABLE")
    result = _lexical_search(db, execution, **args, cursor=cursor)
    if shadow_id or degraded:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE history_queries SET query_plan=query_plan || %s WHERE query_id=%s",
                (
                    jsonb({"shadow_query_id": shadow_id, "shadow_error": degraded}),
                    result["query_id"],
                ),
            )
    if query.strip() and mode != "lexical" and (semantic_mode == "on" or mode == "semantic"):
        result.update(
            status="partial",
            degraded_reason=degraded or "semantic_not_enabled",
            coverage={"complete": False},
            semantic_search=False,
        )
    return result


def neighbors_in(conn, auth, source, before, filters=None):
    from app.conversation_index import ELIGIBLE

    filters = filters or {}
    original = conn.execute(
        "SELECT event_id FROM conversation_events WHERE source_id=%s", (source["source_id"],)
    ).fetchone()
    if not original:
        return []
    companies = [
        r["company_id"]
        for r in conn.execute(
            "SELECT DISTINCT z.company_id FROM source_scopes z JOIN sources a USING(source_id) WHERE a.turn_id=%s",
            (source["turn_id"],),
        ).fetchall()
    ]
    result = []
    for direction, comparison, order in [("previous", "<", "DESC"), ("next", ">", "ASC")]:
        clauses = [
            ELIGIBLE,
            "e.session_id=%s",
            "e.user_id=%s",
            "t.source_seq<%s",
            f"e.event_id{comparison}%s",
            "NOT EXISTS(SELECT 1 FROM source_scopes z WHERE z.source_id=x.source_id AND NOT(z.company_id=ANY(%s::text[])))",
        ]
        params = [source["session_id"], auth.user_id, before, original["event_id"], companies]
        for key, operation in [("date_from", ">="), ("date_to", "<")]:
            if filters.get(key):
                clauses.append(f"x.created_at{operation}%s")
                params.append(date_bound(filters[key]))
        rows = conn.execute(
            "SELECT x.source_id FROM conversation_events e JOIN sources x ON x.source_id=e.source_id "
            "JOIN turns t ON t.turn_id=e.turn_id JOIN sessions s ON s.session_id=t.session_id WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY e.event_id {order} LIMIT 16",
            params,
        ).fetchall()
        for row in rows:
            try:
                neighbor = read(conn, auth, str(row["source_id"]))
            except AppError as exc:
                if exc.code not in ("SOURCE_INVALIDATED", "RESOURCE_NOT_FOUND", "ACCESS_DENIED"):
                    raise
                continue
            result.append(
                {
                    "direction": direction,
                    "source_id": str(neighbor["source_id"]),
                    "role": neighbor["role"],
                    "created_at": safe_json(neighbor["created_at"]),
                }
            )
            break
    return result
