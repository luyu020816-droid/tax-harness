"""Bounded chat indexing with leases, atomic publication and missing-job repair."""

import asyncio
from contextlib import suppress

import psycopg

from app import conversation_index as index
from app.errors import AppError
from app.resource_access import SESSION_VISIBLE
from app.retrieval_engine import vector_literal
from app.values import digest, jsonb, uid


class ConversationJobs:
    def __init__(self, db, settings, engine):
        self.db, self.settings, self.engine = db, settings, engine
        self.profile_id, self.task, self.current = None, None, None

    def configure(self, *, enabled=True):
        with self.db.transaction() as conn:
            if not index.available(conn):
                raise AppError("DEPENDENCY_UNAVAILABLE", "聊天语义索引迁移尚未安装。", 503)
            self.profile_id = index.register(conn, self.engine, enabled=enabled)
        return self.profile_id

    def backfill(self, limit=100):
        if not self.profile_id:
            self.configure()
        visible = SESSION_VISIBLE.replace("%(user)s", "x.user_id")
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT x.source_id,x.revision,x.content_hash FROM sources x JOIN turns t USING(turn_id) "
                "JOIN sessions s USING(session_id) WHERE "
                + index.ELIGIBLE
                + " AND "
                + visible
                + " AND NOT EXISTS(SELECT 1 FROM conversation_index_jobs j WHERE j.source_id=x.source_id "
                "AND j.profile_id=%s) ORDER BY x.source_id LIMIT %s",
                (self.profile_id, limit),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "INSERT INTO conversation_index_jobs(source_id,profile_id,source_revision,source_hash) "
                    "VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (row["source_id"], self.profile_id, row["revision"], row["content_hash"]),
                )
        return len(rows)

    def retry(self):
        if not self.profile_id:
            self.configure()
        with self.db.transaction() as conn:
            rows = conn.execute(
                "UPDATE conversation_index_jobs SET state='queued',attempts=0,next_retry_at=now(),"
                "fencing_token=fencing_token+1,lease_until=NULL,last_error=NULL WHERE profile_id=%s "
                "AND state IN ('failed','cancelled') RETURNING source_id",
                (self.profile_id,),
            ).fetchall()
        return len(rows)

    def claim(self):
        from app.recovery import in_maintenance

        if in_maintenance(self.settings.database_url):
            return None
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE conversation_index_jobs SET state='failed',lease_until=NULL WHERE state='running' "
                "AND lease_until<clock_timestamp() AND attempts>=3"
            )
            return conn.execute(
                "UPDATE conversation_index_jobs j SET state='running',attempts=attempts+1,"
                "fencing_token=fencing_token+1,target_generation_id=%s,lease_until=clock_timestamp()+%s*interval '1 second',updated_at=now() "
                "WHERE (source_id,profile_id)=(SELECT q.source_id,q.profile_id FROM conversation_index_jobs q "
                "WHERE q.profile_id=%s AND q.attempts<3 AND ((q.state='queued' AND next_retry_at<=now()) "
                "OR (q.state='running' AND lease_until<clock_timestamp())) ORDER BY q.created_at,q.source_id "
                "FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING j.*",
                (uid(), self.settings.knowledge_lease_seconds, self.profile_id),
            ).fetchone()

    def _guard(self, conn, job):
        if not conn.execute(
            "SELECT 1 FROM conversation_index_jobs WHERE source_id=%s AND profile_id=%s AND state='running' "
            "AND fencing_token=%s AND lease_until>clock_timestamp() FOR UPDATE",
            (job["source_id"], job["profile_id"], job["fencing_token"]),
        ).fetchone():
            raise AppError("SOURCE_INVALIDATED", "聊天索引租约已失效。", 409)

    def run(self, job):
        try:
            with self.db.transaction(readonly=True) as conn:
                source = index.source_in(conn, job["source_id"])
            if (source["revision"], source["content_hash"]) != (
                job["source_revision"],
                job["source_hash"],
            ):
                raise AppError("SOURCE_INVALIDATED", "聊天索引原文版本已变化。", 409)
            if index.profile(self.engine)[0] != job["profile_id"]:
                raise AppError("SOURCE_INVALIDATED", "聊天索引模型版本已变化。", 409)
            pieces = index.chunks(source["content"], source["role"], self.engine.count)
            vectors = []
            for offset in range(0, len(pieces), 2):
                texts = [
                    index.embedding_text(source["role"], p[2]) for p in pieces[offset : offset + 2]
                ]
                values = self.engine.embed(texts)
                if len(values) != len(texts):
                    raise AppError("DEPENDENCY_UNAVAILABLE", "聊天向量没有完整返回。", 503)
                vectors.extend(vector_literal(value) for value in values)
            with self.db.transaction() as conn:
                # Same owner/session order as organization changes, then job fence.
                conn.execute("SELECT 1 FROM users WHERE user_id=%s FOR SHARE", (source["user_id"],))
                conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id=%s FOR SHARE", (source["session_id"],)
                )
                conn.execute(
                    "SELECT 1 FROM sources WHERE source_id=%s FOR SHARE", (source["source_id"],)
                )
                conn.execute("SELECT lock_conversation_index_grants(%s)", (source["source_id"],))
                self._guard(conn, job)
                current = index.source_in(conn, job["source_id"])
                if (
                    current["content_hash"] != source["content_hash"]
                    or current["revision"] != source["revision"]
                ):
                    raise AppError("SOURCE_INVALIDATED", "索引发布时原文已变化。", 409)
                for ordinal, ((begin, end, text), vector) in enumerate(
                    zip(pieces, vectors, strict=True)
                ):
                    conn.execute(
                        "INSERT INTO conversation_passages(passage_id,source_id,source_revision,source_hash,profile_id,"
                        "generation_id,ordinal,char_start,char_end,excerpt_hash,embedding_input_hash,embedding) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)",
                        (
                            uid(),
                            source["source_id"],
                            source["revision"],
                            source["content_hash"],
                            job["profile_id"],
                            job["target_generation_id"],
                            ordinal,
                            begin,
                            end,
                            digest(text),
                            digest(index.embedding_text(source["role"], text)),
                            vector,
                        ),
                    )
                conn.execute(
                    "UPDATE conversation_index_jobs SET state='ready',published_generation_id=target_generation_id,"
                    "expected_chunk_count=%s,lease_until=NULL,last_error=NULL,updated_at=now() "
                    "WHERE source_id=%s AND profile_id=%s",
                    (len(pieces), job["source_id"], job["profile_id"]),
                )
        except (AppError, ImportError, OSError, RuntimeError, ValueError, psycopg.Error) as exc:
            error = (
                exc
                if isinstance(exc, AppError)
                else AppError("DEPENDENCY_UNAVAILABLE", "聊天索引构建失败，原文仍保留。", 503)
            )
            cancelled = error.code in (
                "SOURCE_INVALIDATED",
                "RESOURCE_NOT_FOUND",
                "ACCESS_DENIED",
                "STATE_SOURCE_UNAVAILABLE",
            )
            state = "cancelled" if cancelled else "failed" if job["attempts"] >= 3 else "queued"
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE conversation_index_jobs SET state=%s,last_error=%s,lease_until=NULL,"
                    "next_retry_at=now()+%s*interval '1 second',updated_at=now() "
                    "WHERE source_id=%s AND profile_id=%s AND fencing_token=%s AND state='running'",
                    (
                        state,
                        jsonb(error.as_dict()),
                        30 if job["attempts"] == 1 else 120,
                        job["source_id"],
                        job["profile_id"],
                        job["fencing_token"],
                    ),
                )

    async def _heartbeat(self, job):
        while True:
            await asyncio.sleep(self.settings.knowledge_lease_seconds / 3)
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE conversation_index_jobs SET lease_until=clock_timestamp()+%s*interval '1 second' "
                    "WHERE source_id=%s AND profile_id=%s AND fencing_token=%s AND state='running' AND lease_until>clock_timestamp()",
                    (
                        self.settings.knowledge_lease_seconds,
                        job["source_id"],
                        job["profile_id"],
                        job["fencing_token"],
                    ),
                )

    async def start(self):
        with self.db.transaction() as conn:
            if index.available(conn):
                conn.execute("UPDATE conversation_index_profiles SET enabled=false WHERE enabled")
        if self.settings.conversation_semantic_mode != "off":
            self.task = asyncio.create_task(self._worker())

    async def stop(self):
        if self.current:
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE conversation_index_jobs SET state='queued',lease_until=NULL,fencing_token=fencing_token+1 "
                    "WHERE source_id=%s AND profile_id=%s AND fencing_token=%s AND state='running'",
                    (
                        self.current["source_id"],
                        self.current["profile_id"],
                        self.current["fencing_token"],
                    ),
                )
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _worker(self):
        while True:
            try:
                if not self.profile_id:
                    await asyncio.to_thread(self.configure)
                await asyncio.to_thread(self.backfill)
                job = await asyncio.to_thread(self.claim)
                if not job:
                    await asyncio.sleep(2)
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
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            except (AppError, ImportError, OSError, RuntimeError, psycopg.Error):
                await asyncio.sleep(30)
