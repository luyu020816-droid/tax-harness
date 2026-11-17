"""Manage registered tool availability and report referenced agent profiles."""

import hashlib
from pathlib import Path

from app.agent_capabilities import ACTION_TOOLS, ALIASES
from app.agent_profiles import _require_manager
from app.agent_skills import record_event
from app.errors import AppError
from app.tool_schemas import TOOLS
from app.values import digest, safe_json


def _source_bundle_id():
    """Fingerprint the loaded application's source bundle, not a switchable version."""
    root = Path(__file__).parent
    fingerprint = hashlib.sha256()
    for path in sorted(root.glob("*.py")):
        fingerprint.update(path.name.encode("utf-8"))
        fingerprint.update(b"\0")
        fingerprint.update(path.read_bytes())
    return fingerprint.hexdigest()


SOURCE_BUNDLE_ID = _source_bundle_id()
READ_ONLY_TOOLS = frozenset(
    {
        "search_conversations",
        "read_conversation",
        "read_summary_source",
        "search_knowledge",
        "read_evidence",
        "get_invoice_operation",
        "get_invoice_import",
        "query_month",
        "find_invoices",
        "get_rules",
        "check_purposes",
        "get_task",
        "search_history",
        "read_history",
    }
)


def catalog(db):
    with db.transaction(readonly=True) as conn:
        states = {row["tool_name"]: row for row in conn.execute("SELECT * FROM agent_tool_states")}
        result = []
        for name, (schema, description) in TOOLS.items():
            if name in ALIASES:
                continue
            row = states.get(name)
            result.append(
                {
                    "name": name,
                    "description": description,
                    "implementation_version": SOURCE_BUNDLE_ID,
                    "contract_version": digest([name, schema.model_json_schema(), description]),
                    "risk_type": "read_only" if name in READ_ONLY_TOOLS else "side_effect",
                    "actions": sorted(ACTION_TOOLS.get(name, {"invoke"})),
                    "enabled": row["enabled"] if row else True,
                    "revision": row["revision"] if row else 0,
                }
            )
        return safe_json(result)


def set_enabled(db, auth, name: str, expected_revision: int, enabled: bool):
    if name not in TOOLS or name in ALIASES:
        raise AppError("RESOURCE_NOT_FOUND", "工具不存在。", 404)
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(8123419417)")
        _require_manager(conn, auth, lock=True)
        row = conn.execute(
            "SELECT * FROM agent_tool_states WHERE tool_name=%s FOR UPDATE", (name,)
        ).fetchone()
        if (row["revision"] if row else 0) != expected_revision:
            raise AppError("TOOL_CONFIG_CONFLICT", "工具状态已变化，请刷新后重试。", 409)
        affected = conn.execute(
            "SELECT p.agent_id,p.name FROM agent_profiles p JOIN agent_profile_versions v "
            "ON v.agent_id=p.agent_id AND v.version=p.current_version "
            "WHERE v.config->'tool_grants' ? %s ORDER BY p.name",
            (name,),
        ).fetchall()
        if row and row["enabled"] == enabled:
            return safe_json({**row, "affected_agents": affected})
        row = conn.execute(
            "INSERT INTO agent_tool_states(tool_name,enabled,revision,updated_by) VALUES(%s,%s,1,%s) "
            "ON CONFLICT(tool_name) DO UPDATE SET enabled=excluded.enabled,"
            "revision=agent_tool_states.revision+1,updated_by=excluded.updated_by,"
            "updated_at=clock_timestamp() RETURNING *",
            (name, enabled, auth.user_id),
        ).fetchone()
        record_event(conn, auth, "tool", name, "enable" if enabled else "disable", row["revision"])
        return safe_json({**row, "affected_agents": affected})
