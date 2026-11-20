"""Transactional agent bindings and current capability checks.

Callers must keep the returned shared advisory lock until any associated write
commits. Configuration publication and capability changes use the exclusive
form of the same lock.
"""

from app.agent_capabilities import CapabilityPolicy, missing_actions, restrict_document_ids
from app.errors import AppError
from app.tool_schemas import TOOLS
from app.values import jsonb, safe_json

LOCK_ID = 8123419417


def _grants(config):
    return {name: frozenset(actions) for name, actions in config["tool_grants"].items()}


def _disabled_tools(conn):
    return {
        r["tool_name"]
        for r in conn.execute("SELECT tool_name FROM agent_tool_states WHERE NOT enabled")
    }


def capability_policy(snapshot, current, disabled, *, agent_enabled=True):
    current_grants = _grants(current)
    if snapshot["knowledge_mode"] != "authorized":
        # Restricted knowledge must not be widened through historical excerpts.
        for name in ("search_conversations", "read_conversation", "search_history", "read_history"):
            current_grants.pop(name, None)
    if snapshot["knowledge_mode"] == "none":
        for name in ("search_knowledge", "read_evidence", "inspect_workbook", "query_workbook"):
            current_grants.pop(name, None)
    return CapabilityPolicy(
        registered=frozenset(TOOLS),
        bound_grants=_grants(snapshot),
        current_grants=current_grants,
        enabled_tools=frozenset(TOOLS) - disabled,
        agent_enabled=agent_enabled,
    )


def skill_availability(conn, snapshot, policy):
    active, inactive = [], []
    for ref in snapshot["skill_versions"]:
        row = conn.execute(
            "SELECT s.enabled,s.name,v.config FROM agent_skills s LEFT JOIN agent_skill_versions v "
            "ON v.skill_id=s.skill_id AND v.version=%s WHERE s.skill_id=%s",
            (ref["version"], ref["skill_id"]),
        ).fetchone()
        if not row or not row["config"]:
            inactive.append(
                {
                    **ref,
                    "name": row["name"] if row else ref["skill_id"],
                    "reason": "Skill 版本不可用",
                }
            )
            continue
        config = row["config"]
        missing = missing_actions(config["tool_grants"], policy.actions)
        needs_scope = config["requires_knowledge"] and (
            snapshot["knowledge_mode"] == "none"
            or (snapshot["knowledge_mode"] == "selected" and not snapshot["knowledge_document_ids"])
        )
        if not row["enabled"] or missing or needs_scope:
            reason = (
                "Skill 已停用"
                if not row["enabled"]
                else "知识范围不可用"
                if needs_scope
                else "依赖工具动作不可用"
            )
            inactive.append(
                {**ref, "name": config["name"], "reason": reason, "missing_tools": missing}
            )
        else:
            active.append((ref, config))
    return active, inactive


def effective_snapshot(conn, snapshot):
    """Current configuration for a new request, shared by acceptance and previews."""
    policy = capability_policy(snapshot, snapshot, _disabled_tools(conn))
    active, inactive = skill_availability(conn, snapshot, policy)
    return {
        **snapshot,
        "tool_grants": {
            name: sorted(policy.actions(name))
            for name in snapshot["tool_grants"]
            if policy.actions(name)
        },
        "skill_versions": [ref for ref, _ in active],
        "inactive_skills": inactive,
    }


def _revocations_after(conn, binding):
    snapshot = binding["snapshot"]
    rows = conn.execute(
        "SELECT kind,capability_id FROM agent_capability_events WHERE action='disable' AND created_at>%s "
        "AND ((kind='tool' AND capability_id=ANY(%s)) OR (kind='skill' AND capability_id=ANY(%s)))",
        (
            binding["bound_at"],
            list(snapshot["tool_grants"]),
            [s["skill_id"] for s in snapshot["skill_versions"]],
        ),
    ).fetchall()
    return {(r["kind"], r["capability_id"]) for r in rows}


def _agent_was_disabled(conn, binding):
    return (
        conn.execute(
            "SELECT 1 FROM agent_config_events WHERE agent_id=%s AND action='disable' AND created_at>%s LIMIT 1",
            (binding["agent_id"], binding["bound_at"]),
        ).fetchone()
        is not None
    )


def bind_turn(conn, auth, turn_id, agent_id):
    """Bind a queued turn inside its acceptance transaction; never overwrite a binding."""
    conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (LOCK_ID,))
    turn = conn.execute(
        "SELECT status FROM turns WHERE turn_id=%s AND user_id=%s", (turn_id, auth.user_id)
    ).fetchone()
    if not turn or turn["status"] != "queued":
        raise AppError("RESOURCE_NOT_FOUND", "待办理请求不存在或不可访问。", 404)
    profile = conn.execute(
        "SELECT p.enabled,p.current_version,v.config FROM agent_profiles p "
        "JOIN agent_profile_versions v ON v.agent_id=p.agent_id AND v.version=p.current_version "
        "WHERE p.agent_id=%s",
        (agent_id,),
    ).fetchone()
    if not profile or not profile["enabled"]:
        raise AppError("AGENT_UNAVAILABLE", "Agent 未启用或尚未发布。", 409)
    config = effective_snapshot(conn, profile["config"])
    conn.execute(
        "INSERT INTO agent_turn_bindings(turn_id,agent_id,agent_version,snapshot) VALUES(%s,%s,%s,%s)",
        (turn_id, agent_id, profile["current_version"], jsonb(config)),
    )
    return safe_json(
        {"agent_id": agent_id, "version": profile["current_version"], "snapshot": config}
    )


def policy_for_turn(conn, turn_id):
    """Resolve the bound grant against current grants and current tool status."""
    conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (LOCK_ID,))
    binding = conn.execute(
        "SELECT b.*,p.enabled,p.current_version,v.config AS current_config "
        "FROM agent_turn_bindings b LEFT JOIN agent_profiles p USING(agent_id) "
        "LEFT JOIN agent_profile_versions v ON v.agent_id=p.agent_id AND v.version=p.current_version "
        "WHERE b.turn_id=%s",
        (turn_id,),
    ).fetchone()
    if not binding:
        return None
    if binding["current_config"] is None:
        raise AppError("AGENT_UNAVAILABLE", "Agent 配置已失效。", 409)
    disabled = _disabled_tools(conn) | {
        name for kind, name in _revocations_after(conn, binding) if kind == "tool"
    }
    return capability_policy(
        binding["snapshot"],
        binding["current_config"],
        disabled,
        agent_enabled=binding["enabled"] and not _agent_was_disabled(conn, binding),
    )


def authorize_tool(conn, turn_id, name, arguments):
    policy = policy_for_turn(conn, turn_id)
    if policy is not None:
        policy.authorize(name, arguments)
    elif conn.execute(
        "SELECT 1 FROM agent_tool_states WHERE tool_name=%s AND NOT enabled", (name,)
    ).fetchone():
        raise AppError("AGENT_CAPABILITY_DENIED", "该工具已停用。", 403)


def enabled_definitions(conn, definitions):
    """Apply platform-wide tool switches, including the knowledge routing alias."""
    conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (LOCK_ID,))
    disabled = _disabled_tools(conn)
    if disabled & {"get_rules", "search_knowledge"}:
        disabled.add("lookup_knowledge")
    return [d for d in definitions if d["function"]["name"] not in disabled]


def restrict_knowledge(conn, turn_id, document_ids):
    """Apply the bound knowledge scope; legacy unbound turns keep existing checks."""
    conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (LOCK_ID,))
    row = conn.execute(
        "SELECT snapshot FROM agent_turn_bindings WHERE turn_id=%s", (turn_id,)
    ).fetchone()
    if not row:
        return document_ids
    validate_snapshot(conn, turn_id)
    snapshot = row["snapshot"]
    return restrict_document_ids(
        snapshot["knowledge_mode"], frozenset(snapshot["knowledge_document_ids"]), document_ids
    )


def context_configuration(conn, turn_id):
    """Resolve pinned instructions without widening current authorization."""
    policy = policy_for_turn(conn, turn_id)
    if policy is None:
        return None
    validate_snapshot(conn, turn_id)
    snapshot = conn.execute(
        "SELECT snapshot FROM agent_turn_bindings WHERE turn_id=%s", (turn_id,)
    ).fetchone()["snapshot"]
    active, inactive = skill_availability(conn, snapshot, policy)
    return {
        "snapshot": snapshot,
        "policy": policy,
        "skills": [config for _, config in active],
        "inactive_skills": [*snapshot.get("inactive_skills", []), *inactive],
    }


def history_scope_allows(current, previous):
    """A restricted agent sees prior agent output only from an equal/narrower scope."""
    if current["knowledge_mode"] == "authorized":
        return True
    if previous is None:
        return False
    prior_mode = previous.get("knowledge_mode")
    if prior_mode == "none":
        return True
    return (
        current["knowledge_mode"] == "selected"
        and prior_mode == "selected"
        and set(previous.get("knowledge_document_ids", []))
        <= set(current["knowledge_document_ids"])
    )


def validate_snapshot(conn, turn_id):
    """Reject reuse of a context whose originally granted scope was narrowed."""
    conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (LOCK_ID,))
    row = conn.execute(
        "SELECT b.agent_id,b.bound_at,b.snapshot,p.enabled,v.config AS current_config "
        "FROM agent_turn_bindings b LEFT JOIN agent_profiles p USING(agent_id) "
        "LEFT JOIN agent_profile_versions v ON v.agent_id=p.agent_id AND v.version=p.current_version "
        "WHERE b.turn_id=%s",
        (turn_id,),
    ).fetchone()
    if not row:
        return
    current = row["current_config"]
    if not row["enabled"] or current is None or _agent_was_disabled(conn, row):
        raise AppError("AGENT_UNAVAILABLE", "Agent 已停用或配置已失效。", 409)
    snapshot = row["snapshot"]
    old, new = _grants(snapshot), _grants(current)
    if any(not actions <= new.get(name, frozenset()) for name, actions in old.items()):
        raise AppError("AGENT_AUTHORITY_CHANGED", "Agent 能力已撤销，请重新发起请求。", 409)
    if conn.execute(
        "SELECT 1 FROM agent_tool_states WHERE NOT enabled AND tool_name=ANY(%s) LIMIT 1",
        (list(old),),
    ).fetchone():
        raise AppError("AGENT_AUTHORITY_CHANGED", "Agent 能力已停用，请重新发起请求。", 409)
    if _revocations_after(conn, row):
        raise AppError(
            "AGENT_AUTHORITY_CHANGED", "原请求的工具或 Skill 曾停用，请重新发起请求。", 409
        )
    current_skills = set(current["skill_ids"])
    for skill in snapshot["skill_versions"]:
        if skill["skill_id"] not in current_skills:
            raise AppError("AGENT_AUTHORITY_CHANGED", "Agent Skill 已撤销，请重新发起请求。", 409)
        state = conn.execute(
            "SELECT enabled FROM agent_skills WHERE skill_id=%s", (skill["skill_id"],)
        ).fetchone()
        if not state or not state["enabled"]:
            raise AppError("AGENT_AUTHORITY_CHANGED", "Agent Skill 已停用，请重新发起请求。", 409)
    old_mode, new_mode = snapshot["knowledge_mode"], current["knowledge_mode"]
    if old_mode == "authorized" and new_mode != "authorized":
        raise AppError("AGENT_AUTHORITY_CHANGED", "Agent 知识范围已缩小，请重新发起请求。", 409)
    if old_mode == "selected" and (
        new_mode == "none"
        or (
            new_mode == "selected"
            and not set(snapshot["knowledge_document_ids"])
            <= set(current["knowledge_document_ids"])
        )
    ):
        raise AppError("AGENT_AUTHORITY_CHANGED", "Agent 知识范围已缩小，请重新发起请求。", 409)
