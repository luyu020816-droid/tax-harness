"""Permission-filtered retrieval and durable, revalidated evidence."""

import re
import time
from datetime import datetime, timedelta, timezone

from app.errors import AppError
from app.evidence_selection import choose_groups, load_policy
from app.identity import assert_access, owned
from app.knowledge import document_in
from app.knowledge_parser import text_hash
from app.retrieval_engine import terms, vector_literal
from app.values import jsonb, month_bounds, safe_json, uid


def fuse(dense, lexical):
    scores, rows = {}, {}
    for name, channel in (("dense", dense), ("lexical", lexical)):
        for rank, row in enumerate(channel, 1):
            key = str(row["chunk_id"])
            rows.setdefault(key, {}).update(row)
            rows[key][name + "_rank"] = rank
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
    return [
        {**rows[key], "rrf_score": scores[key]}
        for key in sorted(scores, key=lambda k: (-scores[k], k))
    ]


def read_evidence_in(conn, auth, citation_id, *, lock=False):
    evidence = conn.execute(
        "SELECT e.*,q.user_id,q.profile_id,q.scope_snapshot,d.title,d.scope,d.owner_id,d.company_id,d.status,d.active_version_id,d.revision AS current_revision, "
        "v.edition,v.state,v.effective_from,v.effective_to,c.content,c.content_hash AS current_hash,p.status AS profile_status "
        "FROM retrieval_evidence e JOIN retrieval_queries q USING(query_id) JOIN knowledge_documents d USING(document_id) "
        "JOIN knowledge_versions v USING(version_id) JOIN knowledge_chunks c USING(chunk_id) JOIN retrieval_profiles p ON p.profile_id=q.profile_id "
        "WHERE e.citation_id=%s AND q.user_id=%s" + (" FOR SHARE OF d,v,p" if lock else ""),
        (citation_id, auth.user_id),
    ).fetchone()
    if not evidence:
        raise AppError("RESOURCE_NOT_FOUND", "引用不存在或不可访问。", 404)
    document_in(conn, auth, str(evidence["document_id"]))
    if (
        evidence["status"] != "active"
        or evidence["state"] != "ready"
        or evidence["profile_status"] != "ready"
        or evidence["active_version_id"] != evidence["version_id"]
        or evidence["document_revision"] != evidence["current_revision"]
        or evidence["excerpt_hash"] != evidence["current_hash"]
        or text_hash(evidence["excerpt"]) != evidence["excerpt_hash"]
        or evidence["excerpt"] != evidence["content"]
    ):
        raise AppError(
            "SOURCE_INVALIDATED", "资料引用已更新、停用或完整性校验失败，请重新检索。", 409
        )
    return safe_json(
        {
            k: evidence[k]
            for k in (
                "citation_id",
                "query_id",
                "document_id",
                "document_revision",
                "version_id",
                "chunk_id",
                "edition",
                "title",
                "company_id",
                "scope",
                "excerpt",
                "excerpt_hash",
                "locator",
                "profile_id",
                "rank",
            )
        }
    )


def read_evidence_group_in(conn, auth, citation_id, *, lock=False):
    main = read_evidence_in(conn, auth, citation_id, lock=lock)
    query = conn.execute(
        "SELECT coverage FROM retrieval_queries WHERE query_id=%s AND user_id=%s",
        (main["query_id"], auth.user_id),
    ).fetchone()
    group = next(
        (
            g
            for g in query["coverage"].get("evidence_groups", [])
            if g["selected"] and main["chunk_id"] in g["chunk_ids"]
        ),
        None,
    )
    if not group:
        # Original M3 citations stay readable; no invented historical group.
        return [main]
    if not group.get("chunk_ids_complete", True):
        raise AppError("SOURCE_INVALIDATED", "引用缺少完整条款关联，请重新检索。", 409)
    rows = conn.execute(
        "SELECT citation_id FROM retrieval_evidence WHERE query_id=%s AND chunk_id=ANY(%s::uuid[]) ORDER BY rank",
        (main["query_id"], group["chunk_ids"]),
    ).fetchall()
    if len(rows) != group.get("chunk_count", len(group["chunk_ids"])):
        raise AppError("SOURCE_INVALIDATED", "必要条款的引用集合不完整，请重新检索。", 409)
    return [read_evidence_in(conn, auth, str(row["citation_id"]), lock=lock) for row in rows]


class KnowledgeRetrieval:
    def __init__(self, db, settings, engine, *, selection_policy=None):
        self.db, self.settings, self.engine = db, settings, engine
        self.selection_policy = selection_policy

    def ready(self, *, validate=True):
        from app.ocr_status import options

        ocr = options(self.settings, validate=validate)
        installed = self.engine.installed()
        profile_id = None
        if validate and installed["dependencies"] and installed["manifest_present"]:
            try:
                profile_id, _ = self.engine.profile()
            except AppError:
                pass
        with self.db.transaction(readonly=True) as conn:
            vector = bool(
                conn.execute("SELECT to_regclass('public.retrieval_embeddings') AS t").fetchone()[
                    "t"
                ]
            )
            profiles = conn.execute(
                "SELECT count(*) AS n FROM retrieval_profiles WHERE status='ready'"
            ).fetchone()["n"]
            current = (
                profile_id
                and conn.execute(
                    "SELECT 1 FROM retrieval_profiles WHERE profile_id=%s AND status='ready'",
                    (profile_id,),
                ).fetchone()
            )
        policy, policy_reason = (
            load_policy(self.settings.knowledge_selection_policy, profile_id)
            if validate and profile_id
            else (None, "not_checked")
        )
        return {
            "enabled": self.settings.knowledge_enabled,
            "parser": installed["dependencies"],
            "vector": vector,
            "embedding": installed,
            "validated_profiles": profiles,
            "selection_policy_id": policy["policy_id"] if policy else None,
            "selection_calibrated": bool(policy) if validate else None,
            "selection_reason": policy_reason,
            "ocr": bool(ocr),
            "ocr_details": {
                "provider": "PaddleOCR PP-StructureV3",
                "ready": bool(ocr),
                "max_pages": self.settings.knowledge_ocr_max_pages,
                "max_pixels": self.settings.knowledge_ocr_max_pixels,
                "timeout_seconds": self.settings.knowledge_ocr_timeout,
            },
            "ready": bool(self.settings.knowledge_enabled and vector and current and policy)
            if validate
            else None,
            "validation": "checked" if validate else "not_checked",
        }

    def read(self, auth, citation_id):
        with self.db.transaction(readonly=True) as conn:
            group = read_evidence_group_in(conn, auth, citation_id)
            evidence = next(e for e in group if e["citation_id"] == citation_id)
            epoch = conn.execute(
                "SELECT knowledge_epoch FROM company_heads WHERE company_id=%s",
                (evidence["company_id"],),
            ).fetchone()["knowledge_epoch"]
            return {
                "kind": "knowledge_evidence",
                **evidence,
                "validated_knowledge_epoch": epoch,
                "related_evidence": [e for e in group if e["citation_id"] != citation_id],
            }

    def search(
        self,
        auth,
        *,
        company_id,
        query,
        entities=None,
        document_ids=None,
        as_of=None,
        period=None,
        session_id=None,
        turn_id=None,
        rerank=True,
    ):
        if not query.strip() or len(query) > 2000:
            raise AppError("INVALID_ARGUMENT", "知识问题需为1—2000字符。", 422)
        if as_of and period:
            raise AppError("INVALID_ARGUMENT", "资料生效日期与业务月份不能同时指定。", 422)
        entities = list(dict.fromkeys(entities or []))
        if len(entities) > 8 or any(not re.fullmatch(r"[FRC]\d{3,12}", e) for e in entities):
            raise AppError("INVALID_ARGUMENT", "检索实体需为明确票号、规则号或企业号。", 422)
        if any(e.startswith("C") and e != company_id for e in entities):
            raise AppError("INVALID_ARGUMENT", "检索企业实体与当前企业范围不一致。", 422)
        entities = [e for e in entities if not e.startswith("C")]
        current_date = as_of or datetime.now(timezone(timedelta(hours=8))).date()
        start, end = month_bounds(period) if period else (current_date, current_date)
        started = time.monotonic()
        timings = {}
        with self.db.transaction(readonly=True) as conn:
            assert_access(conn, auth, company_id)
            if session_id:
                owned(conn, "sessions", "session_id", session_id, auth)
            if turn_id:
                owned(conn, "turns", "turn_id", turn_id, auth)
            snapshot = {
                "company_id": company_id,
                "start": str(start),
                "end": str(end),
                "period": period,
                "memory_epoch": conn.execute(
                    "SELECT memory_epoch FROM users WHERE user_id=%s", (auth.user_id,)
                ).fetchone()["memory_epoch"],
                "knowledge_epoch": conn.execute(
                    "SELECT knowledge_epoch FROM company_heads WHERE company_id=%s", (company_id,)
                ).fetchone()["knowledge_epoch"],
            }
        profile_id, mode, reason = None, "unavailable", None
        candidates, selected = [], []
        status, coverage = "unavailable", {"eligible": 0, "ready": 0, "pending": 0}
        try:
            if not self.settings.knowledge_enabled:
                raise AppError("DEPENDENCY_UNAVAILABLE", "知识检索未启用。", 503)
            profile_id, _ = self.engine.profile()
            with self.db.transaction(readonly=True) as conn:
                if not conn.execute(
                    "SELECT 1 FROM retrieval_profiles WHERE profile_id=%s AND status='ready'",
                    (profile_id,),
                ).fetchone():
                    profile_id = None
                    raise AppError("INDEX_NOT_READY", "当前检索profile尚无完成的真实索引。", 503)
            if self.selection_policy is None:
                policy, policy_reason = load_policy(
                    self.settings.knowledge_selection_policy, profile_id
                )
            else:
                policy = self.selection_policy or None
                policy_reason = None if policy else "SELECTION_POLICY_NOT_CALIBRATED"
                if policy and policy["profile_id"] != profile_id:
                    policy, policy_reason = None, "SELECTION_POLICY_MISMATCH"
            snapshot["selection_policy_id"] = policy["policy_id"] if policy else None
            dense_vector = None
            stage_started = time.monotonic()
            try:
                dense_vector = vector_literal(self.engine.embed([query])[0])
            except (AppError, ImportError, OSError, RuntimeError, ValueError) as exc:
                reason = exc.code if isinstance(exc, AppError) else "DEPENDENCY_UNAVAILABLE"
            timings["query_embedding_ms"] = round((time.monotonic() - stage_started) * 1000)
            stage_started = time.monotonic()
            with self.db.transaction(readonly=True) as conn:
                base, params = self._scope(
                    auth, company_id, start, end, period, entities, document_ids
                )
                coverage = conn.execute(
                    "SELECT count(*) AS eligible,count(*) FILTER(WHERE v.state='ready' AND v.profile_id=%s) AS ready "
                    "FROM knowledge_documents d LEFT JOIN knowledge_versions v ON v.version_id=d.active_version_id WHERE "
                    + base,
                    [profile_id, *params],
                ).fetchone()
                coverage["pending"] = conn.execute(
                    "SELECT count(*) AS n FROM knowledge_versions v JOIN knowledge_documents d USING(document_id) WHERE d.company_id=%s "
                    "AND d.status='active' AND (d.scope='company' OR d.owner_id=%s) AND v.state NOT IN ('ready','cancelled') "
                    "AND v.edition>COALESCE((SELECT a.edition FROM knowledge_versions a WHERE a.version_id=d.active_version_id),0) "
                    "AND (%s::uuid[] IS NULL OR d.document_id=ANY(%s::uuid[]))",
                    (company_id, auth.user_id, document_ids or None, document_ids or None),
                ).fetchone()["n"]
                base += " AND v.state='ready' AND v.profile_id=%s"
                params.append(profile_id)
                if entities:
                    base += " AND c.retrieval_text ~ %s"
                    params.append(
                        r"(?<![A-Za-z0-9])(?:"
                        + "|".join(re.escape(e) for e in entities)
                        + r")(?![A-Za-z0-9])"
                    )
                join = " FROM knowledge_chunks c JOIN knowledge_versions v USING(version_id) JOIN knowledge_documents d USING(document_id) "
                fields = "c.*,v.document_id,v.edition,d.revision AS document_revision,d.title,d.scope,d.company_id"
                keywords = terms(query)[:32]
                lexical = []
                if keywords:
                    tsquery = " OR ".join('"' + w + '"' for w in keywords)
                    lexical = conn.execute(
                        "SELECT "
                        + fields
                        + ",ts_rank_cd(c.lexical_index,websearch_to_tsquery('simple',%s)) AS lexical_score"
                        + join
                        + "WHERE "
                        + base
                        + " AND c.lexical_index@@websearch_to_tsquery('simple',%s) ORDER BY lexical_score DESC,c.chunk_id LIMIT 30",
                        [tsquery, *params, tsquery],
                    ).fetchall()
                dense = []
                if dense_vector:
                    dense = conn.execute(
                        "SELECT "
                        + fields
                        + ",e.embedding<=>%s::vector AS distance"
                        + join
                        + "JOIN retrieval_embeddings e ON e.target_id=c.chunk_id AND e.profile_id=%s WHERE "
                        + base
                        + " ORDER BY distance,c.chunk_id LIMIT 30",
                        [dense_vector, profile_id, *params],
                    ).fetchall()
                candidates = fuse(dense, lexical)[:20]
                if dense_vector and candidates:
                    distances = conn.execute(
                        "SELECT target_id,embedding<=>%s::vector AS distance FROM retrieval_embeddings WHERE profile_id=%s AND target_id=ANY(%s::uuid[])",
                        (dense_vector, profile_id, [c["chunk_id"] for c in candidates]),
                    ).fetchall()
                    lookup = {str(r["target_id"]): r["distance"] for r in distances}
                    for candidate in candidates:
                        candidate["distance"] = lookup.get(str(candidate["chunk_id"]))
                coverage["candidate_channels"] = {
                    "dense": [str(c["chunk_id"]) for c in dense],
                    "lexical": [str(c["chunk_id"]) for c in lexical],
                    "rrf": [str(c["chunk_id"]) for c in candidates],
                }
                mode = "hybrid_unranked" if dense_vector else "lexical_only"
            timings["candidate_sql_ms"] = round((time.monotonic() - stage_started) * 1000)
            stage_started = time.monotonic()
            if candidates and rerank and dense_vector:
                try:
                    scores = self.engine.rerank(query, [c["retrieval_text"] for c in candidates])
                    candidates = sorted(
                        [
                            {**c, "rerank_logit": score}
                            for c, score in zip(candidates, scores, strict=True)
                        ],
                        key=lambda c: (-c["rerank_logit"], str(c["chunk_id"])),
                    )
                    mode = "hybrid"
                except (AppError, ImportError, OSError, RuntimeError, ValueError) as exc:
                    reason = exc.code if isinstance(exc, AppError) else "DEPENDENCY_UNAVAILABLE"
            timings["rerank_ms"] = round((time.monotonic() - stage_started) * 1000)
            stage_started = time.monotonic()
            blocks_by_version, chunks_by_version = {}, {}
            with self.db.transaction(readonly=True) as conn:
                for version in dict.fromkeys(str(c["version_id"]) for c in candidates):
                    blocks_by_version[version] = conn.execute(
                        "SELECT ordinal,kind,heading_path,left(content,160) AS prefix,locator FROM knowledge_blocks WHERE version_id=%s ORDER BY ordinal",
                        (version,),
                    ).fetchall()
                    chunks_by_version[version] = conn.execute(
                        "SELECT c.chunk_id,c.ordinal,c.block_ordinals,ceil((octet_length(c.content)+octet_length(d.title)+octet_length(c.locator::text))/2.0)::integer AS estimated_tokens "
                        "FROM knowledge_chunks c JOIN knowledge_versions v USING(version_id) JOIN knowledge_documents d USING(document_id) WHERE c.version_id=%s ORDER BY c.ordinal",
                        (version,),
                    ).fetchall()
                selected_ids, selection = choose_groups(
                    candidates, blocks_by_version, chunks_by_version, policy=policy, mode=mode
                )
                coverage.update(selection)
                if selected_ids:
                    rows = conn.execute(
                        "SELECT c.*,v.document_id,v.edition,d.revision AS document_revision,d.title,d.scope,d.company_id FROM knowledge_chunks c JOIN knowledge_versions v USING(version_id) JOIN knowledge_documents d USING(document_id) WHERE c.chunk_id=ANY(%s::uuid[])",
                        (selected_ids,),
                    ).fetchall()
                    by_id = {str(r["chunk_id"]): r for r in rows}
                    selected = [by_id[key] for key in selected_ids]
            timings["evidence_selection_ms"] = round((time.monotonic() - stage_started) * 1000)
            coverage["unrepresented_entities"] = [
                e
                for e in entities
                if not any(
                    re.search(
                        r"(?<![A-Za-z0-9])" + re.escape(e) + r"(?![A-Za-z0-9])", c["retrieval_text"]
                    )
                    for c in selected
                )
            ]
            status = (
                "partial"
                if coverage["pending"]
                or coverage["ready"] < coverage["eligible"]
                or coverage["unrepresented_entities"]
                or not coverage["selection_calibrated"]
                or coverage["budget_incomplete"]
                else "ok"
            )
            if not selected:
                status = (
                    "partial"
                    if coverage["pending"]
                    or coverage["budget_incomplete"]
                    or (mode != "hybrid" and coverage["eligible"] > 0)
                    or (candidates and not coverage["selection_calibrated"])
                    else "not_found"
                )
            if coverage["budget_incomplete"]:
                reason = "EVIDENCE_BUDGET_INSUFFICIENT"
            elif not coverage["selection_calibrated"] and candidates:
                reason = reason or policy_reason or "RETRIEVAL_MODE_NOT_CALIBRATED"
            elif candidates and not selected:
                reason = "BELOW_CALIBRATED_RELEVANCE"
        except AppError as exc:
            reason = exc.code
        except (ImportError, OSError, RuntimeError, ValueError):
            reason = "DEPENDENCY_UNAVAILABLE"
        query_id = uid()
        with self.db.transaction() as conn:
            conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR SHARE", (auth.user_id,))
            head = conn.execute(
                "SELECT knowledge_epoch FROM company_heads WHERE company_id=%s FOR SHARE",
                (company_id,),
            ).fetchone()
            epoch = conn.execute(
                "SELECT memory_epoch FROM users WHERE user_id=%s", (auth.user_id,)
            ).fetchone()["memory_epoch"]
            assert_access(conn, auth, company_id)
            if (
                epoch != snapshot["memory_epoch"]
                or head["knowledge_epoch"] != snapshot["knowledge_epoch"]
            ):
                raise AppError("STALE_CONTEXT", "检索期间资料或权限发生变化，请重新检索。", 409)
            conn.execute(
                "INSERT INTO retrieval_queries(query_id,user_id,session_id,turn_id,company_id,raw_query,profile_id,status,retrieval_mode,scope_snapshot,coverage,candidates,timings,degraded_reason) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    query_id,
                    auth.user_id,
                    session_id,
                    turn_id,
                    company_id,
                    query,
                    profile_id,
                    status,
                    mode,
                    jsonb(snapshot),
                    jsonb(coverage),
                    jsonb(
                        [
                            {
                                k: c[k]
                                for k in (
                                    "chunk_id",
                                    "dense_rank",
                                    "distance",
                                    "lexical_rank",
                                    "lexical_score",
                                    "rrf_score",
                                    "rerank_logit",
                                )
                                if k in c
                            }
                            for c in candidates
                        ]
                    ),
                    jsonb({**timings, "total_ms": round((time.monotonic() - started) * 1000)}),
                    reason,
                ),
            )
            evidence = []
            for rank, candidate in enumerate(selected, 1):
                document = document_in(conn, auth, str(candidate["document_id"]), lock=True)
                if (
                    document["revision"] != candidate["document_revision"]
                    or document["active_version_id"] != candidate["version_id"]
                    or document["status"] != "active"
                ):
                    raise AppError("STALE_CONTEXT", "资料候选已失效，请重新检索。", 409)
                citation_id = uid()
                conn.execute(
                    "INSERT INTO retrieval_evidence(citation_id,query_id,document_id,document_revision,version_id,chunk_id,excerpt,excerpt_hash,locator,rank) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        citation_id,
                        query_id,
                        candidate["document_id"],
                        candidate["document_revision"],
                        candidate["version_id"],
                        candidate["chunk_id"],
                        candidate["content"],
                        candidate["content_hash"],
                        jsonb(candidate["locator"]),
                        rank,
                    ),
                )
                evidence.append(read_evidence_in(conn, auth, citation_id))
            return safe_json(
                {
                    "kind": "knowledge_search",
                    "query_id": query_id,
                    "corpus": "knowledge",
                    "status": status,
                    "retrieval_mode": mode,
                    "profile_id": profile_id,
                    "scope_snapshot": snapshot,
                    "coverage": {
                        **{
                            k: v
                            for k, v in coverage.items()
                            if k not in ("candidate_channels", "evidence_groups")
                        },
                        "evidence_groups": [
                            {
                                k: g[k]
                                for k in ("group_id", "chunk_count", "estimated_tokens", "selected")
                            }
                            for g in coverage.get("evidence_groups", [])
                        ],
                    },
                    "raw_query": query,
                    "answer_support": "not_evaluated",
                    "rewritten_query": None,
                    "evidence": evidence,
                    "degraded_reason": reason,
                    "timings": {**timings, "total_ms": round((time.monotonic() - started) * 1000)},
                    "company_id": company_id,
                }
            )

    def _scope(self, auth, company_id, start, end, period, entities, document_ids):
        base = "d.company_id=%s AND d.status='active' AND (d.scope='company' OR d.owner_id=%s) AND v.version_id=d.active_version_id "
        base += (
            "AND v.effective_from<=%s AND (v.effective_to IS NULL OR v.effective_to"
            + (">=" if period else ">")
            + "%s)"
        )
        params = [company_id, auth.user_id, start, end]
        if document_ids:
            base += " AND d.document_id=ANY(%s::uuid[])"
            params.append(document_ids)
        return base, params


def validate_retrievals(conn, auth, query_ids, *, lock=False):
    for query_id in query_ids:
        row = conn.execute(
            "SELECT * FROM retrieval_queries WHERE query_id=%s AND user_id=%s",
            (query_id, auth.user_id),
        ).fetchone()
        if not row:
            raise AppError("SOURCE_INVALIDATED", "检索记录不存在或不可访问。", 409)
        assert_access(conn, auth, row["company_id"])
        head = conn.execute(
            "SELECT knowledge_epoch FROM company_heads WHERE company_id=%s"
            + (" FOR SHARE" if lock else ""),
            (row["company_id"],),
        ).fetchone()
        if head["knowledge_epoch"] != row["scope_snapshot"]["knowledge_epoch"]:
            raise AppError("STALE_CONTEXT", "企业知识已发布或撤权，原检索不能继续答复。", 409)
        for citation in conn.execute(
            "SELECT citation_id FROM retrieval_evidence WHERE query_id=%s", (query_id,)
        ).fetchall():
            read_evidence_in(conn, auth, str(citation["citation_id"]), lock=lock)
