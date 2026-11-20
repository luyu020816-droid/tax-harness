"""Shared visibility and fail-closed content projection for trace readers."""

import re

from app.errors import AppError
from app.resource_access import TURN_SCOPES, TURN_VISIBLE
from app.sources import read_source
from app.values import safe_json

SCOPES = TURN_SCOPES
VISIBLE = TURN_VISIBLE


def assert_turn(conn, auth, turn_id):
    row = conn.execute(
        "SELECT t.* FROM turns t WHERE t.turn_id=%(turn)s AND " + VISIBLE,
        {"turn": turn_id, "user": auth.user_id},
    ).fetchone()
    if not row:
        raise AppError("RESOURCE_NOT_FOUND", "运行记录不存在或不可访问。", 404)
    return row


def content_allowed(conn, auth, run_id):
    refs = conn.execute(
        "SELECT DISTINCT s.source_id,s.revision FROM sources s JOIN runs r ON r.turn_id=s.turn_id WHERE r.run_id=%s "
        "UNION SELECT (ref->>'source_id')::uuid,(ref->>'revision')::integer FROM context_snapshots c "
        "CROSS JOIN LATERAL jsonb_array_elements(coalesce(c.manifest->'sources','[]')) ref WHERE c.run_id=%s",
        (run_id, run_id),
    ).fetchall()
    if not refs:
        return False
    try:
        from app.workbook_queries import validate_dependency as validate_workbook

        turn = conn.execute("SELECT turn_id FROM runs WHERE run_id=%s", (run_id,)).fetchone()
        for ref in refs:
            source = read_source(conn, auth, str(ref["source_id"]))
            if source["revision"] != ref["revision"]:
                return False
        # Private uploaded knowledge can become disabled without suppressing the user source.
        for row in conn.execute(
            "SELECT manifest FROM context_snapshots WHERE run_id=%s", (run_id,)
        ).fetchall():
            from app.knowledge_retrieval import validate_retrievals

            validate_retrievals(conn, auth, row["manifest"].get("retrieval_refs", []))
            for dependency in row["manifest"].get("workbook_dependencies", []):
                validate_workbook(conn, auth, turn["turn_id"], dependency)
            from app.preference_access import validate_dependencies

            validate_dependencies(
                conn, auth, row["manifest"].get("dependencies", []),
                row["manifest"].get("company_id"),
            )

            def check_embedded(value):
                if isinstance(value, dict):
                    if "source_id" in value and "revision" in value:
                        source = read_source(conn, auth, str(value["source_id"]))
                        if source["revision"] != value["revision"]:
                            raise AppError("SOURCE_INVALIDATED", "嵌入来源已变化。", 409)
                    for v in value.values():
                        check_embedded(v)
                elif isinstance(value, list):
                    for v in value:
                        check_embedded(v)

            check_embedded(row["manifest"])
    except AppError:
        return False
    return True


def redact(value):
    """Defence in depth; primary protection is source/permission validation."""
    if isinstance(value, dict):
        return {
            k: "[已隐藏凭据]"
            if re.search(
                r"(?i)(api.?key|authorization|cookie|password|database_url|connection_string)", k
            )
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9_.~+/=-]+", "Bearer [已隐藏]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[已隐藏密钥]", value)
        return re.sub(r'(?i)postgres(?:ql)?://[^\s"<>]+', "[已隐藏连接串]", value)
    return value


def public_run(db, auth, run_id):
    from app.trace import read_run

    with db.transaction(readonly=True) as conn:
        row = conn.execute("SELECT turn_id FROM runs WHERE run_id=%s", (run_id,)).fetchone()
        if not row:
            raise AppError("RESOURCE_NOT_FOUND", "运行记录不存在或不可访问。", 404)
        assert_turn(conn, auth, str(row["turn_id"]))
        allowed = content_allowed(conn, auth, run_id)
        result = read_run(db, auth, run_id, conn=conn)
        if not allowed:
            for context in result["contexts"]:
                context["payload"] = {"redacted": True, "reason": "来源失效或无法验证"}
                context["manifest"] = {"redacted": True, "kind": context["manifest"].get("kind")}
            for tool in result["tools"]:
                tool["arguments"], tool["actual_result"] = {"redacted": True}, None
            for event in result["events"]:
                event["payload"] = {"redacted": True}
        return safe_json(redact(result))
