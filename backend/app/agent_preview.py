"""Read-only Agent snapshot for previews; acceptance still revalidates and binds."""

from app.agent_authority import LOCK_ID, effective_snapshot
from app.errors import AppError


def configuration(conn, agent_id):
    conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (LOCK_ID,))
    row = conn.execute(
        "SELECT p.agent_id,p.current_version AS agent_version,v.config AS snapshot FROM agent_profiles p JOIN agent_profile_versions v ON v.agent_id=p.agent_id AND v.version=p.current_version WHERE p.agent_id=%s AND p.enabled",
        (agent_id,),
    ).fetchone()
    if not row:
        raise AppError("AGENT_UNAVAILABLE", "Agent 未启用或尚未发布。", 409)
    row["snapshot"] = effective_snapshot(conn, row["snapshot"])
    return row
