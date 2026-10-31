"""History search locates source evidence; it never restores formal memory state."""

import re
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from uuid import UUID

from app.errors import AppError
from app.evidence_selection import load_policy
from app.identity import assert_access, owned
from app.knowledge_retrieval import fuse
from app.retrieval_engine import terms, vector_literal
from app.sources import read_source
from app.values import digest, jsonb, safe_json, uid

VERSION = "history-passages-v1"
CST = timezone(timedelta(hours=8))


@lru_cache(maxsize=4)
def local_engine(directory):
    from app.retrieval_engine import LocalRetrieval

    return LocalRetrieval(directory)


def date_bound(value):
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            result = result.replace(tzinfo=CST)
        if result.tzinfo is None:
            raise ValueError
        return result
    except (TypeError, ValueError) as exc:
        raise AppError("INVALID_ARGUMENT", "历史日期须为ISO日期或带时区的时间。", 422) from exc


def scope_sql(auth, company, before, begin, end, entity, *, semantic=True, company_ids=None):
    clauses = [
        "s.user_id=%s",
        "s.recall_state='active'",
        "s.source_seq<%s",
        "NOT EXISTS(SELECT 1 FROM source_scopes x LEFT JOIN user_company_grants g ON g.company_id=x.company_id AND g.user_id=s.user_id WHERE x.source_id=s.source_id AND g.can_read IS DISTINCT FROM true)",
    ]
    params = [auth.user_id, before]
    companies = company_ids or ([company] if company else [])
    if companies:
        clauses.append(
            "(s.scope_kind='user_private' OR (EXISTS(SELECT 1 FROM source_scopes x WHERE x.source_id=s.source_id AND x.company_id=ANY(%s::text[])) AND NOT EXISTS(SELECT 1 FROM source_scopes x WHERE x.source_id=s.source_id AND NOT(x.company_id=ANY(%s::text[])))))"
        )
        params.extend([companies, companies])
    else:
        clauses.append("s.scope_kind='user_private'")
    if begin:
        clauses.append("s.created_at>=%s")
        params.append(begin)
    if end:
        clauses.append("s.created_at<%s")
        params.append(end)
    if entity:
        clauses.append("s.content ~* %s")
        params.append(r"(?<![A-Za-z0-9])" + re.escape(entity) + r"(?![A-Za-z0-9])")
    if semantic:
        clauses.append(
            "NOT EXISTS(SELECT 1 FROM history_jobs j WHERE j.source_id=s.source_id AND j.state='skipped')"
        )
    return " AND ".join(clauses), params


def validate_source_scope(conn, auth, source, companies):
    for company in companies:
        assert_access(conn, auth, company)
    scopes = [
        r["company_id"]
        for r in conn.execute(
            "SELECT company_id FROM source_scopes WHERE source_id=%s ORDER BY company_id",
            (source["source_id"],),
        ).fetchall()
    ]
    if not set(scopes).issubset(companies):
        raise AppError("SOURCE_INVALIDATED", "历史来源超出本轮明确企业范围。", 409)
    return scopes


def read_evidence_in(conn, auth, citation_id):
    row = conn.execute(
        "SELECT e.*,q.company_id,q.scope_snapshot FROM history_evidence e JOIN history_queries q USING(query_id) WHERE e.citation_id=%s AND q.user_id=%s",
        (citation_id, auth.user_id),
    ).fetchone()
    if not row:
        raise AppError("SOURCE_INVALIDATED", "历史引用不存在或不可访问。", 409)
    source = read_source(conn, auth, str(row["source_id"]), company_id=row["company_id"])
    companies = row["scope_snapshot"].get(
        "company_ids", [row["company_id"]] if row["company_id"] else []
    )
    scopes = validate_source_scope(conn, auth, source, companies)
    if (
        source["revision"] != row["source_revision"]
        or source["source_seq"] >= row["scope_snapshot"]["before_source_seq"]
        or source["content"][row["char_start"] : row["char_end"]] != row["excerpt"]
        or digest(row["excerpt"]) != row["excerpt_hash"]
    ):
        raise AppError("SOURCE_INVALIDATED", "历史引用与当前来源修订不一致。", 409)
    if row["company_id"]:
        assert_access(conn, auth, row["company_id"])
    return safe_json(
        {
            "kind": "history_evidence",
            "citation_id": citation_id,
            "query_id": row["query_id"],
            "source_id": source["source_id"],
            "revision": source["revision"],
            "company_id": row["company_id"],
            "company_ids": scopes,
            "role": source["role"],
            "source_kind": source["kind"],
            "created_at": source["created_at"],
            "source_seq": source["source_seq"],
            "excerpt": row["excerpt"],
            "content": row["excerpt"],
            "excerpt_hash": row["excerpt_hash"],
            "locator": {"char_start": row["char_start"], "char_end": row["char_end"]},
            "excerpt_truncated": row["char_start"] != 0
            or row["char_end"] != len(source["content"]),
            "total_characters": len(source["content"]),
            "next_char_start": row["char_end"]
            if row["char_end"] < len(source["content"])
            else None,
            "historical_only": True,
        }
    )


def validate_query_in(conn, auth, query_id):
    query = conn.execute(
        "SELECT * FROM history_queries WHERE query_id=%s AND user_id=%s", (query_id, auth.user_id)
    ).fetchone()
    if not query:
        raise AppError("SOURCE_INVALIDATED", "历史查询来源不可访问。", 409)
    if query["company_id"]:
        assert_access(conn, auth, query["company_id"])
    for company in query["scope_snapshot"].get("company_ids", []):
        assert_access(conn, auth, company)
    epoch = conn.execute(
        "SELECT memory_epoch FROM users WHERE user_id=%s", (auth.user_id,)
    ).fetchone()["memory_epoch"]
    if epoch != query["scope_snapshot"]["memory_epoch"]:
        raise AppError("STALE_CONTEXT", "历史查询后记忆已更正，请重新检索。", 409)
    for row in conn.execute(
        "SELECT citation_id FROM history_evidence WHERE query_id=%s", (query_id,)
    ).fetchall():
        read_evidence_in(conn, auth, str(row["citation_id"]))


def navigation_in(conn, auth, session_id, selected_company, epoch, before):
    """Only IDs from completed replies; no historical body becomes formal state."""
    page, citations = None, []
    published = conn.execute(
        "SELECT turn_id,final_cards FROM turns WHERE user_id=%s AND session_id=%s AND status='completed' AND source_seq<%s "
        'AND final_cards @> \'[{"kind":"history_page"}]\'::jsonb ORDER BY source_seq DESC LIMIT 1',
        (auth.user_id, session_id, before),
    ).fetchone()
    marker = (
        next((c for c in reversed(published["final_cards"]) if c["kind"] == "history_page"), None)
        if published
        else None
    )
    query = (
        conn.execute(
            "SELECT * FROM history_queries WHERE query_id=%s AND user_id=%s AND scope_snapshot->>'session_id'=%s AND scope_snapshot->>'origin_turn_id'=%s",
            (marker["query_id"], auth.user_id, session_id, str(published["turn_id"])),
        ).fetchone()
        if marker
        else None
    )
    if (
        query
        and query["scope_snapshot"]["memory_epoch"] == epoch
        and query["scope_snapshot"].get("session_company_id") == selected_company
    ):
        try:
            for company in query["scope_snapshot"].get("company_ids", []):
                assert_access(conn, auth, company)
            args = {k: v for k, v in query["query_plan"].items() if v is not None}
            if args.get("company_id"):
                args.pop("company_ids", None)
            args["cursor"] = str(query["query_id"])
            page = {"arguments": args, "has_more": query["pagination"]["has_more"]}
        except AppError:
            pass
    last = conn.execute(
        "SELECT final_cards FROM turns WHERE user_id=%s AND session_id=%s AND status='completed' "
        'AND source_seq<%s AND final_cards @> \'[{"kind":"history_quote"}]\'::jsonb ORDER BY source_seq DESC LIMIT 1',
        (auth.user_id, session_id, before),
    ).fetchone()
    seen = set()
    for card in last["final_cards"] if last else []:
        if card["kind"] != "history_quote" or card["citation_id"] in seen:
            continue
        seen.add(card["citation_id"])
        try:
            evidence = read_evidence_in(conn, auth, card["citation_id"])
            if evidence["company_ids"] and selected_company not in evidence["company_ids"]:
                continue
            citations.append(
                {
                    k: evidence[k]
                    for k in (
                        "citation_id",
                        "source_id",
                        "role",
                        "created_at",
                        "company_ids",
                        "next_char_start",
                        "total_characters",
                    )
                }
            )
        except AppError:
            continue
        if len(citations) == 5:
            break
    return (
        {
            "page": page,
            "citations": citations,
            "instruction": "仅用于用户要求继续时定位。先调用search_history续页或read_history续段，不能仅凭导航引用正文；分页保持arguments中的完整范围。",
        }
        if page or citations
        else None
    )


def continuation_in(conn, execution):
    """Resolve explicit page controls against the last *published* page.

    This narrow control grammar is not general intent recognition. An explicit
    change of dates/company is a new search and must not inherit an old cursor.
    """
    source = read_source(conn, execution.auth, execution.source_id, allow_suppressed=True)
    clauses = re.split(r"[，,。；;！!\n]", source["content"])
    if not any(
        re.fullmatch(r"(?:请|再|继续|接着|往后|翻到|给我|查看|看|翻)*(?:下一页|下页)", c.strip())
        for c in clauses
    ):
        return None
    if re.search(r"\d{4}\s*(?:年|[-/])\s*\d{1,2}|\d{1,2}\s*月", source["content"]):
        return None
    session = owned(conn, "sessions", "session_id", execution.session_id, execution.auth)
    epoch = conn.execute(
        "SELECT memory_epoch FROM users WHERE user_id=%s", (execution.auth.user_id,)
    ).fetchone()["memory_epoch"]
    navigation = navigation_in(
        conn,
        execution.auth,
        execution.session_id,
        session["selected_company_id"],
        epoch,
        execution.source_seq,
    )
    page = (navigation or {}).get("page")
    if not page:
        published = conn.execute(
            "SELECT 1 FROM turns WHERE user_id=%s AND session_id=%s AND status='completed' AND source_seq<%s "
            'AND final_cards @> \'[{"kind":"history_page"}]\'::jsonb LIMIT 1',
            (execution.auth.user_id, execution.session_id, execution.source_seq),
        ).fetchone()
        if published:
            raise AppError(
                "SOURCE_INVALIDATED",
                "历史导航已因范围、记忆或权限变化失效，请明确发起新的历史查找。",
                409,
            )
        return None
    from app.turns import mentioned_companies

    companies = mentioned_companies(conn, source["content"])
    expected = page["arguments"]
    if companies and set(companies) != set(
        expected.get("company_ids") or [expected.get("company_id")]
    ):
        return None
    return expected


class HistoryRetrieval:
    def __init__(self, db, settings, engine, *, selection_policy=None):
        self.db, self.settings, self.engine = db, settings, engine
        self.selection_policy = selection_policy

    def read(self, auth, citation_id, *, char_start=None):
        with self.db.transaction(readonly=True) as conn:
            original = read_evidence_in(conn, auth, citation_id)
            if char_start is None:
                return original
            source = read_source(
                conn, auth, original["source_id"], company_id=original["company_id"]
            )
            if not isinstance(char_start, int) or not 0 <= char_start < len(source["content"]):
                raise AppError("INVALID_ARGUMENT", "历史消息字符起点超出原文范围。", 422)
            query = conn.execute(
                "SELECT * FROM history_queries WHERE query_id=%s", (original["query_id"],)
            ).fetchone()
            epoch = conn.execute(
                "SELECT memory_epoch FROM users WHERE user_id=%s", (auth.user_id,)
            ).fetchone()["memory_epoch"]
        end = min(char_start + 1200, len(source["content"]))
        excerpt = source["content"][char_start:end]
        candidate = {
            **source,
            "chunk_id": str(source["source_id"]),
            "source_revision": source["revision"],
            "char_start": char_start,
            "char_end": end,
            "excerpt": excerpt,
            "excerpt_hash": digest(excerpt),
        }
        plan = {
            "company_id": original["company_id"],
            "query": "",
            "origin_citation_id": citation_id,
            "char_start": char_start,
        }
        snapshot = {**query["scope_snapshot"], "memory_epoch": epoch}
        result = self._persist(
            auth,
            plan,
            snapshot,
            [candidate],
            {"eligible": 1, "ready": 1, "pending": 0, "coverage_kind": "source_excerpt_only"},
            "source_read",
            "ok",
            query["profile_id"],
            None,
            time.monotonic(),
        )
        return result["evidence"][0]

    def search(
        self,
        auth,
        *,
        company_id=None,
        company_ids=None,
        query="",
        entity=None,
        date_from=None,
        date_to=None,
        mode="hybrid",
        cursor=None,
        before_source_seq=None,
        session_id=None,
        origin_turn_id=None,
    ):
        started = time.monotonic()
        query = query.strip()
        companies = sorted(set(company_ids or []))
        if len(companies) > 4 or (company_id and companies):
            raise AppError(
                "INVALID_ARGUMENT", "company_id与company_ids互斥，联合企业最多4个。", 422
            )
        if company_id:
            companies = [company_id]
        begin, end = date_bound(date_from), date_bound(date_to)
        if len(query) > 2000 or mode not in ("hybrid", "lexical", "date"):
            raise AppError("INVALID_ARGUMENT", "历史查询文字或模式无效。", 422)
        if entity and not re.fullmatch(r"[FRC]\d{3,12}", entity):
            raise AppError("INVALID_ARGUMENT", "请使用准确业务编号。", 422)
        if entity and entity.startswith("C"):
            if entity not in companies:
                raise AppError("INVALID_ARGUMENT", "企业编号与历史范围不一致。", 422)
            entity = None
        if not query and not entity:
            mode = "date"
        if (begin and end and begin >= end) or (
            mode == "date" and (not begin or not end or end - begin > timedelta(days=31))
        ):
            raise AppError(
                "INVALID_ARGUMENT", "日期浏览须提供至多31天的起止窗口，结束时间不包含。", 422
            )
        plan = {
            "query": query,
            "entity": entity,
            "date_from": date_from,
            "date_to": date_to,
            "mode": mode,
            "company_id": company_id,
            "company_ids": companies,
        }
        after, exhausted, session_company = None, False, None
        with self.db.transaction(readonly=True) as conn:
            user = conn.execute(
                "SELECT memory_epoch,next_source_seq FROM users WHERE user_id=%s", (auth.user_id,)
            ).fetchone()
            for company in companies:
                assert_access(conn, auth, company)
            if session_id:
                session_company = owned(conn, "sessions", "session_id", session_id, auth)[
                    "selected_company_id"
                ]
            if origin_turn_id:
                turn = owned(conn, "turns", "turn_id", origin_turn_id, auth)
                if str(turn["session_id"]) != session_id:
                    raise AppError("INVALID_ARGUMENT", "历史导航轮次与会话不一致。", 422)
            before = min(
                before_source_seq or user["next_source_seq"] + 1, user["next_source_seq"] + 1
            )
            if cursor:
                try:
                    UUID(cursor)
                except ValueError as exc:
                    raise AppError("INVALID_ARGUMENT", "历史分页标识无效。", 422) from exc
                previous = conn.execute(
                    "SELECT * FROM history_queries WHERE query_id=%s AND user_id=%s",
                    (cursor, auth.user_id),
                ).fetchone()
                if (
                    not previous
                    or previous["query_plan"] != plan
                    or previous["scope_snapshot"].get("session_id") != session_id
                ):
                    raise AppError("INVALID_ARGUMENT", "历史分页条件已改变。", 422)
                if previous["scope_snapshot"]["memory_epoch"] != user["memory_epoch"]:
                    raise AppError("STALE_CONTEXT", "分页期间记忆已变化，请重新查找。", 409)
                before = min(before, previous["scope_snapshot"]["before_source_seq"])
                after = previous["pagination"]["after"]
                exhausted = not previous["pagination"]["has_more"]
            snapshot = {
                "user_id": auth.user_id,
                "company_id": company_id,
                "company_ids": companies,
                "memory_epoch": user["memory_epoch"],
                "before_source_seq": before,
                "session_id": session_id,
                "session_company_id": session_company,
                "origin_turn_id": origin_turn_id,
            }
        predicate, params = scope_sql(
            auth,
            company_id,
            before,
            begin,
            end,
            entity,
            semantic=mode != "date",
            company_ids=companies,
        )
        if mode == "date":
            if exhausted:
                return self._persist(
                    auth,
                    plan,
                    snapshot,
                    [],
                    {
                        "eligible": 0,
                        "ready": 0,
                        "pending": 0,
                        "coverage_kind": "date_page_only",
                        "page_exhausted": True,
                    },
                    "date",
                    "not_found",
                    None,
                    None,
                    started,
                )
            if after:
                predicate += " AND (s.source_seq,s.source_id)<(%s,%s::uuid)"
                params.extend(after)
            with self.db.transaction(readonly=True) as conn:
                rows = conn.execute(
                    "SELECT s.* FROM sources s WHERE "
                    + predicate
                    + " ORDER BY s.source_seq DESC,s.source_id DESC LIMIT 6",
                    params,
                ).fetchall()
            candidates = [self._snippet(r, "") for r in rows]
            coverage = {
                "eligible": len(rows),
                "ready": len(rows),
                "pending": 0,
                "coverage_kind": "date_page_only",
                "selection_calibrated": False,
            }
            return self._persist(
                auth,
                plan,
                snapshot,
                candidates,
                coverage,
                "date",
                "ok",
                None,
                None,
                started,
                more=len(rows) > 5,
            )
        if cursor:
            raise AppError("INVALID_ARGUMENT", "语义检索请缩小条件；游标只用于日期浏览。", 422)
        return self._semantic(auth, plan, snapshot, predicate, params, started)

    @staticmethod
    def _snippet(source, query):
        pos = source["content"].lower().find(query.lower()) if query else 0
        begin = max(0, pos - 120) if pos >= 0 else 0
        end = min(len(source["content"]), begin + 1200)
        return {
            **source,
            "chunk_id": str(source["source_id"]),
            "source_revision": source["revision"],
            "char_start": begin,
            "char_end": end,
            "excerpt": source["content"][begin:end],
            "excerpt_hash": digest(source["content"][begin:end]),
        }

    def _semantic(self, auth, plan, snapshot, predicate, params, started):
        profile, vector, policy, reason = None, None, None, None
        try:
            if self.settings.history_enabled:
                profile, _ = self.engine.profile()
        except (AppError, ImportError, OSError, RuntimeError, ValueError):
            reason = "DEPENDENCY_UNAVAILABLE"
        with self.db.transaction(readonly=True) as conn:
            eligible = conn.execute(
                "SELECT count(*) AS n FROM sources s WHERE " + predicate, params
            ).fetchone()["n"]
            ready = (
                conn.execute(
                    "SELECT count(*) AS n FROM sources s JOIN history_jobs j USING(source_id) WHERE "
                    + predicate
                    + " AND j.source_revision=s.revision AND j.state='done' AND j.profile_id=%s",
                    [*params, profile],
                ).fetchone()["n"]
                if profile
                else 0
            )
            vector_table = conn.execute(
                "SELECT to_regclass('public.history_embeddings') AS t"
            ).fetchone()["t"]
            oldest_pending = conn.execute(
                "SELECT min(s.created_at) AS at FROM sources s WHERE "
                + predicate
                + " AND NOT EXISTS(SELECT 1 FROM history_jobs j WHERE j.source_id=s.source_id AND j.source_revision=s.revision AND j.state='done' AND j.profile_id=%s)",
                [*params, profile],
            ).fetchone()["at"]
        coverage = {
            "eligible": eligible,
            "ready": ready,
            "pending": eligible - ready,
            "selection_calibrated": False,
            "oldest_pending_at": oldest_pending,
            "index_lag_seconds": max(0, int((datetime.now(CST) - oldest_pending).total_seconds()))
            if oldest_pending
            else 0,
        }
        if eligible and ready and profile and vector_table and plan["mode"] == "hybrid":
            try:
                vector = vector_literal(self.engine.embed([plan["query"] or plan["entity"]])[0])
            except (AppError, ImportError, OSError, RuntimeError, ValueError):
                reason = "DEPENDENCY_UNAVAILABLE"
        try:
            words = terms(plan["query"] or plan["entity"] or "")[:32]
        except ImportError:
            words = [plan["query"] or plan["entity"] or ""]
            reason = "DEPENDENCY_UNAVAILABLE"
        lexical, dense = [], []
        fields = "s.*,p.passage_id AS chunk_id,p.source_revision,p.char_start,p.char_end,p.excerpt,p.excerpt_hash"
        join = " FROM sources s JOIN history_passages p ON p.source_id=s.source_id AND p.source_revision=s.revision AND p.profile_id=%s "
        with self.db.transaction(readonly=True) as conn:
            if words and profile:
                tsquery = " OR ".join('"' + w + '"' for w in words)
                lexical = conn.execute(
                    "SELECT "
                    + fields
                    + ",ts_rank_cd(p.lexical_index,websearch_to_tsquery('simple',%s)) AS lexical_score"
                    + join
                    + "WHERE "
                    + predicate
                    + " AND p.lexical_index@@websearch_to_tsquery('simple',%s) ORDER BY lexical_score DESC,p.passage_id LIMIT 30",
                    [tsquery, profile, *params, tsquery],
                ).fetchall()
            # The original synchronous text index remains a fallback for pending sources.
            fallback = conn.execute(
                "SELECT s.* FROM sources s JOIN history_index h USING(source_id) WHERE "
                + predicate
                + " AND h.source_revision=s.revision AND (position(lower(%s) in lower(h.searchable_text))>0 OR EXISTS(SELECT 1 FROM unnest(%s::text[]) word WHERE position(word in lower(h.searchable_text))>0)) AND NOT EXISTS(SELECT 1 FROM history_passages p WHERE p.source_id=s.source_id AND p.source_revision=s.revision AND p.profile_id=%s) ORDER BY s.source_seq DESC,s.source_id DESC LIMIT 30",
                [*params, plan["query"] or plan["entity"] or "", words, profile],
            ).fetchall()
            lexical.extend(
                self._snippet(r, plan["query"] or plan["entity"] or "") for r in fallback
            )
            lexical = lexical[:30]
            if vector:
                dense = conn.execute(
                    "SELECT "
                    + fields
                    + ",v.embedding<=>%s::vector AS distance"
                    + join
                    + "JOIN history_embeddings v ON v.passage_id=p.passage_id WHERE "
                    + predicate
                    + " ORDER BY distance,p.passage_id LIMIT 30",
                    [vector, profile, *params],
                ).fetchall()
        candidates = fuse(dense, lexical)[:20]
        coverage["candidate_channels"] = {
            "lexical": [str(r["chunk_id"]) for r in lexical],
            "dense": [str(r["chunk_id"]) for r in dense],
            "rrf": [str(r["chunk_id"]) for r in candidates],
        }
        mode = "hybrid_unranked" if vector else "lexical_only"
        if vector and candidates:
            try:
                scores = self.engine.rerank(
                    plan["query"] or plan["entity"], [r["excerpt"] for r in candidates]
                )
                candidates = sorted(
                    [{**r, "rerank_logit": v} for r, v in zip(candidates, scores, strict=True)],
                    key=lambda r: (-r["rerank_logit"], str(r["chunk_id"])),
                )
                mode = "hybrid"
            except (AppError, ImportError, OSError, RuntimeError, ValueError):
                reason = "DEPENDENCY_UNAVAILABLE"
        policy, policy_error = (
            load_policy(self.settings.history_selection_policy, profile, version=VERSION)
            if self.selection_policy is None
            else (self.selection_policy or None, "SELECTION_POLICY_NOT_CALIBRATED")
        )
        calibrated = bool(policy and mode == "hybrid" and policy["profile_id"] == profile)
        coverage.update(
            selection_calibrated=calibrated,
            selection_policy_id=policy["policy_id"] if calibrated else None,
            selection_cutoff=policy["cutoff"] if calibrated else None,
        )
        seeds = [r for r in candidates if not calibrated or r["rerank_logit"] >= policy["cutoff"]]
        status = "ok" if seeds else "not_found"
        if coverage["pending"] or (eligible and (mode != "hybrid" or not calibrated)):
            status = "partial"
            reason = reason or ("HISTORY_INDEX_PENDING" if coverage["pending"] else policy_error)
        elif not seeds and candidates:
            reason = "BELOW_CALIBRATED_RELEVANCE"
        return self._persist(
            auth,
            plan,
            snapshot,
            seeds,
            coverage,
            mode,
            status,
            profile,
            reason,
            started,
            candidates=candidates,
        )

    def _persist(
        self,
        auth,
        plan,
        snapshot,
        selected,
        coverage,
        mode,
        status,
        profile,
        reason,
        started,
        *,
        candidates=None,
        more=False,
    ):
        query_id, evidence, consumed, seen = uid(), [], 0, set()
        pagination = {"has_more": more, "after": None}
        with self.db.transaction() as conn:
            user = conn.execute(
                "SELECT memory_epoch FROM users WHERE user_id=%s FOR SHARE", (auth.user_id,)
            ).fetchone()
            if user["memory_epoch"] != snapshot["memory_epoch"]:
                raise AppError("STALE_CONTEXT", "检索期间记忆已变化，请重试。", 409)
            if plan["company_id"]:
                assert_access(conn, auth, plan["company_id"])
            for company in snapshot.get("company_ids", []):
                assert_access(conn, auth, company)
            for candidate in selected:
                if len(evidence) >= 5:
                    if mode == "date":
                        pagination["has_more"] = True
                    break
                key = (str(candidate["source_id"]), candidate["char_start"], candidate["char_end"])
                if key in seen:
                    continue
                seen.add(key)
                try:
                    source = read_source(
                        conn, auth, str(candidate["source_id"]), company_id=plan["company_id"]
                    )
                    validate_source_scope(
                        conn,
                        auth,
                        source,
                        snapshot.get(
                            "company_ids", [plan["company_id"]] if plan["company_id"] else []
                        ),
                    )
                except AppError as error:
                    if error.code in ("SOURCE_INVALIDATED", "STALE_CONTEXT", "ACCESS_DENIED"):
                        if mode == "date":
                            pagination["after"] = [
                                candidate["source_seq"],
                                str(candidate["source_id"]),
                            ]
                        continue
                    raise
                if (
                    source["revision"] != candidate["source_revision"]
                    or source["source_seq"] >= snapshot["before_source_seq"]
                    or source["content"][candidate["char_start"] : candidate["char_end"]]
                    != candidate["excerpt"]
                    or digest(candidate["excerpt"]) != candidate["excerpt_hash"]
                ):
                    raise AppError("SOURCE_INVALIDATED", "历史索引与原文不一致，请重建索引。", 409)
                cost = (len(candidate["excerpt"].encode()) + 1) // 2 + 80
                if consumed + cost > 3000:
                    status, reason = "partial", "EVIDENCE_BUDGET_INSUFFICIENT"
                    if mode == "date":
                        pagination["has_more"] = True
                    break
                consumed += cost
                evidence.append({**candidate, "citation_id": uid()})
                if mode == "date":
                    pagination["after"] = [source["source_seq"], str(source["source_id"])]
            if mode == "date" and not evidence and not more:
                status = "not_found"
            coverage.update(evidence_tokens=consumed, evidence_budget=3000)
            trace = [
                {
                    k: r[k]
                    for k in (
                        "chunk_id",
                        "source_id",
                        "source_revision",
                        "lexical_rank",
                        "dense_rank",
                        "rrf_score",
                        "rerank_logit",
                    )
                    if k in r
                }
                for r in (candidates or selected)
            ]
            timings = {"total_ms": round((time.monotonic() - started) * 1000)}
            conn.execute(
                "INSERT INTO history_queries(query_id,user_id,company_id,query_plan,scope_snapshot,status,retrieval_mode,profile_id,coverage,candidates,timings,pagination,degraded_reason) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    query_id,
                    auth.user_id,
                    plan["company_id"],
                    jsonb(plan),
                    jsonb(snapshot),
                    status,
                    mode,
                    profile,
                    jsonb(coverage),
                    jsonb(trace),
                    jsonb(timings),
                    jsonb(pagination),
                    reason,
                ),
            )
            result = []
            for rank, item in enumerate(evidence, 1):
                conn.execute(
                    "INSERT INTO history_evidence(citation_id,query_id,source_id,source_revision,char_start,char_end,excerpt,excerpt_hash,rank) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        item["citation_id"],
                        query_id,
                        item["source_id"],
                        item["source_revision"],
                        item["char_start"],
                        item["char_end"],
                        item["excerpt"],
                        item["excerpt_hash"],
                        rank,
                    ),
                )
                result.append(read_evidence_in(conn, auth, str(item["citation_id"])))
        return safe_json(
            {
                "kind": "history_search",
                "corpus": "history",
                "query_id": query_id,
                "profile_id": profile,
                "status": status,
                "retrieval_mode": mode,
                "scope_snapshot": snapshot,
                "raw_query": plan["query"],
                "rewritten_query": None,
                "coverage": {k: v for k, v in coverage.items() if k != "candidate_channels"},
                "evidence": result,
                "matches": result,
                "not_found": not result,
                "has_more": pagination["has_more"],
                "next_cursor": query_id if pagination["has_more"] else None,
                "timings": timings,
                "degraded_reason": reason,
                "answer_support": "not_evaluated",
            }
        )
