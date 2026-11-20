"""Authorization and provenance for active and historical preference reads."""

import json
from uuid import UUID

from app.errors import AppError
from app.identity import assert_access
from app.preference_rules import clean, display_format, preference_evidence
from app.sources import add_source, assess, read_source

_GLOBAL = "@user"


def company_names(conn):
    return {
        row["company_id"]: tuple(filter(None, (row["company_id"], row["name"], *row["aliases"])))
        for row in conn.execute("SELECT company_id,name,aliases FROM companies")
    }


def _mentioned(text, catalogue):
    text = clean(text)
    return {
        company
        for company, names in catalogue.items()
        if any(clean(name) in text for name in names)
    }


def scope_error():
    return AppError(
        "MEMORY_SCOPE_MISMATCH", "偏好适用范围与用户原话或来源不一致，请明确使用范围。", 422
    )


def authorize_scope(source, scope, value, signature, catalogue):
    text = source["content"]
    matches = _mentioned(text, catalogue)
    local = any(phrase in text for phrase in ("本企业", "当前企业", "这家企业", "这一家", "本公司"))
    if scope == _GLOBAL:
        if (
            matches
            or local
            or (value is not None and source["scope_kind"] != "user_private" and signature is None)
        ):
            raise scope_error()
    elif (matches and matches != {scope}) or any(
        phrase in text for phrase in ("所有企业", "全部企业", "全局偏好", "个人全局")
    ):
        raise scope_error()


def personal_authorization(conn, auth, source, change, value, signature):
    evidence = add_source(
        conn,
        auth,
        {"value": value, "dimension": signature[0], "scene": signature[1]},
        role="user",
        kind="personal_preference_authorization",
        source_seq=change["source_seq"],
    )
    assess(conn, str(evidence["source_id"]), "accepted")
    return [
        {
            "source_id": str(evidence["source_id"]),
            "quote": evidence["content"],
            "origin_source_id": str(source["source_id"]),
        }
    ]


def can_read(conn, *, turn_id=None, binding=None):
    if turn_id:
        from app.agent_authority import policy_for_turn

        policy = policy_for_turn(conn, turn_id)
        if policy is not None:
            return "list" in policy.actions("manage_preference")
    if binding is not None:
        return "list" in binding["snapshot"]["tool_grants"].get("manage_preference", ())
    return True


def sources_in(conn, auth, row, company, *, catalogue=None):
    if row["scope_key"] not in (_GLOBAL, company) or not row["source_refs"]:
        raise AppError("SOURCE_INVALIDATED", "偏好的来源或适用范围不可用。", 409)
    if row["scope_key"] != _GLOBAL:
        assert_access(conn, auth, row["scope_key"])
    result = []
    for ref in row["source_refs"]:
        try:
            source_id = str(UUID(str(ref["source_id"])))
        except (TypeError, KeyError, ValueError) as exc:
            raise AppError("SOURCE_INVALIDATED", "偏好记录的来源编号无效。", 409) from exc
        source = read_source(
            conn,
            auth,
            source_id,
            company_id=(row["scope_key"] if row["scope_key"] != _GLOBAL else None),
            allow_suppressed=True,
        )
        if (
            source["role"] != "user"
            or not ref.get("quote")
            or ref["quote"] not in source["content"]
        ):
            raise AppError("SOURCE_INVALIDATED", "偏好缺少可核验的用户来源。", 409)
        if source["kind"] in ("user_action", "personal_preference_authorization"):
            try:
                confirmed = json.loads(source["content"])["value"]
            except (KeyError, TypeError, ValueError) as exc:
                raise AppError("SOURCE_INVALIDATED", "偏好确认记录无法核验。", 409) from exc
            if confirmed != row.get("value"):
                raise AppError("SOURCE_INVALIDATED", "偏好内容与用户确认记录不一致。", 409)
            if row["scope_key"] == _GLOBAL and confirmed is not None:
                try:
                    authorize_scope(
                        {**source, "content": confirmed},
                        _GLOBAL,
                        confirmed,
                        display_format(confirmed),
                        catalogue if catalogue is not None else company_names(conn),
                    )
                except AppError as exc:
                    raise AppError(
                        "SOURCE_INVALIDATED", "旧偏好的作用范围需要重新确认。", 409
                    ) from exc
        if row["scope_key"] == _GLOBAL and source["kind"] not in (
            "user_action",
            "personal_preference_authorization",
        ):
            # Legacy conversation preferences are valid globally only if their stored scope
            # is supported by the original user request and contains no enterprise material.
            value = row.get("value")
            try:
                signature = (
                    preference_evidence(source["content"], [ref["quote"]], "remember", value)[0]
                    if value is not None
                    else None
                )
                authorize_scope(
                    source,
                    _GLOBAL,
                    value,
                    signature,
                    catalogue if catalogue is not None else company_names(conn),
                )
            except AppError as exc:
                raise AppError("SOURCE_INVALIDATED", "旧偏好的作用范围需要重新确认。", 409) from exc
        result.append({"source_id": source["source_id"], "revision": source["revision"]})
    return result


def invalid_source(exc):
    return exc.code in ("ACCESS_DENIED", "SOURCE_INVALIDATED", "STATE_SOURCE_UNAVAILABLE")


def validate_dependencies(
    conn, auth, dependencies, company, *, reader_turn_id=None, historical=False
):
    refs = [dependency for dependency in dependencies if dependency.get("kind") == "preference"]
    if refs and not can_read(conn, turn_id=reader_turn_id):
        raise AppError("SOURCE_INVALIDATED", "当前 Agent 无权使用这项记忆。", 409)
    for ref in refs:
        if historical:
            preference = conn.execute(
                "SELECT r.*,p.scope_key FROM preference_revisions r JOIN preferences p USING(preference_id) "
                "WHERE p.preference_id=%s AND p.user_id=%s AND r.revision=%s",
                (ref["id"], auth.user_id, ref["revision"]),
            ).fetchone()
        else:
            preference = conn.execute(
                "SELECT * FROM preferences WHERE preference_id=%s AND user_id=%s AND status='active'",
                (ref["id"], auth.user_id),
            ).fetchone()
        if not preference or any(preference[key] != ref[key] for key in ("revision", "generation")):
            raise AppError("SOURCE_INVALIDATED", "回答使用的偏好版本已失效。", 409)
        try:
            sources_in(conn, auth, preference, company)
        except AppError as exc:
            if not invalid_source(exc):
                raise
            raise AppError("SOURCE_INVALIDATED", "回答使用的偏好来源已失效。", 409) from exc


def validate_answer(conn, auth, source_id, *, company=None, reader_turn_id=None, historical=False):
    for record in conn.execute(
        "SELECT c.manifest FROM context_snapshots c JOIN runs r USING(run_id) "
        "WHERE r.final_source_id=%s",
        (source_id,),
    ):
        manifest = record["manifest"]
        validate_dependencies(
            conn,
            auth,
            manifest.get("dependencies", []),
            company or manifest.get("company_id"),
            reader_turn_id=reader_turn_id,
            historical=historical,
        )
