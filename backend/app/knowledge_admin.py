"""Local-maintainer operations, never exposed as chat tools or HTTP mutations."""

from app.errors import AppError
from app.knowledge import _in_use, bump, cancel_pending_in, require_ready_version
from app.knowledge_parser import text_hash
from app.values import digest, jsonb, safe_json


def _replayed(conn, request_id, action, payload):
    if not request_id or len(request_id) > 200:
        raise AppError("INVALID_ARGUMENT", "维护请求编号需为1—200字符。", 422)
    expected = digest([action, payload])
    row = conn.execute(
        "SELECT * FROM knowledge_maintenance_ops WHERE request_id=%s", (request_id,)
    ).fetchone()
    if row:
        if row["request_hash"] != expected:
            raise AppError("IDEMPOTENCY_CONFLICT", "维护请求编号已用于其他内容。", 409)
        return row["result"]
    return None


def _record(conn, request_id, action, payload, result):
    conn.execute(
        "INSERT INTO knowledge_maintenance_ops(request_id,request_hash,action,result) VALUES(%s,%s,%s,%s)",
        (request_id, digest([action, payload]), action, jsonb(result)),
    )
    return safe_json(result)


def publish_company(conn, document_id, version_id, expected_revision, request_id):
    payload = [document_id, version_id, expected_revision]
    document = conn.execute(
        "SELECT * FROM knowledge_documents WHERE document_id=%s", (document_id,)
    ).fetchone()
    if not document:
        raise AppError("RESOURCE_NOT_FOUND", "待发布资料不存在。", 404)
    conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (document["owner_id"],))
    conn.execute(
        "SELECT 1 FROM company_heads WHERE company_id=%s FOR UPDATE", (document["company_id"],)
    )
    if replayed := _replayed(conn, request_id, "publish_company", payload):
        return replayed
    document = conn.execute(
        "SELECT * FROM knowledge_documents WHERE document_id=%s FOR UPDATE", (document_id,)
    ).fetchone()
    version = conn.execute(
        "SELECT * FROM knowledge_versions WHERE document_id=%s AND version_id=%s AND state='ready'",
        (document_id, version_id),
    ).fetchone()
    if document["revision"] != expected_revision or not version:
        raise AppError("KNOWLEDGE_VERSION_CONFLICT", "资料版本已变化或尚未就绪。", 409)
    require_ready_version(conn, version)
    if _in_use(conn, document_id) and document["active_version_id"] != version["version_id"]:
        raise AppError("KNOWLEDGE_IN_USE", "需要先维护关联的生效规则，不能单独替换资料。", 409)
    row = conn.execute(
        "UPDATE knowledge_documents SET scope='company',active_version_id=%s,status='active',revision=revision+1,updated_at=now() WHERE document_id=%s RETURNING *",
        (version_id, document_id),
    ).fetchone()
    if document["scope"] == "user_private":
        bump(conn, document["owner_id"], document["company_id"], "user_private")
    bump(conn, document["owner_id"], document["company_id"], "company")
    return _record(conn, request_id, "publish_company", payload, row)


def disable_company(conn, document_id, expected_revision, request_id):
    payload = [document_id, expected_revision]
    document = conn.execute(
        "SELECT * FROM knowledge_documents WHERE document_id=%s AND scope='company'", (document_id,)
    ).fetchone()
    if not document:
        raise AppError("RESOURCE_NOT_FOUND", "企业资料不存在。", 404)
    conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (document["owner_id"],))
    conn.execute(
        "SELECT 1 FROM company_heads WHERE company_id=%s FOR UPDATE", (document["company_id"],)
    )
    if replayed := _replayed(conn, request_id, "disable_company", payload):
        return replayed
    document = conn.execute(
        "SELECT * FROM knowledge_documents WHERE document_id=%s FOR UPDATE", (document_id,)
    ).fetchone()
    if document["revision"] != expected_revision or _in_use(conn, document_id):
        raise AppError(
            "KNOWLEDGE_IN_USE" if _in_use(conn, document_id) else "KNOWLEDGE_VERSION_CONFLICT",
            "资料版本已变化或仍被生效规则使用。",
            409,
        )
    result = conn.execute(
        "UPDATE knowledge_documents SET status='disabled',revision=revision+1 WHERE document_id=%s RETURNING *",
        (document_id,),
    ).fetchone()
    cancel_pending_in(conn, document_id)
    bump(conn, document["owner_id"], document["company_id"], "company")
    return _record(conn, request_id, "disable_company", payload, result)


def validate_rule_refs(conn, rule_id, companies=None):
    refs = conn.execute(
        "SELECT k.*,d.scope,d.status,d.active_version_id,v.effective_from,v.effective_to,v.state,b.content,b.content_hash "
        "FROM rule_knowledge_refs k JOIN knowledge_versions v ON v.version_id=k.knowledge_version_id "
        "JOIN knowledge_documents d USING(document_id) JOIN knowledge_blocks b USING(block_id) WHERE k.rule_version_id=%s",
        (rule_id,),
    ).fetchall()
    if not refs:
        return []
    rule = conn.execute(
        "SELECT * FROM rule_versions WHERE rule_version_id=%s", (rule_id,)
    ).fetchone()
    required = set(
        companies
        or [
            r["company_id"]
            for r in conn.execute(
                "SELECT company_id FROM rule_scopes WHERE rule_version_id=%s", (rule_id,)
            ).fetchall()
        ]
    )
    if not required <= {r["company_id"] for r in refs}:
        raise AppError("RULE_CONFLICT", "规则缺少完整企业范围的知识依据。", 409)
    for ref in refs:
        if ref["company_id"] not in required:
            continue
        if (
            ref["scope"] != "company"
            or ref["status"] != "active"
            or ref["state"] != "ready"
            or ref["active_version_id"] != ref["knowledge_version_id"]
            or ref["content_hash"] != ref["excerpt_hash"]
            or text_hash(ref["content"]) != ref["excerpt_hash"]
            or ref["content"].replace("\r\n", "\n").strip()
            != rule["full_text"].replace("\r\n", "\n").strip()
            or ref["effective_from"] > rule["effective_from"]
            or (
                ref["effective_to"]
                and (rule["effective_to"] is None or ref["effective_to"] < rule["effective_to"])
            )
        ):
            raise AppError("RULE_CONFLICT", "规则知识依据失效、范围不完整或与执行正文不一致。", 409)
    return safe_json(
        [
            {
                k: ref[k]
                for k in (
                    "company_id",
                    "knowledge_version_id",
                    "block_id",
                    "locator",
                    "excerpt_hash",
                )
            }
            for ref in refs
            if ref["company_id"] in required
        ]
    )


def link_rule(conn, rule_id, references, request_id):
    rule = conn.execute(
        "SELECT * FROM rule_versions WHERE rule_version_id=%s", (rule_id,)
    ).fetchone()
    if not rule:
        raise AppError("RESOURCE_NOT_FOUND", "规则不存在。", 404)
    anchors = []
    for ref in references:
        row = conn.execute(
            "SELECT b.*,v.company_id,v.document_id,d.owner_id FROM knowledge_blocks b JOIN knowledge_versions v USING(version_id) JOIN knowledge_documents d USING(document_id) "
            "WHERE b.block_id=%s AND b.version_id=%s",
            (ref["block_id"], ref["knowledge_version_id"]),
        ).fetchone()
        if not row:
            raise AppError("RESOURCE_NOT_FOUND", "规则来源定位不存在。", 404)
        anchors.append(row)
    companies = [
        r["company_id"]
        for r in conn.execute(
            "SELECT company_id FROM rule_scopes WHERE rule_version_id=%s ORDER BY company_id",
            (rule_id,),
        ).fetchall()
    ]
    if len(anchors) != len(companies) or {a["company_id"] for a in anchors} != set(companies):
        raise AppError("RULE_SCOPE_MISMATCH", "规则知识依据必须逐一覆盖完整企业范围。", 409)
    conn.execute(
        "SELECT 1 FROM users WHERE user_id=ANY(%s) ORDER BY user_id FOR UPDATE",
        (sorted({a["owner_id"] for a in anchors}),),
    )
    conn.execute(
        "SELECT 1 FROM company_heads WHERE company_id=ANY(%s) ORDER BY company_id FOR UPDATE",
        (companies,),
    )
    payload = [rule_id, sorted(references, key=lambda r: r["knowledge_version_id"])]
    if replayed := _replayed(conn, request_id, "link_rule", payload):
        return replayed
    for anchor in sorted(anchors, key=lambda a: str(a["document_id"])):
        conn.execute(
            "SELECT 1 FROM knowledge_documents WHERE document_id=%s FOR UPDATE",
            (anchor["document_id"],),
        )
        conn.execute(
            "INSERT INTO rule_knowledge_refs(rule_version_id,knowledge_version_id,block_id,locator,excerpt_hash,company_id) VALUES(%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(rule_version_id,company_id) DO UPDATE SET knowledge_version_id=excluded.knowledge_version_id,block_id=excluded.block_id,locator=excluded.locator,excerpt_hash=excluded.excerpt_hash",
            (
                rule_id,
                anchor["version_id"],
                anchor["block_id"],
                jsonb(anchor["locator"]),
                anchor["content_hash"],
                anchor["company_id"],
            ),
        )
    validate_rule_refs(conn, rule_id)
    conn.execute(
        "UPDATE rule_versions SET content_revision=content_revision+1 WHERE rule_version_id=%s",
        (rule_id,),
    )
    conn.execute(
        "UPDATE company_heads SET rules_epoch=rules_epoch+1,knowledge_epoch=knowledge_epoch+1 WHERE company_id=ANY(%s)",
        (companies,),
    )
    return _record(
        conn,
        request_id,
        "link_rule",
        payload,
        {"rule_version_id": rule_id, "references": validate_rule_refs(conn, rule_id)},
    )


def revoke_read(conn, user_id, company_id, request_id):
    conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (user_id,))
    conn.execute("SELECT 1 FROM company_heads WHERE company_id=%s FOR UPDATE", (company_id,))
    payload = [user_id, company_id]
    if replayed := _replayed(conn, request_id, "revoke_read", payload):
        return replayed
    conn.execute(
        "UPDATE user_company_grants SET can_read=false,can_update=false,can_upload_knowledge=false,can_import_invoices=false WHERE user_id=%s AND company_id=%s",
        (user_id, company_id),
    )
    bump(conn, user_id, company_id, "user_private")
    bump(conn, user_id, company_id, "company")
    return _record(
        conn,
        request_id,
        "revoke_read",
        payload,
        {"user_id": user_id, "company_id": company_id, "can_read": False},
    )
