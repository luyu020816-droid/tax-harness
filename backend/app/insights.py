"""Read-only request cohorts and execution evidence. No runtime/tool dependencies."""

import base64
import json
from datetime import UTC, datetime

from app.errors import AppError
from app.identity import assert_access
from app.insight_access import SCOPES, VISIBLE, assert_turn, content_allowed, redact
from app.insight_metrics import TERMINAL, costs, percentile, union_ms, usage_valid, window
from app.insight_pricing import load_prices
from app.values import canonical, digest, safe_json


def filters(auth, start=None, end=None, company_id=None, status=None, model=None, as_of=None):
    a, b = window(start, end)
    if status and status not in (*TERMINAL, "queued", "running"):
        raise AppError("INVALID_ARGUMENT", "未知请求状态。", 422)
    try:
        ceiling = datetime.fromisoformat(as_of) if as_of else datetime.now(UTC)
        if not ceiling.tzinfo:
            raise ValueError
    except ValueError as exc:
        raise AppError("INVALID_ARGUMENT", "无效高水位。", 422) from exc
    return {
        "user": auth.user_id,
        "from": a,
        "to": b,
        "company": company_id,
        "status": status,
        "model": model,
        "ceiling": ceiling,
    }


def _where():
    return (
        VISIBLE
        + f""" AND t.created_at>=%(from)s AND t.created_at<%(to)s AND t.created_at<=%(ceiling)s
        AND (%(status)s::text IS NULL OR t.status=%(status)s)
        AND (%(company)s::text IS NULL OR EXISTS(SELECT 1 FROM ({SCOPES}) z WHERE z.company_id=%(company)s))
        AND (%(model)s::text IS NULL OR EXISTS(SELECT 1 FROM runs r JOIN insight_attempts a USING(run_id)
             WHERE r.turn_id=t.turn_id AND a.model=%(model)s))"""
    )


def _check(conn, auth, f):
    conn.execute("SET LOCAL statement_timeout='5s'")
    if f["company"]:
        assert_access(conn, auth, f["company"])


def _compose(turn, runs, attempts, allowed):
    usages = [a["usage"] for a in attempts if usage_valid(a["usage"])]
    intervals = [(r["started_at"], r["finished_at"]) for r in runs]
    return {
        **turn,
        "text": redact(turn["text"]) if allowed else "内容受保护，展开查看可用记录",
        "run_count": len(runs),
        "model_attempts": sum(a["dispatched"] is True for a in attempts),
        "unknown_dispatch_calls": sum(a["dispatched"] is None for a in attempts),
        "usage_known_calls": len(usages),
        "input_tokens": sum(u["prompt_tokens"] for u in usages),
        "output_tokens": sum(u["completion_tokens"] for u in usages),
        "active_ms": union_ms(intervals)
        if intervals and all(r.get("state") != "interrupted" for r in runs)
        else None,
        "active_time_note": "中断执行的结束时刻可能来自接管，不能当作完整活跃时间"
        if any(r.get("state") == "interrupted" for r in runs)
        else "由完整执行区间合并计算",
        "elapsed_ms": (max(r["finished_at"] for r in runs) - turn["created_at"]).total_seconds()
        * 1000
        if runs and all(r["finished_at"] for r in runs) and turn["status"] in TERMINAL
        else None,
        "queue_ms": (runs[0]["started_at"] - turn["created_at"]).total_seconds() * 1000
        if runs
        else None,
        "models": sorted({a["model"] for a in attempts if a["model"]}),
        "cost": costs(attempts, load_prices()),
        "content_available": allowed,
        "correctness": "not_evaluated",
    }


def _row(conn, auth, turn):
    runs = conn.execute(
        "SELECT run_id,generation,state,started_at,finished_at,insight_snapshot FROM runs WHERE turn_id=%s ORDER BY generation",
        (turn["turn_id"],),
    ).fetchall()
    attempts = conn.execute(
        "SELECT a.*,p.attributes->'price_snapshot' AS price_snapshot,coalesce(p.attributes ? 'price_snapshot',false) AS price_captured FROM insight_attempts a JOIN runs r ON r.run_id=a.run_id LEFT JOIN trace_spans p ON p.span_id=a.span_id WHERE r.turn_id=%s ORDER BY a.started_at",
        (turn["turn_id"],),
    ).fetchall()
    allowed = bool(runs) and all(content_allowed(conn, auth, str(r["run_id"])) for r in runs)
    return _compose(turn, runs, attempts, allowed)


def requests(db, auth, f, cursor=None, limit=20):
    if not 1 <= limit <= 50:
        raise AppError("INVALID_ARGUMENT", "每页记录数须为1—50。", 422)
    params, clause = dict(f), ""
    signature = digest(safe_json(f))
    if cursor:
        try:
            data = json.loads(base64.urlsafe_b64decode(cursor))
            if data["filter"] != signature:
                raise ValueError
            params.update(last_time=datetime.fromisoformat(data["time"]), last_id=data["id"])
            clause = " AND (t.created_at,t.turn_id)<(%(last_time)s,%(last_id)s::uuid)"
        except (ValueError, KeyError, TypeError) as exc:
            raise AppError("INVALID_ARGUMENT", "分页条件已变化，请刷新列表。", 422) from exc
    params["limit"] = limit + 1
    with db.transaction(readonly=True) as conn:
        _check(conn, auth, f)
        rows = conn.execute(
            "SELECT t.turn_id,t.session_id,t.text,t.status,t.created_at FROM turns t WHERE "
            + _where()
            + clause
            + " ORDER BY t.created_at DESC,t.turn_id DESC LIMIT %(limit)s",
            params,
        ).fetchall()
        visible = rows[:limit]
        ids = [r["turn_id"] for r in visible]
        runs = (
            conn.execute(
                "SELECT turn_id,run_id,state,started_at,finished_at FROM runs WHERE turn_id=ANY(%s::uuid[]) ORDER BY generation",
                (ids,),
            ).fetchall()
            if ids
            else []
        )
        attempts = (
            conn.execute(
                "SELECT r.turn_id,a.*,p.attributes->'price_snapshot' AS price_snapshot,coalesce(p.attributes ? 'price_snapshot',false) AS price_captured FROM runs r JOIN insight_attempts a ON a.run_id=r.run_id LEFT JOIN trace_spans p ON p.span_id=a.span_id WHERE r.turn_id=ANY(%s::uuid[])",
                (ids,),
            ).fetchall()
            if ids
            else []
        )
        # Batched conservative title projection; complete content is guarded lazily in detail.
        safe = (
            conn.execute(
                "SELECT t.turn_id FROM turns t WHERE t.turn_id=ANY(%s::uuid[]) AND EXISTS(SELECT 1 FROM sources s WHERE s.turn_id=t.turn_id AND s.role='user' AND s.content=t.text AND s.recall_state='active') AND NOT EXISTS(SELECT 1 FROM sources s WHERE s.turn_id=t.turn_id AND s.recall_state<>'active') AND NOT EXISTS(SELECT 1 FROM runs r JOIN context_snapshots c USING(run_id) CROSS JOIN LATERAL jsonb_array_elements(coalesce(c.manifest->'sources','[]')) ref LEFT JOIN sources s ON s.source_id=(ref->>'source_id')::uuid WHERE r.turn_id=t.turn_id AND (s.source_id IS NULL OR s.recall_state<>'active' OR s.revision<>(ref->>'revision')::integer)) AND NOT EXISTS(SELECT 1 FROM runs r JOIN context_snapshots c USING(run_id) WHERE r.turn_id=t.turn_id AND coalesce(c.manifest->'retrieval_refs','[]')<>'[]'::jsonb)",
                (ids,),
            ).fetchall()
            if ids
            else []
        )
        safe_ids = {r["turn_id"] for r in safe}
        by_run, by_attempt = {}, {}
        for r in runs:
            by_run.setdefault(r["turn_id"], []).append(r)
        for a in attempts:
            by_attempt.setdefault(a["turn_id"], []).append(a)
        result = [
            _compose(
                row,
                by_run.get(row["turn_id"], []),
                by_attempt.get(row["turn_id"], []),
                row["turn_id"] in safe_ids,
            )
            for row in visible
        ]
        next_cursor = None
        if len(rows) > limit:
            last = visible[-1]
            next_cursor = base64.urlsafe_b64encode(
                canonical(
                    {"filter": signature, "time": last["created_at"], "id": last["turn_id"]}
                ).encode()
            ).decode()
        return safe_json(
            {
                "schema_version": 1,
                "as_of": f["ceiling"],
                "measured_at": datetime.now(UTC),
                "requests": result,
                "has_more": len(rows) > limit,
                "next_cursor": next_cursor,
            }
        )


def summary(db, auth, f):
    """Batch SQL projects only counts/intervals/usage, never full trace payloads."""
    with db.transaction(readonly=True) as conn:
        _check(conn, auth, f)
        cohort_rows = conn.execute(
            "SELECT t.turn_id,t.status FROM turns t WHERE " + _where(), f
        ).fetchall()
        ids = [row["turn_id"] for row in cohort_rows]
        counts = {}
        for row in cohort_rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        status_by_turn = {row["turn_id"]: row["status"] for row in cohort_rows}
        intervals = (
            conn.execute(
                "SELECT turn_id,state AS run_state,started_at,finished_at,insight_snapshot IS NOT NULL AS versioned FROM runs WHERE turn_id=ANY(%s::uuid[]) ORDER BY turn_id,started_at",
                (ids,),
            ).fetchall()
            if ids
            else []
        )
        for row in intervals:
            row["status"] = status_by_turn[row["turn_id"]]
        attempts = (
            conn.execute(
                "SELECT a.model,a.dispatched,a.usage,a.duration_ms,p.attributes->'price_snapshot' AS price_snapshot,coalesce(p.attributes ? 'price_snapshot',false) AS price_captured FROM runs r JOIN insight_attempts a ON a.run_id=r.run_id LEFT JOIN trace_spans p ON p.span_id=a.span_id WHERE r.turn_id=ANY(%s::uuid[])",
                (ids,),
            ).fetchall()
            if ids
            else []
        )
        snapshots = {"known": sum(row["versioned"] for row in intervals), "n": len(intervals)}
        incomplete = (
            conn.execute(
                "SELECT count(*) FILTER(WHERE s.ended_at IS NULL) AS open,count(*) FILTER(WHERE s.ended_at IS NULL AND r.finished_at IS NOT NULL) AS abandoned FROM trace_spans s JOIN runs r USING(run_id) WHERE r.turn_id=ANY(%s::uuid[])",
                (ids,),
            ).fetchone()
            if ids
            else {"open": 0, "abandoned": 0}
        )
        grouped = {}
        for row in intervals:
            group = grouped.setdefault(
                row["turn_id"], {"status": row["status"], "intervals": [], "interrupted": False}
            )
            group["interrupted"] = group["interrupted"] or row["run_state"] == "interrupted"
            group["intervals"].append((row["started_at"], row["finished_at"]))
        durations = [
            v
            for g in grouped.values()
            if g["status"] in TERMINAL
            and not g["interrupted"]
            and (v := union_ms(g["intervals"])) is not None
        ]
        usage = [
            a["usage"] for a in attempts if a["dispatched"] is not False and usage_valid(a["usage"])
        ]
        terminal = sum(counts.get(s, 0) for s in TERMINAL)
        return safe_json(
            {
                "schema_version": 1,
                "as_of": f["ceiling"],
                "measured_at": datetime.now(UTC),
                "request_count": sum(counts.values()),
                "states": counts,
                "terminal_count": terminal,
                "completion_ratio": counts.get("completed", 0) / terminal if terminal else None,
                "p50_ms": percentile(durations, 0.5),
                "p95_ms": percentile(durations, 0.95),
                "timed_requests": len(durations),
                "small_sample": len(durations) < 20,
                "run_count": len(intervals),
                "model_attempts": sum(a["dispatched"] is True for a in attempts),
                "input_tokens": sum(u["prompt_tokens"] for u in usage),
                "output_tokens": sum(u["completion_tokens"] for u in usage),
                "cost": costs(attempts, load_prices()),
                "coverage": {
                    "usage_known": len(usage),
                    "dispatched": sum(a["dispatched"] is True for a in attempts),
                    "dispatch_unknown": sum(a["dispatched"] is None for a in attempts),
                    "timed_calls": sum(
                        a["duration_ms"] is not None and a["dispatched"] is not False
                        for a in attempts
                    ),
                    "versioned_runs": snapshots["known"],
                    "runs": snapshots["n"],
                    "open_spans": incomplete["open"],
                    "abandoned_spans": incomplete["abandoned"],
                },
                "truncated": False,
            }
        )


def span_page(conn, run_id, offset=0):
    if not 0 <= offset <= 100000:
        raise AppError("INVALID_ARGUMENT", "无效节点偏移。", 422)
    rows = conn.execute(
        """WITH nodes AS (
        SELECT s.span_id,s.parent_span_id,s.kind,s.name,
          CASE WHEN s.status='running' AND r.finished_at IS NOT NULL THEN 'interrupted' ELSE s.status END AS status,
          s.started_at,s.ended_at,s.duration_ms,s.measurement_kind,s.context_id,s.tool_call_id,s.error_code,
          jsonb_build_object('model',s.attributes->'model','usage',s.attributes->'usage',
            'input_composition',s.attributes->'input_composition','attempt_kind',s.attributes->'attempt_kind',
            'operation_count',s.attributes->'operation_count','model_mode',s.attributes->'model_mode',
            'price_snapshot',s.attributes->'price_snapshot','price_captured',s.attributes ? 'price_snapshot') AS attributes
        FROM trace_spans s JOIN runs r USING(run_id) WHERE s.run_id=%s
        UNION ALL
        SELECT a.context_id,NULL::uuid,'model','历史模型调用',a.status,a.started_at,NULL::timestamptz,
          a.duration_ms,a.measurement_kind,a.context_id,NULL::text,NULL::text,
          jsonb_build_object('model',a.model,'usage',a.usage)
        FROM insight_attempts a WHERE a.run_id=%s AND a.span_id IS NULL
        UNION ALL
        SELECT x.result_source_id,NULL::uuid,'tool',x.name,
          CASE WHEN x.state='done' THEN 'succeeded' WHEN x.state='failed' THEN 'failed' ELSE 'interrupted' END,
          min(e.created_at) FILTER(WHERE e.kind='tool_started'),max(e.created_at) FILTER(WHERE e.kind='tool_finished'),
          extract(epoch FROM (max(e.created_at) FILTER(WHERE e.kind='tool_finished')-min(e.created_at) FILTER(WHERE e.kind='tool_started')))*1000,
          'event_derived',NULL::uuid,x.tool_call_id,NULL::text,'{}'::jsonb
        FROM tool_executions x LEFT JOIN run_events e ON e.run_id=x.run_id AND e.tool_call_id=x.tool_call_id
        WHERE x.run_id=%s AND NOT EXISTS(SELECT 1 FROM trace_spans s WHERE s.run_id=x.run_id AND s.tool_call_id=x.tool_call_id)
        GROUP BY x.run_id,x.tool_call_id
    ) SELECT * FROM nodes ORDER BY started_at NULLS LAST,span_id NULLS LAST LIMIT 101 OFFSET %s""",
        (run_id, run_id, run_id, offset),
    ).fetchall()
    return {
        "spans": rows[:100],
        "has_more": len(rows) > 100,
        "next_offset": offset + 100 if len(rows) > 100 else None,
    }


def read_spans(db, auth, run_id, offset=0):
    with db.transaction(readonly=True) as conn:
        row = conn.execute("SELECT turn_id FROM runs WHERE run_id=%s", (run_id,)).fetchone()
        if not row:
            raise AppError("RESOURCE_NOT_FOUND", "运行记录不存在。", 404)
        assert_turn(conn, auth, str(row["turn_id"]))
        return safe_json(span_page(conn, run_id, offset))


def detail(db, auth, turn_id):
    with db.transaction(readonly=True) as conn:
        turn = assert_turn(conn, auth, turn_id)
        request = _row(
            conn,
            auth,
            {k: turn[k] for k in ("turn_id", "session_id", "text", "status", "created_at")},
        )
        runs = conn.execute(
            "SELECT * FROM runs WHERE turn_id=%s ORDER BY generation", (turn_id,)
        ).fetchall()
        for run in runs:
            rid = str(run["run_id"])
            run["content_available"] = content_allowed(conn, auth, rid)
            page = span_page(conn, rid)
            run["spans"] = page["spans"]
            run["has_more_spans"] = page["has_more"]
            run["next_span_offset"] = page["next_offset"]
            run["evidence"] = _evidence(conn, auth, rid, run["content_available"])
        return safe_json(redact({"schema_version": 1, "request": request, "runs": runs}))


def _evidence(conn, auth, run_id, allowed):
    if not allowed:
        return {"redacted": True, "reason": "部分来源已失效，原始输入、结果和证据内容不可查看"}
    contexts = conn.execute(
        "SELECT context_id,manifest FROM context_snapshots WHERE run_id=%s ORDER BY model_step",
        (run_id,),
    ).fetchall()
    operations = conn.execute(
        "SELECT DISTINCT o.operation_id,o.company_id,o.invoice_id,o.source_id,o.authorization_plan_id,o.before_revision,o.after_revision,o.state,o.verification_state,o.committed_at,'request_ledger_not_run_attribution' AS association FROM operations o JOIN sources s ON s.source_id=o.source_id LEFT JOIN write_plans p ON p.plan_id=o.authorization_plan_id LEFT JOIN sources origin ON origin.source_id=p.origin_source_id JOIN runs r ON r.turn_id=s.turn_id OR r.turn_id=origin.turn_id WHERE r.run_id=%s AND o.user_id=%s",
        (run_id, auth.user_id),
    ).fetchall()
    deliveries = conn.execute(
        "SELECT kind,payload->>'code' AS code,tool_call_id,created_at FROM run_events WHERE run_id=%s AND kind IN ('recovery_queued','failed','completed','compaction_skipped','reply_rejected') ORDER BY event_seq",
        (run_id,),
    ).fetchall()
    rule_ids = list(
        {
            r["rule_version_id"]
            for c in contexts
            for r in c["manifest"].get("rule_refs", [])
            if r.get("rule_version_id")
        }
    )
    current_rules = (
        conn.execute(
            "SELECT rule_version_id,rule_code,business_version,status,content_revision,effective_from,effective_to FROM rule_versions WHERE rule_version_id=ANY(%s::uuid[])",
            (rule_ids,),
        ).fetchall()
        if rule_ids
        else []
    )
    # This is a projection of performed work, not an invented user-intent plan.
    turn = conn.execute(
        "SELECT t.final_cards FROM turns t JOIN runs r USING(turn_id) WHERE r.run_id=%s", (run_id,)
    ).fetchone()
    tools = conn.execute(
        "SELECT e.name,s.content FROM tool_executions e JOIN sources s ON s.source_id=e.result_source_id WHERE e.run_id=%s AND e.state='done' AND e.name IN ('query_month','check_purposes')",
        (run_id,),
    ).fetchall()
    performed = {}
    for tool in tools:
        try:
            result = json.loads(tool["content"])
            data = result["data"]
            if result.get("ok") is not True:
                continue
            key = (data["kind"], data["company_id"], data["period"])
            delivered = any(
                card.get("kind") == key[0]
                and card.get("company_id") == key[1]
                and card.get("period") == key[2]
                for card in (turn["final_cards"] or [])
            )
            performed[key] = {
                "kind": key[0],
                "company_id": key[1],
                "period": key[2],
                "status": "delivered" if delivered else "executed_not_delivered",
            }
        except (ValueError, KeyError, TypeError):
            continue
    return {
        "correctness": "not_evaluated",
        "operations": operations,
        "outcomes": deliveries,
        "current_rules": current_rules,
        "performed_delivery": list(performed.values()),
        "user_intent_coverage": "not_verified",
        "contexts": [
            {
                "context_id": c["context_id"],
                **{
                    k: c["manifest"].get(k)
                    for k in (
                        "kind",
                        "sources",
                        "rule_refs",
                        "heads",
                        "summary_id",
                        "retrieval_refs",
                        "memory_epoch",
                        "context_policy",
                        "dropped_optional_turns",
                        "preferences",
                        "task",
                        "dependencies",
                    )
                },
            }
            for c in contexts
        ],
    }


def node(db, auth, span_id, offset=0):
    if offset < 0 or offset > 10 * 1024 * 1024:
        raise AppError("INVALID_ARGUMENT", "无效内容偏移。", 422)
    with db.transaction(readonly=True) as conn:
        row = conn.execute(
            "SELECT run_id,context_id,tool_call_id FROM trace_spans WHERE span_id=%s", (span_id,)
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT run_id,context_id,NULL::text AS tool_call_id FROM context_snapshots WHERE context_id=%s",
                (span_id,),
            ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT run_id,NULL::uuid AS context_id,tool_call_id FROM tool_executions WHERE result_source_id=%s",
                (span_id,),
            ).fetchone()
        if not row:
            raise AppError("RESOURCE_NOT_FOUND", "轨迹节点不存在。", 404)
        run = conn.execute("SELECT turn_id FROM runs WHERE run_id=%s", (row["run_id"],)).fetchone()
        assert_turn(conn, auth, str(run["turn_id"]))
        if not content_allowed(conn, auth, str(row["run_id"])):
            return {"redacted": True, "reason": "来源失效或无法验证"}
        data = {}
        if row["context_id"]:
            data["context"] = conn.execute(
                "SELECT payload,manifest,usage,duration_ms FROM context_snapshots WHERE context_id=%s",
                (row["context_id"],),
            ).fetchone()
        if row["tool_call_id"]:
            data["tool"] = conn.execute(
                "SELECT e.name,e.arguments,s.content AS result FROM tool_executions e LEFT JOIN sources s ON s.source_id=e.result_source_id WHERE e.run_id=%s AND e.tool_call_id=%s",
                (row["run_id"], row["tool_call_id"]),
            ).fetchone()
        serialized = canonical(redact(data))
        # Character boundaries keep UTF-8 valid. Return chunks as text, not partial JSON objects.
        sizes, count = [], 0
        for ch in serialized:
            size = len(ch.encode("utf-8"))
            if count >= offset and count + size > offset + 32768:
                break
            if count >= offset:
                sizes.append(ch)
            count += size
        return {
            "text": "".join(sizes),
            "offset": offset,
            "next_offset": count if count < len(serialized.encode("utf-8")) else None,
            "total_bytes": len(serialized.encode("utf-8")),
            "redacted": False,
        }
