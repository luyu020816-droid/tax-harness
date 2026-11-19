"""Read-only Agent snapshot for previews; acceptance still revalidates and binds."""

from app.errors import AppError


def configuration(conn, agent_id):
    conn.execute("SELECT pg_advisory_xact_lock_shared(8123419417)")
    row = conn.execute(
        "SELECT p.agent_id,p.current_version AS agent_version,v.config AS snapshot FROM agent_profiles p JOIN agent_profile_versions v ON v.agent_id=p.agent_id AND v.version=p.current_version WHERE p.agent_id=%s AND p.enabled",
        (agent_id,),
    ).fetchone()
    if not row:
        raise AppError("AGENT_UNAVAILABLE", "Agent 未启用或尚未发布。", 409)
    disabled = {
        r["tool_name"]
        for r in conn.execute("SELECT tool_name FROM agent_tool_states WHERE NOT enabled")
    }
    row["snapshot"]["tool_grants"] = {
        name: actions
        for name, actions in row["snapshot"]["tool_grants"].items()
        if name not in disabled
    }
    row["snapshot"]["skill_versions"] = [
        ref
        for ref in row["snapshot"]["skill_versions"]
        if conn.execute(
            "SELECT 1 FROM agent_skills WHERE skill_id=%s AND enabled", (ref["skill_id"],)
        ).fetchone()
    ]
    return row
