"""Leased indexing jobs. Parsing/inference never hold document or user locks."""

import asyncio
import hashlib
from contextlib import nullcontext, suppress

from app.errors import AppError
from app.identity import Identity
from app.knowledge import _in_use, document_in, publish_in, upload_access
from app.knowledge_parser import (
    CHUNKER_VERSION,
    chunk_blocks,
    inspect_file,
    parse_isolated,
    text_hash,
)
from app.retrieval_engine import terms, vector_literal
from app.values import digest, jsonb, uid


class KnowledgeJobs:
    def __init__(self, db, settings, engine):
        self.db, self.settings, self.engine = db, settings, engine
        self.task, self.current = None, None

    async def start(self):
        if self.settings.knowledge_enabled:
            self.task = asyncio.create_task(self._worker())

    async def stop(self):
        if self.current:
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE retrieval_jobs SET state='queued',lease_until=NULL,fencing_token=fencing_token+1 "
                    "WHERE job_id=%s AND fencing_token=%s AND state='running'",
                    (self.current["job_id"], self.current["fencing_token"]),
                )
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def claim(self):
        from app.recovery import in_maintenance

        if in_maintenance(self.settings.database_url):
            return None
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE retrieval_jobs SET state='running',stage='parsing',attempts=attempts+1,"
                "lease_until=now()+(%s * interval '1 second'),fencing_token=fencing_token+1 WHERE job_id=("
                "SELECT job_id FROM retrieval_jobs WHERE state='queued' OR (state='running' AND lease_until<clock_timestamp()) "
                "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *",
                (self.settings.knowledge_lease_seconds,),
            ).fetchone()

    def _guard(self, conn, job):
        row = conn.execute(
            "SELECT * FROM retrieval_jobs WHERE job_id=%s AND fencing_token=%s AND state='running' "
            "AND lease_until>clock_timestamp() FOR UPDATE",
            (job["job_id"], job["fencing_token"]),
        ).fetchone()
        if not row:
            raise AppError("SOURCE_INVALIDATED", "索引任务已取消或由新执行接管。", 409)
        return row

    def stage(self, job, stage, progress=None):
        with self.db.transaction() as conn:
            self._guard(conn, job)
            conn.execute(
                "UPDATE retrieval_jobs SET stage=%s,progress=%s WHERE job_id=%s",
                (stage, jsonb(progress or {}), job["job_id"]),
            )
            conn.execute(
                "UPDATE knowledge_versions SET state=%s WHERE version_id=%s",
                (stage, job["version_id"]),
            )

    def renew(self, job):
        with self.db.transaction() as conn:
            row = conn.execute(
                "UPDATE retrieval_jobs SET lease_until=now()+(%s * interval '1 second') "
                "WHERE job_id=%s AND fencing_token=%s AND state='running' AND lease_until>clock_timestamp() RETURNING job_id",
                (self.settings.knowledge_lease_seconds, job["job_id"], job["fencing_token"]),
            ).fetchone()
            return bool(row)

    async def _heartbeat(self, job):
        while True:
            await asyncio.sleep(self.settings.knowledge_lease_seconds / 3)
            if not await asyncio.to_thread(self.renew, job):
                return

    async def _worker(self):
        while True:
            try:
                job = await asyncio.to_thread(self.claim)
                if not job:
                    await asyncio.sleep(0.5)
                    continue
                self.current = job
                heartbeat = asyncio.create_task(self._heartbeat(job))
                try:
                    await asyncio.to_thread(self.run, job)
                finally:
                    heartbeat.cancel()
                    with suppress(asyncio.CancelledError):
                        await heartbeat
                    self.current = None
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient database outage leaves the persistent lease for recovery.
                await asyncio.sleep(1)

    def run(self, job):
        auth = Identity(job["user_id"])
        try:
            with self.db.transaction(readonly=True) as conn:
                version = conn.execute(
                    "SELECT v.*,f.file_bytes,f.sha256,f.original_name FROM knowledge_versions v "
                    "JOIN knowledge_files f USING(file_id) WHERE v.version_id=%s",
                    (job["version_id"],),
                ).fetchone()
                document = document_in(conn, auth, str(version["document_id"]))
                upload_access(conn, auth, document["company_id"])
                vector_ready = conn.execute(
                    "SELECT to_regclass('public.retrieval_embeddings') AS t"
                ).fetchone()["t"]
            if not vector_ready:
                raise AppError(
                    "DEPENDENCY_UNAVAILABLE", "pgvector尚未安装，保留原文件并等待索引重试。", 503
                )
            content = bytes(version["file_bytes"])
            if hashlib.sha256(content).hexdigest() != version["sha256"]:
                raise AppError("SOURCE_INVALIDATED", "原文件校验失败，未发布。", 503)
            suffix, _ = inspect_file(version["original_name"], content)
            self.stage(job, "parsing")
            from app.ocr_status import options

            ocr = options(self.settings) if suffix == ".pdf" else None
            work = getattr(self.engine, "external_work", nullcontext) if ocr else nullcontext
            with work():
                parsed = parse_isolated(
                    content,
                    suffix,
                    self.settings.knowledge_ocr_timeout
                    if ocr
                    else self.settings.knowledge_parser_timeout,
                    ocr=ocr,
                )
            self.stage(job, "chunking", {"blocks": len(parsed["blocks"])})
            chunks = chunk_blocks(parsed["blocks"], document["title"], self.engine.count)
            profile_id, manifest = self.engine.profile()
            self.stage(job, "embedding", {"total": len(chunks), "completed": 0})
            vectors = []
            for start in range(0, len(chunks), 4):
                vectors.extend(
                    self.engine.embed([c["retrieval_text"] for c in chunks[start : start + 4]])
                )
                self.stage(job, "embedding", {"total": len(chunks), "completed": len(vectors)})
            if len(vectors) != len(chunks):
                raise AppError("DEPENDENCY_UNAVAILABLE", "向量结果不完整，未发布部分索引。", 503)
            self.stage(job, "validating", {"chunks": len(chunks)})
            self.publish(job, parsed, chunks, vectors, profile_id, manifest)
        except AppError as error:
            try:
                self.fail(job, error)
            except AppError as stale:
                if stale.code != "SOURCE_INVALIDATED":
                    raise
        except Exception as exc:
            self.fail(
                job,
                AppError(
                    "INDEX_BUILD_FAILED",
                    "索引处理失败；原有效版本保持不变。",
                    503,
                    details={"type": type(exc).__name__},
                ),
            )

    def fail(self, job, error):
        blocked = error.code in ("DEPENDENCY_UNAVAILABLE", "OCR_REQUIRED")
        state = "blocked" if blocked else "failed"
        with self.db.transaction() as conn:
            self._guard(conn, job)
            conn.execute(
                "UPDATE retrieval_jobs SET state=%s,stage=%s,error=%s,lease_until=NULL WHERE job_id=%s",
                (state, state, jsonb(error.as_dict()), job["job_id"]),
            )
            conn.execute(
                "UPDATE knowledge_versions SET state=%s,error=%s WHERE version_id=%s",
                (state, jsonb(error.as_dict()), job["version_id"]),
            )

    def publish(self, job, parsed, chunks, vectors, profile_id, manifest):
        auth = Identity(job["user_id"])
        with self.db.transaction() as conn:
            conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (auth.user_id,))
            version = conn.execute(
                "SELECT * FROM knowledge_versions WHERE version_id=%s", (job["version_id"],)
            ).fetchone()
            conn.execute(
                "SELECT 1 FROM company_heads WHERE company_id=%s FOR UPDATE",
                (version["company_id"],),
            )
            document = document_in(conn, auth, str(version["document_id"]), lock=True)
            upload_access(conn, auth, document["company_id"])
            self._guard(conn, job)
            conn.execute(
                "INSERT INTO retrieval_profiles(profile_id,manifest,dimension,status) VALUES(%s,%s,1024,'ready') ON CONFLICT DO NOTHING",
                (profile_id, jsonb(manifest)),
            )
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO knowledge_blocks(block_id,version_id,ordinal,kind,heading_path,content,content_hash,locator) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    [
                        (
                            uid(),
                            version["version_id"],
                            b["ordinal"],
                            b["kind"],
                            b["heading_path"],
                            b["content"],
                            b["content_hash"],
                            jsonb(b["locator"]),
                        )
                        for b in parsed["blocks"]
                    ],
                )
                chunk_rows, embeddings = [], []
                for chunk, vector in zip(chunks, vectors, strict=True):
                    chunk_id = uid()
                    chunk_rows.append(
                        (
                            chunk_id,
                            version["version_id"],
                            chunk["ordinal"],
                            chunk["block_ordinals"],
                            chunk["content"],
                            chunk["retrieval_text"],
                            chunk["content_hash"],
                            chunk["token_count"],
                            " ".join(terms(chunk["retrieval_text"])),
                            jsonb(chunk["locator"]),
                        )
                    )
                    embeddings.append(
                        (
                            profile_id,
                            chunk_id,
                            text_hash(chunk["retrieval_text"]),
                            vector_literal(vector),
                        )
                    )
                cursor.executemany(
                    "INSERT INTO knowledge_chunks(chunk_id,version_id,ordinal,block_ordinals,content,retrieval_text,content_hash,token_count,lexical_index,locator) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s)",
                    chunk_rows,
                )
                cursor.executemany(
                    "INSERT INTO retrieval_embeddings(profile_id,target_id,input_hash,embedding) VALUES(%s,%s,%s,%s::vector)",
                    embeddings,
                )
            version = conn.execute(
                "UPDATE knowledge_versions SET state='ready',error=NULL,parser_version=%s,chunker_version=%s,profile_id=%s,content_hash=%s,block_count=%s,chunk_count=%s "
                "WHERE version_id=%s RETURNING *",
                (
                    parsed["parser_version"],
                    CHUNKER_VERSION,
                    profile_id,
                    digest([b["content_hash"] for b in parsed["blocks"]]),
                    len(parsed["blocks"]),
                    len(chunks),
                    version["version_id"],
                ),
            ).fetchone()
            published = (
                document["scope"] == "user_private"
                and document["status"] == "active"
                and document["revision"] == version["expected_document_revision"]
                and not _in_use(conn, document["document_id"])
            )
            if published:
                publish_in(conn, document, version, version["expected_document_revision"])
            conn.execute(
                "UPDATE retrieval_jobs SET state='done',stage='ready',lease_until=NULL,error=NULL,progress=%s WHERE job_id=%s",
                (jsonb({"chunks": len(chunks), "published": bool(published)}), job["job_id"]),
            )
