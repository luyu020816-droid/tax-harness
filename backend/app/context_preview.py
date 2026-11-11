"""Read-only, explicitly approximate projection of the next model request."""

from dataclasses import dataclass

from app.context import RECENT_DIALOGUE_TURNS, SYSTEM_PROMPT, estimate_tokens
from app.conversation_projection import groups as conversation_groups
from app.errors import AppError
from app.identity import Identity, owned
from app.memory import load_core
from app.model_requests import build_request
from app.summary_evidence import validate as validate_summary
from app.tool_schemas import tool_definitions
from app.values import canonical


@dataclass(frozen=True)
class ProjectionScope:
    auth: Identity
    session_id: str
    source_seq: int
    turn_id: str = "00000000-0000-0000-0000-000000000000"


def actual_usage(db, settings, auth, session_id):
    """Last actual normal model input in this session, not a forecast or a sum."""
    with db.transaction(readonly=True) as conn:
        owned(conn, "sessions", "session_id", session_id, auth)
        row = conn.execute(
            "SELECT c.usage,c.payload->>'model' AS model,c.created_at "
            "FROM context_snapshots c JOIN runs r ON r.run_id=c.run_id "
            "JOIN turns t ON t.turn_id=r.turn_id "
            "WHERE t.session_id=%s AND t.user_id=%s "
            "AND coalesce(c.manifest->>'kind','') NOT IN ('compaction','control_execution') "
            "ORDER BY c.created_at DESC,c.model_step DESC LIMIT 1",
            (session_id, auth.user_id),
        ).fetchone()
    window = settings.context_limits["effective_window"]
    if not row:
        return {"status": "no_calls", "input_tokens": 0, "effective_window": window}
    usage = row["usage"] if isinstance(row["usage"], dict) else {}
    count = usage.get("prompt_tokens")
    if type(count) is not int or count < 0:
        return {"status": "unknown", "input_tokens": None, "effective_window": window}
    if row["model"] != settings.model_id:
        window = None
    return {
        "status": "measured",
        "input_tokens": count,
        "effective_window": window,
        "model": row["model"],
        "measured_at": row["created_at"].isoformat(),
    }


def preview(db, settings, auth, session_id, draft):
    """Estimate without creating sources, bindings, turns or an Execution."""
    core = load_core(db, auth, session_id)
    with db.transaction(readonly=True) as conn:
        session = owned(conn, "sessions", "session_id", session_id, auth)
        user = conn.execute(
            "SELECT next_source_seq FROM users WHERE user_id=%s", (auth.user_id,)
        ).fetchone()
        projection = ProjectionScope(auth, session_id, user["next_source_seq"] + 1)
        summary = conn.execute(
            "SELECT * FROM summaries WHERE user_id=%s AND session_id=%s AND scope=%s "
            "AND memory_epoch=%s AND status='active' ORDER BY generation DESC LIMIT 1",
            (
                auth.user_id,
                session_id,
                session["selected_company_id"] or "@user",
                core["memory_epoch"],
            ),
        ).fetchone()
        if summary and settings.context_use_summary:
            try:
                validate_summary(conn, projection, summary["content"], summary["covered_sources"])
            except AppError:
                summary = None
        else:
            summary = None
    groups = conversation_groups(
        db,
        projection,
        summary["covered_sources"] if summary else [],
        preserve_recent=RECENT_DIALOGUE_TURNS,
    )
    # Request-specific scope bindings and attachments only exist after acceptance.
    # Keep these explicit placeholders, not invented real identifiers or facts.
    state = {
        **core,
        "conversation_retrieval": "independent_history_tools",
        "history_coverage": {"excluded_groups": 0, "complete": True},
        "request_frame": {"available_scope_refs": [], "estimated": True},
        "current_source": {"source_id": projection.turn_id, "source_seq": projection.source_seq},
        "known_operations": [],
        "preference_operations_this_turn": [],
        "current_attachments": [],
        "current_knowledge_attachments": [],
        "knowledge_candidates": [],
        "last_visible_view": None,
    }
    messages = [{"role": "system", "content": SYSTEM_PROMPT + "\n正式状态：\n" + canonical(state)}]
    if summary:
        messages.append(
            {
                "role": "system",
                "content": "历史导航摘要（必要时回原文，不能覆盖当前正式状态）："
                + summary["content"],
            }
        )
    messages.extend(message for group in groups for message in group["messages"])
    messages.append({"role": "user", "content": draft})
    # The real assembler may filter schemas. Include the full registered set and
    # declare unknown dynamic state rather than pretending this is an exact count.
    payload = build_request(settings, messages, tools=tool_definitions())
    count, limits = estimate_tokens(payload), settings.context_limits
    window = limits["effective_window"]
    pressure = count + limits["output_reserve"] + (limits["margin"] or 0)
    return {
        "approximate": True,
        "window_status": limits["window_status"],
        "model_capacity_source": limits.get("model_capacity_source"),
        "effective_window": window,
        "input_estimate": count,
        "output_reserve": limits["output_reserve"],
        "margin": limits["margin"],
        "input_limit": limits["input_limit"],
        "pressure_ratio": pressure / window if window else None,
        "count_kind": "estimated:utf8_div2_plus_protocol",
        "unaccounted_items": ["本轮范围引用、附件元数据、后续工具结果和部分动态状态"],
        "summary_id": str(summary["summary_id"]) if summary else None,
        "history_groups": len(groups),
    }
