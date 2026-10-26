"""User-owned invoice ingestion with frozen previews and atomic append receipts."""

import hashlib
import secrets
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.conninfo import conninfo_to_dict

from app.errors import AppError
from app.execution import guard
from app.identity import assert_access, assert_import_access, owned
from app.recovery import in_maintenance
from app.sources import add_source, advance_progress, assess, next_sequence
from app.values import digest, is_blank, jsonb, safe_json, uid
from app.xlsx_invoices import PARSER_VERSION, file_hash, parse_invoice_workbook, validate_archive

TERMINAL = ("committed", "no_change", "cancelled")
BATCH_FIELDS = (
    "batch_id",
    "upload_id",
    "company_id",
    "revision",
    "status",
    "policy",
    "parser_version",
    "preview_session_id",
    "preview_data_revision",
    "preview_rules_epoch",
    "preview_hash",
    "token_expires_at",
    "counts_json",
    "receipt_json",
    "source_refs",
    "created_at",
    "committed_at",
)
UPLOAD_FIELDS = "upload_id,source_id,user_id,company_id,session_id,original_name,file_sha256,byte_size,created_at"


def upload_metadata(conn, auth, upload_id, company_id=None):
    row = conn.execute(
        f"SELECT {UPLOAD_FIELDS} FROM file_uploads WHERE upload_id=%s AND user_id=%s",
        (upload_id, auth.user_id),
    ).fetchone()
    if not row:
        raise AppError("RESOURCE_NOT_FOUND", "附件不存在或不可访问。", 404)
    assert_access(conn, auth, row["company_id"])
    if company_id and row["company_id"] != company_id:
        raise AppError("IMPORT_ACCESS_DENIED", "附件不属于当前企业。", 403)
    return row


def _batch(conn, auth, batch_id, *, lock=False):
    row = conn.execute(
        "SELECT * FROM invoice_import_batches WHERE batch_id=%s AND user_id=%s"
        + (" FOR UPDATE" if lock else ""),
        (batch_id, auth.user_id),
    ).fetchone()
    if not row:
        raise AppError("RESOURCE_NOT_FOUND", "导入批次不存在或不可访问。", 404)
    assert_access(conn, auth, row["company_id"])
    return row


def _session(conn, auth, session_id, company):
    session = owned(conn, "sessions", "session_id", session_id, auth)
    if session["selected_company_id"] != company:
        raise AppError("IMPORT_ACCESS_DENIED", "请在同一企业的会话中预览和确认。", 403)


def _lock(conn, auth, db, execution=None):
    if in_maintenance(db.dsn):
        raise AppError("MAINTENANCE", "数据库维护核对中，暂不接受导入。", 503)
    if execution:
        guard(conn, execution)
    else:
        conn.execute("SELECT user_id FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))


def _replay(conn, auth, request_id, body):
    if not request_id or len(request_id) > 200:
        raise AppError("INVALID_ARGUMENT", "请求编号不能为空且最多 200 字符。")
    row = conn.execute(
        "SELECT request_hash,result_ref FROM memory_change_ops WHERE user_id=%s AND request_id=%s",
        (auth.user_id, request_id),
    ).fetchone()
    if row and row["request_hash"] != digest(body):
        raise AppError("IDEMPOTENCY_CONFLICT", "同一请求编号不能用于不同导入内容。", 409)
    return row["result_ref"] if row else None


def _result(conn, auth, batch_id, offset=0):
    batch = _batch(conn, auth, batch_id)
    rows = conn.execute(
        "SELECT row_number,normalized_values,classification,reason_code,reason_message,compared_values FROM invoice_import_rows WHERE batch_id=%s ORDER BY row_number LIMIT 21 OFFSET %s",
        (batch_id, offset),
    ).fetchall()
    epoch = conn.execute(
        "SELECT memory_epoch FROM users WHERE user_id=%s", (auth.user_id,)
    ).fetchone()["memory_epoch"]
    receipt = batch["receipt_json"]
    if receipt:
        # The complete receipt stays durable; model and UI responses remain paged.
        visible = {r["row_number"] for r in rows[:20]}
        receipt = {
            **receipt,
            "inserted_rows": [r for r in receipt["inserted_rows"] if r["row_number"] in visible],
            "inserted_rows_paginated": True,
        }
    return safe_json(
        {
            **{k: batch[k] for k in BATCH_FIELDS if k != "receipt_json"},
            "kind": "import_receipt"
            if batch["status"] in ("committed", "no_change")
            else "import_preview",
            "counts": batch["counts_json"],
            "receipt": receipt,
            "rows": rows[:20],
            "has_more": len(rows) > 20,
            "next_cursor": offset + 20 if len(rows) > 20 else None,
            "memory_epoch": epoch,
            "confirmation_expired": bool(
                batch["token_expires_at"] and batch["token_expires_at"] <= datetime.now(UTC)
            ),
        }
    )


def _finish(conn, auth, request_id, body, batch_id, action, execution=None):
    if execution:
        source_id = execution.source_id
    else:
        source = add_source(
            conn,
            auth,
            {
                "action": action,
                "batch_id": batch_id,
                "company_id": body.get("company_id"),
                "preview_hash": body.get("preview_hash"),
            },
            role="user",
            kind="user_action",
            source_seq=next_sequence(conn, auth),
            companies=[body["company_id"]],
        )
        source_id = str(source["source_id"])
    conn.execute("UPDATE users SET memory_epoch=memory_epoch+1 WHERE user_id=%s", (auth.user_id,))
    conn.execute(
        "UPDATE summaries SET status='invalidated' WHERE user_id=%s AND status='active'",
        (auth.user_id,),
    )
    conn.execute(
        "UPDATE invoice_import_batches SET source_refs=source_refs||%s,updated_at=now() WHERE batch_id=%s",
        (jsonb([{"source_id": source_id, "action": action}]), batch_id),
    )
    result = _result(conn, auth, batch_id)
    conn.execute(
        "INSERT INTO memory_change_ops(change_id,user_id,request_id,request_hash,target_type,target_id,action,state,result_ref) VALUES(%s,%s,%s,%s,'invoice_import',%s,%s,'committed',%s)",
        (
            uid(),
            auth.user_id,
            request_id,
            digest(body),
            batch_id,
            action,
            jsonb({"batch_id": batch_id}),
        ),
    )
    assess(conn, source_id, "accepted", [{"batch_id": batch_id, "action": action}])
    advance_progress(conn, auth)
    return result


def _classify(rows, existing):
    groups = defaultdict(list)
    for row in rows:
        row.update(
            row_hash=digest(row["normalized_values"]),
            comparison_revision=None,
            compared_values=None,
        )
        if row["classification"] != "invalid":
            groups[row["normalized_values"]["invoice_id"]].append(row)
    for number, group in groups.items():
        if len({row["row_hash"] for row in group}) > 1:
            for row in group:
                row.update(
                    classification="conflict",
                    reason_code="FILE_KEY_CONFLICT",
                    reason_message="文件内同编号内容不一致，整组不导入。",
                )
            continue
        first, *duplicates = group
        for row in duplicates:
            row.update(
                classification="duplicate_in_file",
                reason_code="FILE_DUPLICATE",
                reason_message="与本文件较早一行完全相同。",
            )
        old = existing.get(number)
        if not old:
            continue
        old = safe_json(old)
        incoming = first["normalized_values"]
        first.update(
            comparison_revision=old["revision"], compared_values={k: old[k] for k in incoming}
        )
        different = any(incoming[k] != old[k] for k in ("invoice_date", "amount", "seller", "item"))
        changed_fields = any(
            not is_blank(incoming[k]) and incoming[k] != old[k] for k in ("purpose", "note")
        )
        if different or changed_fields:
            first.update(
                classification="conflict",
                reason_code="EXISTING_FIELD_CHANGE",
                reason_message="已有记录内容不同；导入不能覆盖，请使用原修改流程。",
            )
        else:
            first.update(
                classification="duplicate_existing",
                reason_code="ALREADY_EXISTS",
                reason_message="已存在，保留系统当前用途和备注。",
            )
    return rows


def _plan_hash(upload, policy, head, rows):
    return digest(
        {
            "file": upload["file_sha256"],
            "company_id": upload["company_id"],
            "parser": PARSER_VERSION,
            "policy": policy,
            "head": {k: head[k] for k in ("data_revision", "rules_epoch")},
            "rows": rows,
        }
    )


class InvoiceImports:
    def __init__(self, db, ingest_db=None):
        self.db, self.ingest_db = db, ingest_db

    def available(self):
        if not self.ingest_db:
            return False
        a, b = conninfo_to_dict(self.db.dsn), conninfo_to_dict(self.ingest_db.dsn)
        return all(a.get(k) == b.get(k) for k in ("host", "port", "dbname"))

    def upload(self, auth, *, company_id, session_id, client_request_id, filename, content):
        with self.db.transaction(readonly=True) as conn:
            assert_import_access(conn, auth, company_id)
            _session(conn, auth, session_id, company_id)
        if (
            not filename
            or not filename.lower().endswith(".xlsx")
            or len(filename) > 255
            or any(ord(c) < 32 for c in filename)
        ):
            raise AppError("WORKBOOK_UNSUPPORTED", "只接受文件名不超过 255 字符的 XLSX 文件。", 422)
        validate_archive(content)
        sha = file_hash(content)
        fingerprint = digest([company_id, session_id, filename, sha])
        with self.db.transaction() as conn:
            _lock(conn, auth, self.db)
            assert_import_access(conn, auth, company_id)
            _session(conn, auth, session_id, company_id)
            old = conn.execute(
                "SELECT upload_id,request_hash FROM file_uploads WHERE user_id=%s AND client_request_id=%s",
                (auth.user_id, client_request_id),
            ).fetchone()
            if old:
                if old["request_hash"] != fingerprint:
                    raise AppError("IDEMPOTENCY_CONFLICT", "上传请求编号的文件或范围不同。", 409)
                return safe_json(upload_metadata(conn, auth, str(old["upload_id"]), company_id))
            quota = conn.execute(
                "SELECT count(*) FILTER(WHERE created_at>now()-interval '1 hour') AS recent,coalesce(sum(byte_size),0) AS bytes FROM file_uploads WHERE user_id=%s",
                (auth.user_id,),
            ).fetchone()
            if quota["recent"] >= 20 or quota["bytes"] + len(content) > 100 * 1024 * 1024:
                raise AppError(
                    "UPLOAD_TOO_LARGE", "上传频率或文件存储配额已到上限，请稍后或联系维护者。", 413
                )
            upload_id = uid()
            source = add_source(
                conn,
                auth,
                {
                    "action": "upload_invoice_file",
                    "upload_id": upload_id,
                    "filename": filename,
                    "sha256": sha,
                    "byte_size": len(content),
                },
                role="user",
                kind="file_upload",
                source_seq=next_sequence(conn, auth),
                companies=[company_id],
            )
            conn.execute(
                "INSERT INTO file_uploads(upload_id,source_id,user_id,company_id,session_id,client_request_id,request_hash,original_name,file_sha256,byte_size,content) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    upload_id,
                    source["source_id"],
                    auth.user_id,
                    company_id,
                    session_id,
                    client_request_id,
                    fingerprint,
                    filename,
                    sha,
                    len(content),
                    content,
                ),
            )
            assess(conn, str(source["source_id"]), "no_change")
            advance_progress(conn, auth)
            return safe_json(upload_metadata(conn, auth, upload_id))

    def create(self, auth, *, upload_id, session_id, client_request_id, execution=None):
        with self.db.transaction() as conn:
            _lock(conn, auth, self.db, execution)
            upload = upload_metadata(conn, auth, upload_id)
            company = upload["company_id"]
            assert_import_access(conn, auth, company)
            _session(conn, auth, session_id, company)
            body = {
                "action": "create",
                "upload_id": upload_id,
                "session_id": session_id,
                "company_id": company,
            }
            old = _replay(conn, auth, client_request_id, body)
            if old:
                return _result(conn, auth, old["batch_id"])
            active = conn.execute(
                "SELECT count(*) AS n FROM invoice_import_batches WHERE user_id=%s AND status IN ('draft','validation_failed','preview_ready')",
                (auth.user_id,),
            ).fetchone()["n"]
            if active >= 20:
                raise AppError(
                    "IMPORT_STATE_CONFLICT", "未完成导入达到上限，请先取消或完成已有批次。", 409
                )
            batch_id = uid()
            conn.execute(
                "INSERT INTO invoice_import_batches(batch_id,upload_id,user_id,company_id,source_refs) VALUES(%s,%s,%s,%s,%s)",
                (
                    batch_id,
                    upload_id,
                    auth.user_id,
                    company,
                    jsonb([{"source_id": str(upload["source_id"]), "action": "upload"}]),
                ),
            )
            return _finish(conn, auth, client_request_id, body, batch_id, "create", execution)

    def get(self, auth, batch_id, offset=0):
        if offset < 0:
            raise AppError("INVALID_ARGUMENT", "分页位置无效。")
        with self.db.transaction(readonly=True) as conn:
            return _result(conn, auth, batch_id, offset)

    def list(self, auth, company_id=None, offset=0):
        if offset < 0:
            raise AppError("INVALID_ARGUMENT", "分页位置无效。")
        with self.db.transaction(readonly=True) as conn:
            if company_id:
                assert_access(conn, auth, company_id)
            rows = conn.execute(
                "SELECT b.batch_id,b.company_id,b.status,b.revision,b.counts_json,b.created_at,u.original_name FROM invoice_import_batches b JOIN file_uploads u USING(upload_id) JOIN user_company_grants g ON g.user_id=b.user_id AND g.company_id=b.company_id WHERE b.user_id=%s AND g.can_read AND (%s::text IS NULL OR b.company_id=%s) ORDER BY b.created_at DESC,b.batch_id DESC LIMIT 21 OFFSET %s",
                (auth.user_id, company_id, company_id, offset),
            ).fetchall()
            return safe_json(
                {
                    "batches": rows[:20],
                    "has_more": len(rows) > 20,
                    "next_cursor": offset + 20 if len(rows) > 20 else None,
                }
            )

    def preview(
        self,
        auth,
        batch_id,
        *,
        session_id,
        expected_revision,
        client_request_id,
        policy="strict",
        execution=None,
        issue_token=True,
    ):
        if policy not in ("strict", "skip_issues") or (execution and policy != "strict"):
            raise AppError("INVALID_ARGUMENT", "预览策略无效。")
        with self.db.transaction(readonly=True) as conn:
            batch = _batch(conn, auth, batch_id)
            assert_import_access(conn, auth, batch["company_id"])
            _session(conn, auth, session_id, batch["company_id"])
            upload = upload_metadata(conn, auth, str(batch["upload_id"]))
            content = bytes(
                conn.execute(
                    "SELECT content FROM file_uploads WHERE upload_id=%s", (batch["upload_id"],)
                ).fetchone()["content"]
            )
        if file_hash(content) != upload["file_sha256"]:
            raise AppError("STATE_SOURCE_UNAVAILABLE", "文件校验失败，不能预览。", 503)
        fatal = None
        try:
            rows = parse_invoice_workbook(content, batch["company_id"])
        except AppError as error:
            fatal = error.as_dict()
            rows = []
        with self.db.transaction(readonly=True) as conn:
            head = conn.execute(
                "SELECT * FROM company_heads WHERE company_id=%s", (batch["company_id"],)
            ).fetchone()
            numbers = [
                row["normalized_values"]["invoice_id"]
                for row in rows
                if row["classification"] != "invalid"
            ]
            existing = conn.execute(
                "SELECT * FROM invoices WHERE company_id=%s AND invoice_id=ANY(%s)",
                (batch["company_id"], numbers),
            ).fetchall()
            rows = _classify(rows, {r["invoice_id"]: r for r in existing})
        counts = {
            key: 0
            for key in ("new", "duplicate_existing", "duplicate_in_file", "conflict", "invalid")
        }
        counts.update(Counter(row["classification"] for row in rows))
        counts["total"] = len(rows)
        from decimal import Decimal

        counts["new_amount"] = format(
            sum(
                (
                    Decimal(row["normalized_values"]["amount"])
                    for row in rows
                    if row["classification"] == "new"
                ),
                Decimal(0),
            ),
            ".2f",
        )
        if fatal:
            counts["error"] = fatal
        token = secrets.token_urlsafe(32)
        plan = _plan_hash(upload, policy, head, rows)
        body = {
            "action": "preview",
            "batch_id": batch_id,
            "company_id": batch["company_id"],
            "session_id": session_id,
            "expected_revision": expected_revision,
            "policy": policy,
        }
        with self.db.transaction() as conn:
            _lock(conn, auth, self.db, execution)
            assert_import_access(conn, auth, batch["company_id"])
            old = _replay(conn, auth, client_request_id, body)
            if old:
                return _result(conn, auth, batch_id)
            current_head = conn.execute(
                "SELECT * FROM company_heads WHERE company_id=%s FOR SHARE", (batch["company_id"],)
            ).fetchone()
            current = _batch(conn, auth, batch_id, lock=True)
            if current["status"] in TERMINAL:
                raise AppError("IMPORT_STATE_CONFLICT", "批次已经结束，请查看回执。", 409)
            if current["revision"] != expected_revision:
                raise AppError("IMPORT_PREVIEW_STALE", "批次预览已更新，请重新打开。", 409)
            if any(head[k] != current_head[k] for k in ("data_revision", "rules_epoch")):
                raise AppError("IMPORT_PREVIEW_STALE", "预览期间资料发生变化，请重新预览。", 409)
            allowed = not fatal and (
                policy == "skip_issues" or not (counts["invalid"] or counts["conflict"])
            )
            conn.execute("DELETE FROM invoice_import_rows WHERE batch_id=%s", (batch_id,))
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO invoice_import_rows(batch_id,row_number,normalized_values,row_hash,classification,reason_code,reason_message,comparison_revision,compared_values) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [
                        (
                            batch_id,
                            r["row_number"],
                            jsonb(r["normalized_values"]),
                            r["row_hash"],
                            r["classification"],
                            r["reason_code"],
                            r["reason_message"],
                            r["comparison_revision"],
                            jsonb(r["compared_values"]) if r["compared_values"] else None,
                        )
                        for r in rows
                    ],
                )
            conn.execute(
                "UPDATE invoice_import_batches SET revision=revision+1,status=%s,policy=%s,preview_session_id=%s,preview_data_revision=%s,preview_rules_epoch=%s,preview_hash=%s,confirmation_token_hash=%s,token_expires_at=%s,counts_json=%s WHERE batch_id=%s",
                (
                    "preview_ready" if allowed else "validation_failed",
                    policy,
                    session_id,
                    head["data_revision"],
                    head["rules_epoch"],
                    plan,
                    hashlib.sha256(token.encode()).hexdigest() if allowed else None,
                    datetime.now(UTC) + timedelta(minutes=15) if allowed else None,
                    jsonb(counts),
                    batch_id,
                ),
            )
            result = _finish(conn, auth, client_request_id, body, batch_id, "preview", execution)
            if allowed and issue_token:
                result["confirmation_token"] = token
            return result

    def prepare(self, execution, upload_id, company_id):
        with self.db.transaction(readonly=True) as conn:
            turn = owned(conn, "turns", "turn_id", execution.turn_id, execution.auth)
            if upload_id not in [str(x) for x in turn["upload_ids"]]:
                raise AppError("IMPORT_ACCESS_DENIED", "该文件没有附在本轮请求中。", 403)
            upload_metadata(conn, execution.auth, upload_id, company_id)
        batch = self.create(
            execution.auth,
            upload_id=upload_id,
            session_id=execution.session_id,
            client_request_id=f"prepare-create:{execution.source_id}:{upload_id}",
            execution=execution,
        )
        execution.expected_epoch = batch["memory_epoch"]
        if batch["status"] in TERMINAL:
            return batch
        return self.preview(
            execution.auth,
            batch["batch_id"],
            session_id=execution.session_id,
            expected_revision=batch["revision"],
            client_request_id=f"prepare-preview:{execution.source_id}:{upload_id}",
            execution=execution,
            issue_token=False,
        )

    def cancel(self, auth, batch_id, *, expected_revision, client_request_id):
        with self.db.transaction() as conn:
            _lock(conn, auth, self.db)
            before = _batch(conn, auth, batch_id)
            conn.execute(
                "SELECT * FROM company_heads WHERE company_id=%s FOR SHARE", (before["company_id"],)
            )
            batch = _batch(conn, auth, batch_id, lock=True)
            body = {
                "action": "cancel",
                "batch_id": batch_id,
                "company_id": batch["company_id"],
                "expected_revision": expected_revision,
            }
            old = _replay(conn, auth, client_request_id, body)
            if old:
                return _result(conn, auth, batch_id)
            if batch["status"] in TERMINAL or batch["revision"] != expected_revision:
                raise AppError("IMPORT_STATE_CONFLICT", "批次已结束或版本变化，不能取消。", 409)
            conn.execute(
                "UPDATE invoice_import_batches SET status='cancelled',revision=revision+1,confirmation_token_hash=NULL WHERE batch_id=%s",
                (batch_id,),
            )
            return _finish(conn, auth, client_request_id, body, batch_id, "cancel")

    def confirm(
        self,
        auth,
        batch_id,
        *,
        session_id,
        expected_revision,
        preview_hash,
        confirmation_token,
        client_request_id,
    ):
        if not self.available():
            raise AppError("IMPORT_UNAVAILABLE", "专用入库连接未配置或不属于此实例。", 503)
        with self.db.transaction(readonly=True) as conn:
            instance = conn.execute(
                "SELECT instance_id FROM dataset_metadata WHERE singleton_id=1"
            ).fetchone()["instance_id"]
        body = {
            "action": "confirm",
            "batch_id": batch_id,
            "session_id": session_id,
            "expected_revision": expected_revision,
            "preview_hash": preview_hash,
            "token_hash": hashlib.sha256(confirmation_token.encode()).hexdigest(),
        }
        try:
            with self.ingest_db.transaction() as conn:
                if (
                    conn.execute(
                        "SELECT instance_id FROM dataset_metadata WHERE singleton_id=1"
                    ).fetchone()["instance_id"]
                    != instance
                ):
                    raise AppError("IMPORT_UNAVAILABLE", "专用入库连接实例不同。", 503)
                _lock(conn, auth, self.ingest_db)
                before = _batch(conn, auth, batch_id)
                body["company_id"] = before["company_id"]
                head = conn.execute(
                    "SELECT * FROM company_heads WHERE company_id=%s FOR UPDATE",
                    (before["company_id"],),
                ).fetchone()
                batch = _batch(conn, auth, batch_id, lock=True)
                _replay(conn, auth, client_request_id, body)
                if batch["status"] in ("committed", "no_change"):
                    return _result(conn, auth, batch_id)
                assert_import_access(conn, auth, batch["company_id"])
                _session(conn, auth, session_id, batch["company_id"])
                if (
                    batch["status"] != "preview_ready"
                    or batch["revision"] != expected_revision
                    or str(batch["preview_session_id"]) != session_id
                    or batch["preview_hash"] != preview_hash
                ):
                    raise AppError(
                        "IMPORT_PREVIEW_STALE", "确认与当前预览不一致，请重新预览。", 409
                    )
                if not secrets.compare_digest(
                    body["token_hash"], batch["confirmation_token_hash"] or ""
                ):
                    raise AppError("IMPORT_STATE_CONFLICT", "确认凭据无效，请重新预览。", 409)
                if not batch["token_expires_at"] or batch["token_expires_at"] <= datetime.now(UTC):
                    raise AppError(
                        "IMPORT_CONFIRMATION_EXPIRED", "预览已过期，请重新预览再确认。", 409
                    )
                if (
                    head["data_revision"] != batch["preview_data_revision"]
                    or head["rules_epoch"] != batch["preview_rules_epoch"]
                ):
                    raise AppError("IMPORT_PREVIEW_STALE", "资料或规则已变更，请重新预览。", 409)
                upload = upload_metadata(conn, auth, str(batch["upload_id"]))
                content = bytes(
                    conn.execute(
                        "SELECT content FROM file_uploads WHERE upload_id=%s", (batch["upload_id"],)
                    ).fetchone()["content"]
                )
                rows = conn.execute(
                    "SELECT row_number,normalized_values,row_hash,classification,reason_code,reason_message,comparison_revision,compared_values FROM invoice_import_rows WHERE batch_id=%s ORDER BY row_number",
                    (batch_id,),
                ).fetchall()
                if (
                    batch["parser_version"] != PARSER_VERSION
                    or file_hash(content) != upload["file_sha256"]
                    or any(digest(r["normalized_values"]) != r["row_hash"] for r in rows)
                    or _plan_hash(upload, batch["policy"], head, rows) != preview_hash
                ):
                    raise AppError(
                        "STATE_SOURCE_UNAVAILABLE", "持久计划或来源校验失败，未执行追加。", 503
                    )
                new = [r for r in rows if r["classification"] == "new"]
                numbers = [r["normalized_values"]["invoice_id"] for r in new]
                if conn.execute(
                    "SELECT 1 FROM invoices WHERE company_id=%s AND invoice_id=ANY(%s) LIMIT 1",
                    (batch["company_id"], numbers),
                ).fetchone():
                    raise AppError("IMPORT_PREVIEW_STALE", "待新增编号已存在，请重新预览。", 409)
                with conn.cursor() as cursor:
                    cursor.executemany(
                        "INSERT INTO invoices(company_id,invoice_id,invoice_date,seller,item,amount,purpose,note) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                        [
                            tuple(
                                r["normalized_values"][k]
                                for k in (
                                    "company_id",
                                    "invoice_id",
                                    "invoice_date",
                                    "seller",
                                    "item",
                                    "amount",
                                    "purpose",
                                    "note",
                                )
                            )
                            for r in new
                        ],
                    )
                if new:
                    conn.execute(
                        "UPDATE company_heads SET data_revision=data_revision+1 WHERE company_id=%s",
                        (batch["company_id"],),
                    )
                receipt = {
                    "batch_id": batch_id,
                    "company_id": batch["company_id"],
                    "inserted_count": len(new),
                    "skipped_count": len(rows) - len(new),
                    "counts": batch["counts_json"],
                    "inserted_rows": [
                        {
                            "row_number": r["row_number"],
                            "invoice_id": r["normalized_values"]["invoice_id"],
                            "row_hash": r["row_hash"],
                        }
                        for r in new
                    ],
                    "before_data_revision": head["data_revision"],
                    "after_data_revision": head["data_revision"] + bool(new),
                    "committed_at": datetime.now(UTC).isoformat(),
                    "verification_state": "committed",
                }
                conn.execute(
                    "UPDATE invoice_import_batches SET status=%s,revision=revision+1,receipt_json=%s,committed_at=now(),confirmation_token_hash=NULL WHERE batch_id=%s",
                    ("committed" if new else "no_change", jsonb(receipt), batch_id),
                )
                return _finish(conn, auth, client_request_id, body, batch_id, "confirm")
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            raise AppError(
                "IMPORT_STATUS_UNKNOWN",
                "提交结果暂时无法确认，请按原批次核对，不要重新上传。",
                503,
                True,
                {"batch_id": batch_id},
            ) from exc
