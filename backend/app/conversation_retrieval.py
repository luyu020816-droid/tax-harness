"""Authorized, exact-vector chat candidates with frozen pages and explicit coverage."""

import json
import math
import re
import time
from contextlib import contextmanager, nullcontext

from app import conversation_index as index
from app.conversation_log import read
from app.errors import AppError
from app.retrieval_engine import terms, vector_literal
from app.values import digest, jsonb, uid

VERSION = "conversation-hybrid-v1"


@contextmanager
def measured(db, execution, timings, name):
    from app.telemetry import phase

    started = time.monotonic()
    manager = (
        phase(db, execution, "conversation:" + name + ":" + uid(), "retrieval", name)
        if hasattr(execution, "run_id")
        else nullcontext({"attributes": {}})
    )
    try:
        with manager as span:
            yield span["attributes"]
    finally:
        timings[name] = round((time.monotonic() - started) * 1000, 3)


def policy(settings, profile_id):
    try:
        value = json.loads(settings.conversation_selection_policy.read_text(encoding="utf-8"))
        if (
            value["profile_id"] != profile_id
            or value["algorithm"] != "dense-rerank-v1"
            or not value["development_hash"]
        ):
            raise ValueError
        if not all(
            type(value[k]) in (int, float) and math.isfinite(value[k])
            for k in ("dense_min", "rerank_min")
        ):
            raise ValueError
        return value
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise AppError("SELECTION_POLICY_UNAVAILABLE", "聊天语义支持度策略尚未验证。", 503) from exc


def search(db, execution, arguments, settings, engine):
    from app.conversation_history import scope_in

    timings = {}
    with db.transaction(readonly=True) as conn:
        if not index.available(conn):
            raise AppError("DEPENDENCY_UNAVAILABLE", "聊天语义索引尚未安装。", 503)
        active = conn.execute("SELECT * FROM conversation_index_profiles WHERE enabled").fetchone()
        if not active:
            raise AppError("DEPENDENCY_UNAVAILABLE", "聊天语义索引尚未准备。", 503)
        profile_id = active["profile_id"]
        through = conn.execute(
            "SELECT coalesce(max(event_id),0) n FROM conversation_events WHERE user_id=%s",
            (execution.auth.user_id,),
        ).fetchone()["n"]
        clauses, params = scope_in(conn, execution, arguments, execution.source_seq, through)
        params["profile"] = profile_id
        # Identifier matching is a hard constraint on both lanes, not a similarity hint.
        identifiers = re.findall(
            r"(?<![A-Za-z0-9])[FR]\d{3,12}(?![A-Za-z0-9])", arguments["query"], re.I
        )
        identifier_clauses = []
        for n, identifier in enumerate(identifiers):
            params[f"identifier{n}"] = (
                r"(^|[^A-Za-z0-9])" + re.escape(identifier) + r"([^A-Za-z0-9]|$)"
            )
            identifier_clauses.append(f"x.content ~* %(identifier{n})s")
        if identifier_clauses:
            clauses.append("(" + " OR ".join(identifier_clauses) + ")")
        where = " AND ".join(clauses)
        coverage = conn.execute(
            "SELECT count(*) FILTER(WHERE " + index.ELIGIBLE + ") eligible,"
            "count(*) FILTER(WHERE "
            + index.ELIGIBLE
            + " AND j.published_generation_id IS NOT NULL "
            "AND j.source_hash=x.content_hash AND j.source_revision=x.revision) indexed,"
            "count(*) FILTER(WHERE j.state IN ('failed','cancelled')) failed,"
            "count(*) FILTER(WHERE t.status IN ('queued','running')) unsettled "
            "FROM conversation_events e JOIN sources x ON x.source_id=e.source_id JOIN turns t ON t.turn_id=e.turn_id "
            "JOIN sessions s ON s.session_id=t.session_id LEFT JOIN conversation_index_jobs j "
            "ON j.source_id=x.source_id AND j.profile_id=%(profile)s WHERE " + where,
            params,
        ).fetchone()
        coverage = {
            **coverage,
            "pending": max(0, coverage["eligible"] - coverage["indexed"]),
            "through_event_id": through,
            "dependency_validation": "revalidated_before_candidate_delivery",
        }
        with measured(db, execution, timings, "聊天词法候选") as attrs:
            params.update(
                literal=arguments["query"].lower(), terms=" ".join(terms(arguments["query"])[:16])
            )
            lexical = conn.execute(
                "SELECT x.source_id FROM conversation_events e JOIN sources x ON x.source_id=e.source_id "
                "JOIN turns t ON t.turn_id=e.turn_id JOIN sessions s ON s.session_id=t.session_id "
                "LEFT JOIN conversation_terms i ON i.source_id=x.source_id AND i.source_hash=x.content_hash "
                "WHERE " + where + " AND (strpos(lower(x.content),%(literal)s)>0 OR "
                "i.lexical_index @@ plainto_tsquery('simple',%(terms)s)) ORDER BY "
                "(strpos(lower(x.content),%(literal)s)>0) DESC,ts_rank(i.lexical_index,plainto_tsquery('simple',%(terms)s)) DESC,e.event_id LIMIT 31",
                params,
            ).fetchall()
            attrs.update(profile_id=profile_id, candidate_count=min(len(lexical), 30))
    if index.profile(engine)[0] != profile_id:
        raise AppError("DEPENDENCY_UNAVAILABLE", "当前模型与聊天索引profile不一致。", 503)
    selected_policy = None
    if settings.conversation_semantic_mode == "on":
        selected_policy = policy(settings, profile_id)
    with measured(db, execution, timings, "聊天查询向量") as attrs:
        values = engine.embed([arguments["query"]])
        if len(values) != 1:
            raise AppError("DEPENDENCY_UNAVAILABLE", "查询向量返回数量错误。", 503)
        params["vector"] = vector_literal(values[0])
        attrs.update(
            text_count=1, local_tokens=engine.count(arguments["query"]), provider_cost=None
        )
    with db.transaction(readonly=True) as conn:
        with measured(db, execution, timings, "聊天授权向量候选") as attrs:
            dense = conn.execute(
                "WITH authorized AS MATERIALIZED (SELECT x.source_id FROM conversation_events e "
                "JOIN sources x ON x.source_id=e.source_id JOIN turns t ON t.turn_id=e.turn_id "
                "JOIN sessions s ON s.session_id=t.session_id WHERE " + where + "), best AS ("
                "SELECT DISTINCT ON(p.source_id) p.source_id,p.char_start,p.char_end,p.excerpt_hash,"
                "1-(p.embedding <=> %(vector)s::vector) dense_score FROM authorized a "
                "JOIN conversation_passages p USING(source_id) JOIN conversation_index_jobs j "
                "ON j.source_id=p.source_id AND j.profile_id=p.profile_id AND j.published_generation_id=p.generation_id "
                "JOIN sources x ON x.source_id=p.source_id WHERE p.profile_id=%(profile)s "
                "AND p.source_hash=x.content_hash AND p.source_revision=x.revision "
                "ORDER BY p.source_id,p.embedding <=> %(vector)s::vector,p.ordinal) "
                "SELECT * FROM best ORDER BY dense_score DESC,source_id LIMIT 31",
                params,
            ).fetchall()
            attrs.update(profile_id=profile_id, candidate_count=min(len(dense), 30))
    candidates = {}
    for rank, row in enumerate(lexical[:30], 1):
        candidates[str(row["source_id"])] = {
            "source_id": str(row["source_id"]),
            "lexical_rank": rank,
            "rrf": 1 / (60 + rank),
        }
    if arguments["mode"] == "semantic":
        candidates = {}
    for rank, row in enumerate(dense[:30], 1):
        key = str(row["source_id"])
        item = candidates.setdefault(key, {"source_id": key, "rrf": 0})
        item.update({k: row[k] for k in ("char_start", "char_end", "dense_score", "excerpt_hash")})
        item["rrf"] += 1 / (60 + rank)
    candidates = sorted(candidates.values(), key=lambda c: (-c["rrf"], c["source_id"]))
    valid, invalidated = [], 0
    with db.transaction(readonly=True) as conn:
        with measured(db, execution, timings, "聊天候选原文核验"):
            for item in candidates:
                try:
                    source = read(conn, execution.auth, item["source_id"])
                except AppError as exc:
                    if exc.code not in (
                        "SOURCE_INVALIDATED",
                        "RESOURCE_NOT_FOUND",
                        "ACCESS_DENIED",
                    ):
                        raise
                    invalidated += 1
                    continue
                if source["source_seq"] >= execution.source_seq:
                    continue
                start = item.get("char_start")
                if start is None:
                    content = source["content"].lower()
                    found = content.find(arguments["query"].lower())
                    if found < 0:
                        found = min(
                            (content.find(t) for t in terms(arguments["query"]) if t in content),
                            default=0,
                        )
                    start = max(0, found - 60)
                end = item.get("char_end", min(start + 600, len(source["content"])))
                excerpt = source["content"][start:end]
                if "excerpt_hash" in item and item["excerpt_hash"] != digest(excerpt):
                    raise AppError("STATE_SOURCE_UNAVAILABLE", "聊天索引片段校验失败。", 503)
                valid.append(
                    {
                        **item,
                        "char_start": start,
                        "char_end": end,
                        "revision": source["revision"],
                        "source_hash": source["content_hash"],
                        "excerpt_hash": digest(excerpt),
                        "text": excerpt,
                    }
                )
    shortlist = valid[:20]
    if shortlist:
        with measured(db, execution, timings, "聊天候选重排") as attrs:
            scores = engine.rerank(arguments["query"], [c["text"] for c in shortlist])
            if len(scores) != len(shortlist) or not all(math.isfinite(s) for s in scores):
                raise AppError("DEPENDENCY_UNAVAILABLE", "聊天重排结果不完整。", 503)
            attrs.update(
                text_count=len(shortlist),
                local_tokens=None,
                token_measurement="reranker_pair_tokens_not_measured",
                provider_cost=None,
            )
            for item, score in zip(shortlist, scores, strict=True):
                item["rerank_score"] = float(score)
    with measured(db, execution, timings, "聊天融合与支持度") as attrs:
        accepted = select_candidates(valid, selected_policy)
        attrs.update(
            profile_id=profile_id,
            policy_hash=digest(selected_policy),
            candidate_count=len(valid),
            accepted_count=len(accepted),
            invalidated_count=invalidated,
            coverage=coverage,
        )
    coverage.update(
        invalidated_candidates=invalidated,
        complete=coverage["eligible"] == coverage["indexed"]
        and not coverage["unsettled"]
        and not invalidated,
    )
    diagnostics = [{k: v for k, v in c.items() if k != "text"} for c in valid]
    with db.transaction() as conn:
        query_id = uid()
        conn.execute(
            "INSERT INTO history_queries(query_id,user_id,query_plan,scope_snapshot,status,retrieval_mode,profile_id,coverage,candidates,timings,pagination) "
            "VALUES(%s,%s,%s,%s,%s,'conversation_hybrid',%s,%s,%s,%s,%s)",
            (
                query_id,
                execution.auth.user_id,
                jsonb(
                    {
                        "kind": "conversation",
                        "version": VERSION,
                        "arguments": arguments,
                        "policy_hash": digest(selected_policy),
                        "accepted": [c["source_id"] for c in accepted],
                    }
                ),
                jsonb({"before_source_seq": execution.source_seq, "through_event_id": through}),
                "ok" if coverage["complete"] else "partial",
                profile_id,
                jsonb(coverage),
                jsonb(diagnostics),
                jsonb(timings),
                jsonb({"offset": 0, "limit_reached": len(dense) > 30 or len(lexical) > 30}),
            ),
        )
    return query_id


def page(db, execution, arguments, cursor, settings, engine):
    from app.conversation_history import _evidence

    with db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM history_queries WHERE query_id=%s AND user_id=%s",
            (cursor, execution.auth.user_id),
        ).fetchone()
        if (
            not row
            or row["query_plan"].get("version") != VERSION
            or row["query_plan"]["arguments"] != arguments
        ):
            raise AppError("INVALID_ARGUMENT", "聊天候选游标与查询不一致。", 422)
        if settings is None or settings.conversation_semantic_mode != "on":
            raise AppError("INVALID_ARGUMENT", "聊天检索模式已切换，请重新查找。", 422)
        active = conn.execute(
            "SELECT profile_id FROM conversation_index_profiles WHERE enabled"
        ).fetchone()
        if (
            not active
            or active["profile_id"] != row["profile_id"]
            or digest(policy(settings, row["profile_id"])) != row["query_plan"]["policy_hash"]
        ):
            raise AppError("INVALID_ARGUMENT", "聊天索引或策略已切换，请重新查找。", 422)
        # Reapply explicit current-company scope, including an old cursor used in a new turn.
        from app.conversation_history import scope_in

        clauses, params = scope_in(
            conn,
            execution,
            arguments,
            row["scope_snapshot"]["before_source_seq"],
            row["scope_snapshot"]["through_event_id"],
        )
        allowed = {
            str(r["source_id"])
            for r in conn.execute(
                "SELECT x.source_id FROM conversation_events e JOIN sources x ON x.source_id=e.source_id JOIN turns t ON t.turn_id=e.turn_id "
                "JOIN sessions s ON s.session_id=t.session_id WHERE " + " AND ".join(clauses),
                params,
            ).fetchall()
        }
        candidates = {c["source_id"]: c for c in row["candidates"]}
        accepted = row["query_plan"]["accepted"]
        offset, matches = row["pagination"]["offset"], []
        invalidated = 0
        while offset < len(accepted) and len(matches) < arguments["page_size"]:
            key = accepted[offset]
            offset += 1
            if key not in allowed:
                invalidated += 1
                continue
            item = candidates[key]
            try:
                source = read(conn, execution.auth, key)
                if (
                    source["content_hash"] != item["source_hash"]
                    or source["revision"] != item["revision"]
                ):
                    raise AppError("SOURCE_INVALIDATED", "聊天来源已变化。", 409)
            except AppError as exc:
                if exc.code not in ("SOURCE_INVALIDATED", "RESOURCE_NOT_FOUND", "ACCESS_DENIED"):
                    raise
                invalidated += 1
                continue
            # Ranking uses the full indexed passage; model input receives a
            # bounded, query-centered original excerpt with exact continuation.
            begin, end = item["char_start"], item["char_end"]
            region = source["content"][begin:end].lower()
            hits = [
                region.find(token)
                for token in terms(arguments["query"])
                if len(token) > 1 and token in region
            ]
            if hits:
                begin += max(0, min(hits) - 60)
            matches.append(
                _evidence(conn, execution.auth, str(cursor), source, begin, min(240, end - begin))
            )
        next_cursor = None
        if offset < len(accepted):
            next_cursor = uid()
            conn.execute(
                "INSERT INTO history_queries(query_id,user_id,query_plan,scope_snapshot,status,retrieval_mode,profile_id,coverage,candidates,timings,pagination) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}',%s)",
                (
                    next_cursor,
                    execution.auth.user_id,
                    jsonb(row["query_plan"]),
                    jsonb(row["scope_snapshot"]),
                    row["status"],
                    row["retrieval_mode"],
                    row["profile_id"],
                    jsonb(row["coverage"]),
                    jsonb(row["candidates"]),
                    jsonb({**row["pagination"], "offset": offset}),
                ),
            )
        partial = row["status"] == "partial" or bool(invalidated)
        return {
            "kind": "conversation_search",
            "query_id": str(cursor),
            "matches": matches,
            "next_cursor": next_cursor,
            "status": "partial" if partial else "ok" if matches else "not_found",
            "mode": "semantic" if arguments["mode"] == "semantic" else "hybrid",
            "retrieval_mode": "ranked",
            "semantic_search": True,
            "historical_only": True,
            "coverage": {**row["coverage"], "invalidated_on_page": invalidated},
            "candidate_limit_reached": row["pagination"]["limit_reached"],
            "degraded_reason": "index_incomplete_or_invalidated" if partial else None,
        }


def select_candidates(valid, selected_policy):
    accepted = []
    for item in valid:
        supported = bool(
            selected_policy
            and item.get("dense_score", -2) >= selected_policy["dense_min"]
            and item.get("rerank_score", float("-inf")) >= selected_policy["rerank_min"]
        )
        if item.get("lexical_rank") or supported:
            accepted.append(item)
    accepted.sort(
        key=lambda c: (
            not bool(c.get("lexical_rank")),
            -c.get("rerank_score", -1e9),
            -c["rrf"],
            c["source_id"],
        )
    )
    return accepted
