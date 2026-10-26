import base64
import json

from app.errors import AppError
from app.identity import Identity, assert_access, owned
from app.sources import read_source
from app.tasks import current_in
from app.values import digest, safe_json


def load_core(db, auth: Identity, session_id: str) -> dict:
    with db.transaction(readonly=True) as conn:
        session = owned(conn, "sessions", "session_id", session_id, auth)
        company = session["selected_company_id"]
        if company:
            assert_access(conn, auth, company)
        company_head = (
            conn.execute(
                "SELECT data_revision,rules_epoch FROM company_heads WHERE company_id=%s",
                (company,),
            ).fetchone()
            if company
            else None
        )
        user = conn.execute(
            "SELECT memory_epoch FROM users WHERE user_id=%s", (auth.user_id,)
        ).fetchone()
        task = current_in(conn, auth, company) if company else None
        candidates = []
        if not company:
            candidates = conn.execute(
                "SELECT t.task_id,t.company_id,t.period,t.intent,t.status,t.revision,t.generation "
                "FROM task_slots s JOIN tasks t ON s.current_task_id=t.task_id "
                "JOIN user_company_grants g ON g.user_id=s.user_id AND g.company_id=s.company_id "
                "WHERE s.user_id=%s AND g.can_read ORDER BY t.company_id",
                (auth.user_id,),
            ).fetchall()
        prefs = conn.execute(
            "SELECT * FROM preferences WHERE user_id=%s AND status='active' "
            "AND (scope_key='@user' OR scope_key=%s) ORDER BY scope_key,stable_key",
            (auth.user_id, company),
        ).fetchall()
        effective = {}
        dependencies = []
        for pref in prefs:
            if not pref["source_refs"]:
                raise AppError(
                    "STATE_SOURCE_UNAVAILABLE", "有效偏好缺少来源，暂停使用并等待修复。", 503
                )
            evidence = []
            for ref in pref["source_refs"]:
                source = read_source(conn, auth, str(ref["source_id"]), allow_suppressed=True)
                evidence.append({"source_id": source["source_id"], "revision": source["revision"]})
            entry = {
                k: pref[k]
                for k in (
                    "preference_id",
                    "scope_key",
                    "stable_key",
                    "value",
                    "revision",
                    "generation",
                )
            }
            entry["evidence"] = evidence
            if pref["stable_key"] not in effective or pref["scope_key"] != "@user":
                effective[pref["stable_key"]] = entry
            dependencies.append(
                {
                    "kind": "preference",
                    "id": str(pref["preference_id"]),
                    "revision": pref["revision"],
                    "generation": pref["generation"],
                }
            )
        task_evidence = None
        if task:
            revision = conn.execute(
                "SELECT source_refs FROM task_revisions WHERE task_id=%s AND revision=%s",
                (task["task_id"], task["revision"]),
            ).fetchone()
            if not revision:
                raise AppError(
                    "STATE_SOURCE_UNAVAILABLE", "任务修订缺少来源，不能当作无任务继续。", 503
                )
            for ref in revision["source_refs"]:
                read_source(conn, auth, str(ref["source_id"]), allow_suppressed=True)
            head = conn.execute(
                "SELECT * FROM company_heads WHERE company_id=%s", (company,)
            ).fetchone()
            task["snapshot_current"] = (
                task["checked_data_revision"] == head["data_revision"]
                and task["checked_rules_epoch"] == head["rules_epoch"]
            )
            if not task["snapshot_current"]:
                task["unresolved_json"] = {**task["unresolved_json"], "needs_refresh": True}
            if task["snapshot_current"] and task["last_check_ref"]:
                evidence = read_source(
                    conn, auth, str(task["last_check_ref"]), allow_suppressed=True
                )
                parsed = json.loads(evidence["content"])
                data = parsed.get("data", parsed)
                if data.get("company_id") != company or data.get("period") != task["period"]:
                    raise AppError("STATE_SOURCE_UNAVAILABLE", "任务检查来源与范围不一致。", 503)
                task_evidence = {
                    "source_id": str(evidence["source_id"]),
                    "source_revision": evidence["revision"],
                    "rule_refs": data.get("rule_refs", data.get("rules", [])),
                    "missing_invoice_ids": [r["invoice_id"] for r in data.get("rows", [])],
                    "missing_count": data.get("missing_count"),
                    "as_of": data.get("as_of"),
                }
            dependencies.append(
                {
                    "kind": "task",
                    "id": str(task["task_id"]),
                    "revision": task["revision"],
                    "generation": task["generation"],
                }
            )
        pending = conn.execute(
            "SELECT turn_id,pending_clarification,source_seq FROM turns WHERE user_id=%s AND status='waiting_user' "
            "AND pending_clarification IS NOT NULL AND pending_clarification->>'state'='waiting_user' "
            "ORDER BY source_seq DESC",
            (auth.user_id,),
        ).fetchall()
        grants = conn.execute(
            "SELECT g.company_id,c.name,c.aliases,g.can_update FROM user_company_grants g JOIN companies c USING(company_id) WHERE g.user_id=%s AND g.can_read ORDER BY g.company_id",
            (auth.user_id,),
        ).fetchall()
        allowed = {g["company_id"] for g in grants}
        pending = [
            p
            for p in pending
            if not p["pending_clarification"].get("company_id")
            or (
                p["pending_clarification"]["company_id"] in allowed
                and (not company or p["pending_clarification"]["company_id"] == company)
            )
        ]
        imports = (
            conn.execute(
                "SELECT batch_id,company_id,status,revision,counts_json FROM invoice_import_batches WHERE user_id=%s AND company_id=%s AND status IN ('draft','validation_failed','preview_ready') ORDER BY created_at DESC LIMIT 5",
                (auth.user_id, company),
            ).fetchall()
            if company
            else []
        )
        dependencies.extend(
            {"kind": "invoice_import", "id": str(row["batch_id"]), "revision": row["revision"]}
            for row in imports
        )
        return safe_json(
            {
                "company_heads": {company: company_head} if company_head else {},
                "pending_imports": imports,
                "user_id": auth.user_id,
                "selected_company_id": company,
                "memory_epoch": user["memory_epoch"],
                "task": task,
                "task_evidence": task_evidence,
                "task_candidates": candidates,
                "preferences": list(effective.values()),
                "pending_clarifications": pending,
                "grants": grants,
                "dependencies": dependencies,
            }
        )


def search_history(
    db,
    auth: Identity,
    *,
    company_id: str | None,
    query: str,
    cursor=None,
    entity: str | None = None,
    date_from=None,
    date_to=None,
) -> dict:
    if not query.strip() and not entity:
        raise AppError("INVALID_ARGUMENT", "请提供要查找的文字或明确编号。")
    if len(query) > 300:
        raise AppError("INVALID_ARGUMENT", "历史查找条件过长，请缩小范围。")
    query_hash = digest([auth.user_id, company_id, query, entity, date_from, date_to])
    offset = 0
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            offset = int(decoded["offset"])
            if offset < 0 or decoded["query"] != query_hash:
                raise ValueError
        except (ValueError, KeyError, TypeError) as exc:
            raise AppError("INVALID_ARGUMENT", "历史分页条件无效。") from exc
    with db.transaction(readonly=True) as conn:
        if company_id:
            assert_access(conn, auth, company_id)
        clauses = [
            "s.user_id=%s",
            "s.recall_state='active'",
            "s.revision=h.source_revision",
            "NOT EXISTS(SELECT 1 FROM source_scopes ss LEFT JOIN user_company_grants g "
            "ON g.company_id=ss.company_id AND g.user_id=s.user_id WHERE ss.source_id=s.source_id AND g.can_read IS DISTINCT FROM true)",
        ]
        params = [auth.user_id]
        if company_id:
            clauses.append(
                "(s.scope_kind='user_private' OR EXISTS(SELECT 1 FROM source_scopes z WHERE z.source_id=s.source_id AND z.company_id=%s))"
            )
            params.append(company_id)
        else:
            clauses.append("s.scope_kind='user_private'")
        if entity:
            clauses.append("position(lower(%s) in lower(s.content))>0")
            params.append(entity)
        if query.strip():
            clauses.append("position(lower(%s) in lower(h.searchable_text))>0")
            params.append(query.strip())
        if date_from:
            clauses.append("s.created_at>=%s::timestamptz")
            params.append(date_from)
        if date_to:
            clauses.append("s.created_at<%s::timestamptz")
            params.append(date_to)
        rows = conn.execute(
            "SELECT s.* FROM sources s JOIN history_index h USING(source_id) WHERE "
            + " AND ".join(clauses)
            + " ORDER BY s.source_seq DESC,s.created_at DESC LIMIT 6 OFFSET %s",
            [*params, offset],
        ).fetchall()
        matches = []
        for row in rows[:5]:
            source = read_source(conn, auth, str(row["source_id"]), company_id=company_id)
            matches.append(
                {
                    k: source[k]
                    for k in (
                        "source_id",
                        "role",
                        "kind",
                        "content",
                        "revision",
                        "source_seq",
                        "created_at",
                    )
                }
            )
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps({"query": query_hash, "offset": offset + 5}).encode()
            ).decode()
            if len(rows) > 5
            else None
        )
        return safe_json(
            {
                "matches": matches,
                "has_more": len(rows) > 5,
                "next_cursor": next_cursor,
                "not_found": not matches,
            }
        )


def recent_groups(db, auth: Identity, session_id: str, current_turn_id: str) -> list[dict]:
    with db.transaction(readonly=True) as conn:
        session = owned(conn, "sessions", "session_id", session_id, auth)
        rows = conn.execute(
            "SELECT t.turn_id,t.source_seq,t.text,t.final_message FROM turns t "
            "WHERE t.session_id=%s AND t.user_id=%s AND t.turn_id<>%s AND t.final_message IS NOT NULL "
            "AND NOT EXISTS(SELECT 1 FROM sources s WHERE s.turn_id=t.turn_id AND s.recall_state<>'active') "
            "ORDER BY t.source_seq DESC LIMIT 12",
            (session_id, auth.user_id, current_turn_id),
        ).fetchall()
        result = []
        for row in reversed(rows):
            sources = conn.execute(
                "SELECT * FROM sources WHERE turn_id=%s AND role IN ('user','assistant') AND recall_state='active'",
                (row["turn_id"],),
            ).fetchall()
            valid = []
            for source in sources:
                try:
                    if (
                        not session["selected_company_id"]
                        and source["scope_kind"] != "user_private"
                    ):
                        raise AppError("SCOPE_MISMATCH", "未选企业时不加载企业历史。", 409)
                    read_source(
                        conn,
                        auth,
                        str(source["source_id"]),
                        company_id=session["selected_company_id"],
                    )
                    valid.append(
                        {"source_id": str(source["source_id"]), "revision": source["revision"]}
                    )
                except AppError:
                    valid = []
                    break
            if valid:
                result.append(
                    {
                        "turn_id": str(row["turn_id"]),
                        "source_seq": row["source_seq"],
                        "sources": valid,
                        "messages": [
                            {"role": "user", "content": row["text"]},
                            {"role": "assistant", "content": row["final_message"]},
                        ],
                    }
                )
        return result
