"""Optional, bounded measurements. Never controls business execution or retries."""

import asyncio
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache

import psycopg

from app.config import ROOT
from app.values import canonical, digest, jsonb, uid

log = logging.getLogger(__name__)
_parent = ContextVar("insight_parent", default=None)


@contextmanager
def _measurement_transaction(db):
    # Optional telemetry must not sit in the pool queue for the business timeout.
    with db.pool.connection(timeout=0.1) as conn, conn.transaction():
        conn.execute("SET LOCAL statement_timeout='100ms'")
        conn.execute("SET LOCAL lock_timeout='50ms'")
        yield conn


@lru_cache(maxsize=1)
def code_fingerprint():
    return digest(
        [
            (p.name, p.read_text(encoding="utf-8"))
            for p in sorted((ROOT / "backend/app").glob("*.py"))
        ]
    )


def snapshot(settings, mode):
    return {
        "schema_version": 1,
        "code_fingerprint": code_fingerprint(),
        "model": settings.model_id,
        "model_mode": mode,
        "knowledge_policy": "manual_knowledge_independent_conversation_log_v2",
        "model_parameters": {
            k: settings.model_extra_body[k]
            for k in ("temperature", "top_p", "seed")
            if k in settings.model_extra_body
        },
        "output_reserve": settings.output_reserve,
        "context_policy": {
            "policy": "conversation-log-v1",
            "history_search": "independent_lexical",
            **{
                k: getattr(settings, k)
                for k in (
                    "context_use_summary",
                    "context_auto_compact",
                    "input_budget",
                    "max_model_steps",
                    "max_tool_calls",
                )
            },
        },
    }


@contextmanager
def phase(db, execution, key, kind, name, *, context_id=None, tool_call_id=None, attributes=None):
    span_id, inserted = uid(), False
    parent = _parent.get()
    state = {"status": "succeeded", "error_code": None, "attributes": attributes or {}}
    try:
        with _measurement_transaction(db) as conn:
            inserted = bool(
                conn.execute(
                    "INSERT INTO trace_spans(span_id,run_id,parent_span_id,logical_key,kind,name,status,context_id,tool_call_id,attributes) "
                    "VALUES(%s,%s,%s,%s,%s,%s,'running',%s,%s,%s) ON CONFLICT(run_id,logical_key) DO NOTHING RETURNING span_id",
                    (
                        span_id,
                        execution.run_id,
                        parent[1] if parent and parent[0] == execution.run_id else None,
                        key,
                        kind,
                        name,
                        context_id,
                        tool_call_id,
                        jsonb(state["attributes"]),
                    ),
                ).fetchone()
            )
    except psycopg.Error:
        log.warning("insight_measurement_start_failed")
    started = time.perf_counter()
    token = _parent.set((execution.run_id, span_id) if inserted else None)
    try:
        yield state
    except BaseException as exc:
        state["status"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        state["error_code"] = getattr(exc, "code", type(exc).__name__)
        if kind == "model" and state["error_code"] == "RUN_LIMIT_EXCEEDED":
            state["status"] = "not_dispatched"
            state["attributes"]["request_dispatched"] = False
        raise
    finally:
        duration = max(0, (time.perf_counter() - started) * 1000)
        _parent.reset(token)
        if inserted:
            try:
                with _measurement_transaction(db) as conn:
                    conn.execute(
                        "UPDATE trace_spans SET status=%s,ended_at=clock_timestamp(),duration_ms=%s,error_code=%s,attributes=%s WHERE span_id=%s",
                        (
                            state["status"],
                            duration,
                            state["error_code"],
                            jsonb(state["attributes"]),
                            span_id,
                        ),
                    )
            except psycopg.Error:
                log.warning("insight_measurement_finish_failed")


def input_composition(payload, *, material=False):
    import json

    def text_bytes(value):
        return len(canonical(value)[1:-1].encode("utf-8"))

    parts = {
        k: 0
        for k in (
            "system_instruction",
            "task_and_preferences",
            "scope_and_state",
            "history",
            "current_input",
            "current_material",
            "summary",
            "retrieval_results",
            "rule_results",
            "tool_results",
            "tool_definitions",
            "protocol_other",
        )
    }
    if "tools" in payload:
        parts["tool_definitions"] = len(canonical(payload["tools"]).encode("utf-8"))
    messages = payload.get("messages", [])
    user_positions = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    for index, message in enumerate(messages):
        content = message.get("content")
        if (
            message.get("role") == "system"
            and isinstance(content, str)
            and "\n正式状态：\n" in content
        ):
            prefix, state_text = content.split("\n正式状态：\n", 1)
            try:
                state = json.loads(state_text)
                if not isinstance(state, dict):
                    raise ValueError
                parts["system_instruction"] += text_bytes(prefix)
                for key, value in state.items():
                    category = (
                        "task_and_preferences"
                        if key
                        in ("task", "preferences", "task_evidence", "pending", "task_candidates")
                        else "scope_and_state"
                    )
                    parts[category] += text_bytes(canonical({key: value})[1:-1])
                continue
            except (ValueError, TypeError):
                pass
        category = "history"
        if message.get("role") == "system":
            category = (
                "summary" if str(content).startswith("历史导航摘要") else "system_instruction"
            )
        elif message.get("role") == "tool":
            category = "tool_results"
            try:
                kind = (json.loads(content).get("data") or {}).get("kind")
                if kind in (
                    "history_search",
                    "history_evidence",
                    "knowledge_search",
                    "knowledge_evidence",
                ):
                    category = "retrieval_results"
                elif kind == "rules":
                    category = "rule_results"
            except (ValueError, TypeError, AttributeError):
                pass
        elif message.get("role") == "user" and user_positions and index == user_positions[-1]:
            category = "current_material" if material else "current_input"
        parts[category] += len(canonical(message).encode("utf-8"))
    total = len(canonical(payload).encode("utf-8"))
    rest = total - sum(parts.values())
    if rest < 0:
        parts = {"combined_payload": total}
    else:
        parts["protocol_other"] = rest
    return {
        "kind": "estimated:utf8_div2",
        "parts": {k: v / 2 for k, v in parts.items()},
        "note": "按实际请求字节分区，合计为UTF-8预算估算；模块估算不是供应商计费拆分。",
    }


async def complete(db, model, execution, context_id, payload, *, attempt_kind="normal"):
    from app.insight_pricing import load_prices

    price = load_prices().get(payload.get("model"))
    captured_price = (
        {
            k: price[k]
            for k in (
                "model",
                "currency",
                "as_of",
                "source",
                "input_cache_hit_per_million",
                "input_cache_miss_per_million",
                "output_per_million",
            )
        }
        if price
        else None
    )
    attrs = {
        "price_snapshot": captured_price,
        "attempt_kind": attempt_kind,
        "model": payload.get("model"),
        "request_dispatched": getattr(model, "dispatch_enabled", True),
        "model_mode": getattr(model, "mode", "unknown"),
        "payload_hash": digest(payload),
        "usage": None,
        "input_composition": input_composition(payload, material=attempt_kind != "normal"),
    }
    with phase(
        db,
        execution,
        "model:" + context_id,
        "model",
        attempt_kind,
        context_id=context_id,
        attributes=attrs,
    ) as state:
        response = await model.complete(payload)
        # Persist usage before a later business guard can reject stale content.
        state["attributes"]["usage"] = response.get("usage")
        return response
