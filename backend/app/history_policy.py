"""Conversation intent and the authority for model-facing history tools."""

import re

from app.identity import owned

_HISTORY_REQUEST = re.compile(
    r"(?:找回|回顾|回忆).{0,32}(?:之前|昨[天日]|上次|会话|对话|聊天|原话|方案|说的|回复)"
    r"|(?:找|查|翻|读|搜索|检索).{0,24}(?:其他会话|历史会话|聊天记录|(?:以前|之前|上次)的?(?:方案|会话|回复))"
    r"|(?:昨天|上次|以前).{0,24}(?:说了什么|说过什么|聊了什么)"
)
_LOCAL_ONLY = re.compile(
    r"(?:不要|不用|别|禁止).{0,5}(?:查|搜索|检索|读取|回顾).{0,8}(?:历史|会话|聊天)"
    r"|本会话|当前会话|这段对话|本次对话|上面的?原话|上述原话|刚(?:才|刚)的?原话"
)
_CONTINUE = re.compile(
    r"^\s*(?:请)?(?:下一页|再来一页|继续(?:查找|读|翻|回读)|下半段)[。！!？?\s]*$"
)
_NEXT_PAGE = re.compile(r"^\s*(?:请)?(?:下一页|再来一页)[。！!？?\s]*$")
_QUOTE = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"')


def requests_other_sessions(conn, auth, session_id, before, text):
    text = _QUOTE.sub(" ", text)
    if _LOCAL_ONLY.search(text):
        return False
    if _HISTORY_REQUEST.search(text):
        return True
    if not _CONTINUE.fullmatch(text):
        return False
    eligible = ("history_page",) if _NEXT_PAGE.fullmatch(text) else ("history_page", "history_quote")
    return any(
        card["kind"] in eligible
        for card in _previous_cross_session_cards(conn, auth, session_id, before)
    )


def _previous_cross_session_cards(conn, auth, session_id, before):
    previous = conn.execute(
        "SELECT t.final_cards, EXISTS(SELECT 1 FROM runs r JOIN context_snapshots c USING(run_id) "
        "WHERE r.turn_id=t.turn_id AND r.generation=t.active_run_generation "
        "AND c.manifest->>'conversation_retrieval'='explicit') AS explicit_history "
        "FROM turns t WHERE t.session_id=%s AND t.user_id=%s AND t.source_seq<%s "
        "AND t.status IN ('completed','resolved') AND t.request_kind<>'compact' "
        "ORDER BY t.source_seq DESC LIMIT 1",
        (session_id, auth.user_id, before),
    ).fetchone()
    return previous["final_cards"] if previous and previous["explicit_history"] else []


def next_page(conn, execution, text):
    if not _NEXT_PAGE.fullmatch(text):
        return None
    cards = _previous_cross_session_cards(
        conn, execution.auth, execution.session_id, execution.source_seq,
    )
    page = next((card for card in reversed(cards) if card["kind"] == "history_page"), None)
    if not page:
        return None
    if "next_cursor" not in page:
        return {"status": "stale"}
    cursor = page["next_cursor"]
    if cursor is None:
        return {"status": "end"}
    row = conn.execute(
        "SELECT query_plan,scope_snapshot FROM history_queries WHERE query_id=%s AND user_id=%s",
        (cursor, execution.auth.user_id),
    ).fetchone()
    expected = {
        "session_id": str(execution.session_id),
        "companies": list(company_scope(conn, execution, text)),
    }
    if (
        not row or row["scope_snapshot"].get("history_scope") != expected
        or row["query_plan"].get("kind") != "conversation"
    ):
        return {"status": "stale"}
    return {"status": "ready", "arguments": {**row["query_plan"]["arguments"], "cursor": cursor}}


def availability(requested, definitions):
    if not requested:
        return "current_session"
    if any(item["function"]["name"] == "search_conversations" for item in definitions):
        return "explicit"
    return "disabled"


def instruction(mode):
    return {
        "explicit": "\n用户明确要求回顾其他会话，使用独立历史工具并保留原话的会话、日期和角色。",
        "current_session": "\n默认只使用本会话。read_conversation 仅能回读本会话可见来源。",
        "disabled": "\n本轮明确要求跨会话回顾，但当前 Agent 未获历史检索授权。请用 explanation，code=conversation_boundary。",
    }[mode]


def company_scope(conn, execution, text):
    from app.turns import mentioned_companies

    session = owned(conn, "sessions", "session_id", execution.session_id, execution.auth)
    company = session["selected_company_id"]
    return tuple(sorted(mentioned_companies(conn, text) or ([company] if company else [])))


def can_search_other_sessions(conn, turn_id):
    from app.agent_authority import policy_for_turn

    policy = policy_for_turn(conn, turn_id)
    if policy is not None:
        return "invoke" in policy.actions("search_conversations")
    return not conn.execute(
        "SELECT 1 FROM agent_tool_states WHERE tool_name='search_conversations' AND NOT enabled"
    ).fetchone()


def cursor_scope(execution):
    companies = getattr(execution, "history_companies", None)
    if companies is None:
        return None
    return {"session_id": str(execution.session_id), "companies": list(companies)}
