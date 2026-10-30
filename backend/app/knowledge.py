"""Authoritative files, private documents and version lifecycle."""

import hashlib
from datetime import date
from pathlib import Path

from app.errors import AppError
from app.identity import assert_access
from app.knowledge_parser import inspect_file
from app.memory_changes import begin_change, finish_change
from app.values import safe_json, uid


def document_in(conn, auth, document_id, *, manage=False, lock=False):
    row = conn.execute(
        "SELECT * FROM knowledge_documents WHERE document_id=%s" + (" FOR UPDATE" if lock else ""),
        (document_id,),
    ).fetchone()
    if (
        not row
        or (row["scope"] == "user_private" and row["owner_id"] != auth.user_id)
        or (manage and (row["scope"] != "user_private" or row["owner_id"] != auth.user_id))
    ):
        raise AppError("RESOURCE_NOT_FOUND", "知识资料不存在或不可访问。", 404)
    try:
        assert_access(conn, auth, row["company_id"])
    except AppError as exc:
        raise AppError("RESOURCE_NOT_FOUND", "知识资料不存在或不可访问。", 404) from exc
    return row


def upload_access(conn, auth, company_id):
    grant = assert_access(conn, auth, company_id)
    if not grant["can_upload_knowledge"]:
        raise AppError("KNOWLEDGE_ACCESS_DENIED", "当前用户没有该企业的知识上传权限。", 403)


def upload_metadata(conn, auth, file_id, company_id):
    row = conn.execute(
        "SELECT f.file_id,f.original_name,f.sha256,f.company_id,v.version_id,v.state,v.edition,d.document_id,d.title "
        "FROM knowledge_files f JOIN knowledge_versions v USING(file_id) JOIN knowledge_documents d USING(document_id) "
        "WHERE f.file_id=%s AND f.owner_id=%s AND f.company_id=%s",
        (file_id, auth.user_id, company_id),
    ).fetchone()
    if not row:
        raise AppError("RESOURCE_NOT_FOUND", "知识附件不存在或与当前企业不一致。", 404)
    assert_access(conn, auth, company_id)
    return safe_json(row)


def bump(conn, owner_id, company_id, scope):
    if scope == "user_private":
        conn.execute("UPDATE users SET memory_epoch=memory_epoch+1 WHERE user_id=%s", (owner_id,))
        conn.execute(
            "UPDATE summaries SET status='invalidated' WHERE user_id=%s AND status='active'",
            (owner_id,),
        )
    else:
        conn.execute(
            "UPDATE company_heads SET knowledge_epoch=knowledge_epoch+1 WHERE company_id=%s",
            (company_id,),
        )


def _in_use(conn, document_id):
    return conn.execute(
        "SELECT 1 FROM rule_knowledge_refs k JOIN rule_versions r USING(rule_version_id) "
        "JOIN knowledge_versions v ON v.version_id=k.knowledge_version_id WHERE v.document_id=%s AND r.status='active' LIMIT 1",
        (document_id,),
    ).fetchone()


def require_ready_version(conn, version):
    profile = conn.execute(
        "SELECT 1 FROM retrieval_profiles WHERE profile_id=%s AND status='ready' FOR SHARE",
        (version["profile_id"],),
    ).fetchone()
    if version["state"] != "ready" or not profile:
        raise AppError("INDEX_NOT_READY", "新版本或检索profile尚未就绪，未发布。", 409)


def cancel_pending_in(conn, document_id):
    conn.execute(
        "UPDATE retrieval_jobs j SET state='cancelled',stage='cancelled',lease_until=NULL,fencing_token=fencing_token+1 "
        "FROM knowledge_versions v WHERE j.version_id=v.version_id AND v.document_id=%s AND j.state IN ('queued','running','blocked')",
        (document_id,),
    )
    conn.execute(
        "UPDATE knowledge_versions SET state='cancelled',error=NULL WHERE document_id=%s "
        "AND state IN ('queued','parsing','chunking','embedding','validating','blocked')",
        (document_id,),
    )


def publish_in(conn, document, version, expected_revision, *, notify=True):
    if (
        document["revision"] != expected_revision
        or version["document_id"] != document["document_id"]
    ):
        raise AppError("KNOWLEDGE_VERSION_CONFLICT", "文档已变化，请核对当前版本。", 409)
    require_ready_version(conn, version)
    if (
        _in_use(conn, document["document_id"])
        and document["active_version_id"] != version["version_id"]
    ):
        raise AppError("KNOWLEDGE_IN_USE", "资料正在作为生效规则依据，需要同时维护关联规则。", 409)
    row = conn.execute(
        "UPDATE knowledge_documents SET active_version_id=%s,status='active',revision=revision+1,updated_at=now() "
        "WHERE document_id=%s AND revision=%s RETURNING *",
        (version["version_id"], document["document_id"], expected_revision),
    ).fetchone()
    if not row:
        raise AppError("KNOWLEDGE_VERSION_CONFLICT", "候选版本发布冲突，原有效版本保持不变。", 409)
    if notify:
        bump(conn, row["owner_id"], row["company_id"], row["scope"])
    return row


class Knowledge:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def upload(
        self,
        auth,
        *,
        name,
        content,
        company_id,
        title,
        request_id,
        document_id=None,
        expected_revision=None,
        effective_from=None,
        effective_to=None,
        maintenance=False,
    ):
        if not self.settings.knowledge_enabled:
            raise AppError("DEPENDENCY_UNAVAILABLE", "知识接入尚未启用。", 503)
        if not content or len(content) > self.settings.knowledge_max_file_bytes:
            raise AppError("DOCUMENT_TOO_LARGE", "文件不能为空且最多20 MiB。", 413)
        suffix, media = inspect_file(name, content)
        name = Path(name.replace("\\", "/")).name[:200]
        title = title.strip()
        if not title or len(title) > 200 or not request_id or len(request_id) > 200:
            raise AppError("INVALID_ARGUMENT", "标题和请求编号需为1—200字符。", 422)
        start, end = (
            date.fromisoformat(str(effective_from)) if effective_from else date.min,
            date.fromisoformat(str(effective_to)) if effective_to else None,
        )
        if end and end <= start:
            raise AppError("INVALID_ARGUMENT", "资料生效结束日期必须晚于开始日期。", 422)
        sha = hashlib.sha256(content).hexdigest()
        body = {
            "company_id": company_id,
            "title": title,
            "filename": name,
            "sha256": sha,
            "document_id": document_id,
            "expected_revision": expected_revision,
            "effective_from": str(start),
            "effective_to": str(end) if end else None,
        }
        with self.db.transaction() as conn:
            conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
            if (
                maintenance
                and not conn.execute(
                    "SELECT has_table_privilege(current_user,'knowledge_maintenance_ops','INSERT') AS allowed"
                ).fetchone()["allowed"]
            ):
                raise AppError("KNOWLEDGE_ACCESS_DENIED", "企业资料版本只能由本地维护者接入。", 403)
            upload_access(conn, auth, company_id)
            existing, change = begin_change(
                conn, auth, "knowledge", "upload", body, request_id, None, []
            )
            if change is None:
                return safe_json(existing)
            conn.execute(
                "SELECT 1 FROM company_heads WHERE company_id=%s FOR UPDATE", (company_id,)
            )
            quota = conn.execute(
                "SELECT coalesce(sum(byte_size),0) AS bytes,count(*) FILTER(WHERE created_at>now()-interval '1 hour') AS recent "
                "FROM knowledge_files WHERE owner_id=%s",
                (auth.user_id,),
            ).fetchone()
            pending = conn.execute(
                "SELECT count(*) AS n FROM retrieval_jobs WHERE user_id=%s AND state IN ('queued','running','blocked')",
                (auth.user_id,),
            ).fetchone()["n"]
            if (
                quota["bytes"] + len(content) > 200 * 1024 * 1024
                or quota["recent"] >= 20
                or pending >= 10
            ):
                raise AppError(
                    "KNOWLEDGE_QUOTA_EXCEEDED",
                    "资料总量、接入频率或待处理任务达到上限，请先处理现有任务。",
                    429,
                )
            if document_id:
                document = document_in(conn, auth, document_id, manage=not maintenance, lock=True)
                if maintenance and (
                    document["scope"] != "company" or document["owner_id"] != auth.user_id
                ):
                    raise AppError("KNOWLEDGE_ACCESS_DENIED", "维护目标不属于当前企业资料。", 403)
                if (
                    document["company_id"] != company_id
                    or document["revision"] != expected_revision
                ):
                    raise AppError("KNOWLEDGE_VERSION_CONFLICT", "资料企业或版本已变化。", 409)
            else:
                document = conn.execute(
                    "INSERT INTO knowledge_documents(document_id,owner_id,company_id,scope,title) "
                    "VALUES(%s,%s,%s,'user_private',%s) RETURNING *",
                    (uid(), auth.user_id, company_id, title),
                ).fetchone()
            file_id, version_id, job_id = uid(), uid(), uid()
            edition = conn.execute(
                "SELECT coalesce(max(edition),0)+1 AS n FROM knowledge_versions WHERE document_id=%s",
                (document["document_id"],),
            ).fetchone()["n"]
            conn.execute(
                "INSERT INTO knowledge_files(file_id,owner_id,company_id,original_name,media_type,file_bytes,byte_size,sha256,request_id,request_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    file_id,
                    auth.user_id,
                    company_id,
                    name,
                    media,
                    content,
                    len(content),
                    sha,
                    request_id,
                    sha,
                ),
            )
            conn.execute(
                "INSERT INTO knowledge_versions(version_id,document_id,owner_id,company_id,file_id,edition,effective_from,effective_to,expected_document_revision) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    version_id,
                    document["document_id"],
                    auth.user_id,
                    company_id,
                    file_id,
                    edition,
                    start,
                    end,
                    document["revision"],
                ),
            )
            conn.execute(
                "INSERT INTO retrieval_jobs(job_id,user_id,version_id) VALUES(%s,%s,%s)",
                (job_id, auth.user_id, version_id),
            )
            result = {
                "document_id": str(document["document_id"]),
                "version_id": version_id,
                "file_id": file_id,
                "job_id": job_id,
                "status": "queued",
                "company_id": company_id,
                "suffix": suffix,
            }
            return safe_json(finish_change(conn, auth, change, result))

    def list(self, auth, company_id, offset=0):
        if offset < 0 or offset > 10000:
            raise AppError("INVALID_ARGUMENT", "资料分页范围无效。", 422)
        with self.db.transaction(readonly=True) as conn:
            assert_access(conn, auth, company_id)
            rows = conn.execute(
                "SELECT d.*,v.edition AS active_edition,v.state AS active_state,"
                "(SELECT jsonb_build_object('version_id',x.version_id,'edition',x.edition,'state',x.state,'job_id',j.job_id,'error',x.error) "
                "FROM knowledge_versions x JOIN retrieval_jobs j USING(version_id) WHERE x.document_id=d.document_id AND (d.owner_id=%s OR x.version_id=d.active_version_id) ORDER BY x.edition DESC LIMIT 1) AS latest "
                "FROM knowledge_documents d LEFT JOIN knowledge_versions v ON v.version_id=d.active_version_id WHERE d.company_id=%s "
                "AND (d.scope='company' OR d.owner_id=%s) ORDER BY d.created_at DESC,d.document_id LIMIT 21 OFFSET %s",
                (auth.user_id, company_id, auth.user_id, offset),
            ).fetchall()
            return safe_json(
                {
                    "documents": rows[:20],
                    "has_more": len(rows) > 20,
                    "next_offset": offset + 20 if len(rows) > 20 else None,
                }
            )

    def get(self, auth, document_id):
        with self.db.transaction(readonly=True) as conn:
            document = document_in(conn, auth, document_id)
            versions = conn.execute(
                "SELECT v.*,f.original_name,f.sha256,j.job_id,j.stage,j.progress FROM knowledge_versions v JOIN knowledge_files f USING(file_id) "
                "JOIN retrieval_jobs j USING(version_id) WHERE v.document_id=%s AND (%s OR v.version_id=%s) ORDER BY v.edition DESC",
                (
                    document_id,
                    document["owner_id"] == auth.user_id,
                    document["active_version_id"] if document["status"] == "active" else None,
                ),
            ).fetchall()
            return safe_json({**document, "versions": versions})

    def file(self, auth, document_id, version_id=None):
        with self.db.transaction(readonly=True) as conn:
            document = document_in(conn, auth, document_id)
            version_id = version_id or document["active_version_id"]
            if document["owner_id"] != auth.user_id and (
                document["status"] != "active"
                or str(version_id) != str(document["active_version_id"])
            ):
                raise AppError("RESOURCE_NOT_FOUND", "未发布或停用的资料版本不可访问。", 404)
            row = conn.execute(
                "SELECT f.* FROM knowledge_versions v JOIN knowledge_files f USING(file_id) WHERE v.document_id=%s AND v.version_id=%s",
                (document_id, version_id),
            ).fetchone()
            if not row:
                raise AppError("RESOURCE_NOT_FOUND", "资料版本不可访问。", 404)
            if hashlib.sha256(bytes(row["file_bytes"])).hexdigest() != row["sha256"]:
                raise AppError("SOURCE_INVALIDATED", "原文件完整性校验失败，暂停读取。", 503)
            return row

    def job(self, auth, job_id):
        with self.db.transaction(readonly=True) as conn:
            row = conn.execute(
                "SELECT j.*,v.document_id FROM retrieval_jobs j JOIN knowledge_versions v USING(version_id) WHERE j.job_id=%s AND j.user_id=%s",
                (job_id, auth.user_id),
            ).fetchone()
            if not row:
                raise AppError("RESOURCE_NOT_FOUND", "接入任务不存在或不可访问。", 404)
            document_in(conn, auth, str(row["document_id"]))
            return safe_json(row)

    def retry(self, auth, job_id, request_id):
        with self.db.transaction() as conn:
            conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
            row = conn.execute(
                "SELECT j.*,v.document_id FROM retrieval_jobs j JOIN knowledge_versions v USING(version_id) WHERE j.job_id=%s AND j.user_id=%s FOR UPDATE OF j",
                (job_id, auth.user_id),
            ).fetchone()
            if not row:
                raise AppError("RESOURCE_NOT_FOUND", "接入任务不可访问。", 404)
            document = document_in(conn, auth, str(row["document_id"]), manage=True)
            upload_access(conn, auth, document["company_id"])
            if row["retry_request_id"] == request_id or row["state"] in (
                "queued",
                "running",
                "done",
            ):
                return safe_json(row)
            if row["state"] not in ("failed", "blocked"):
                raise AppError("KNOWLEDGE_VERSION_CONFLICT", "该任务不可重试。", 409)
            conn.execute(
                "UPDATE knowledge_versions SET state='queued',error=NULL WHERE version_id=%s",
                (row["version_id"],),
            )
            return safe_json(
                conn.execute(
                    "UPDATE retrieval_jobs SET state='queued',stage='queued',error=NULL,retry_request_id=%s,fencing_token=fencing_token+1 WHERE job_id=%s RETURNING *",
                    (request_id, job_id),
                ).fetchone()
            )

    def change(self, auth, document_id, *, action, expected_revision, request_id, version_id=None):
        if action not in ("publish", "disable"):
            raise AppError("INVALID_ARGUMENT", "不支持的知识维护动作。", 422)
        with self.db.transaction() as conn:
            conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
            document = document_in(conn, auth, document_id, manage=True)
            conn.execute(
                "SELECT 1 FROM company_heads WHERE company_id=%s FOR UPDATE",
                (document["company_id"],),
            )
            existing, change = begin_change(
                conn,
                auth,
                "knowledge",
                action,
                {
                    "company_id": document["company_id"],
                    "document_id": document_id,
                    "version_id": version_id,
                    "expected_revision": expected_revision,
                },
                request_id,
                None,
                [],
            )
            if change is None:
                return safe_json(existing)
            document = document_in(conn, auth, document_id, manage=True, lock=True)
            if document["revision"] != expected_revision:
                raise AppError("KNOWLEDGE_VERSION_CONFLICT", "资料已变化，请刷新。", 409)
            if action == "publish":
                version = conn.execute(
                    "SELECT * FROM knowledge_versions WHERE document_id=%s AND version_id=%s",
                    (document_id, version_id),
                ).fetchone()
                if not version:
                    raise AppError("RESOURCE_NOT_FOUND", "该文档没有这个版本。", 404)
                result = publish_in(conn, document, version, expected_revision, notify=False)
            else:
                if _in_use(conn, document_id):
                    raise AppError(
                        "KNOWLEDGE_IN_USE", "资料仍被生效业务规则引用，不能单独停用。", 409
                    )
                result = conn.execute(
                    "UPDATE knowledge_documents SET status='disabled',revision=revision+1,updated_at=now() WHERE document_id=%s RETURNING *",
                    (document_id,),
                ).fetchone()
                cancel_pending_in(conn, document_id)
            return safe_json(finish_change(conn, auth, change, result))
