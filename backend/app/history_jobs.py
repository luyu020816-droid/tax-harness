"""Rebuildable history passages; source writes never call an inference service."""

import asyncio
import json
from contextlib import suppress

from app.errors import AppError
from app.identity import Identity
from app.retrieval_engine import terms, vector_literal
from app.sources import read_source
from app.values import digest, jsonb, uid

CHUNKER = "history-char600-overlap60-token1200-v1"


def passages(text, count):
    start, result = 0, []
    while start < len(text):
        end = min(start + 600, len(text))
        while count(text[start:end]) > 1200:
            end = start + (end - start) // 2
            if end <= start:
                raise AppError("DOCUMENT_SEGMENT_TOO_LARGE", "历史原文无法在索引输入边界内分块。")
        result.append((start, end, text[start:end]))
        if end == len(text):
            break
        start = max(start + 1, end - min(60, (end - start) // 4))
    return result


def is_retrieval_output(source):
    if source["role"] != "tool":
        return False
    try:
        data = json.loads(source["content"]).get("data") or {}
        return isinstance(data, dict) and data.get("kind") in (
            "knowledge_search",
            "knowledge_evidence",
            "history_search",
            "history_evidence",
        )
    except (ValueError, AttributeError):
        return False


class HistoryJobs:
    def __init__(self, db, settings, engine):
        self.db, self.settings, self.engine = db, settings, engine
        self.task, self.current = None, None

    def claim(self):
        from app.recovery import in_maintenance

        if in_maintenance(self.settings.database_url):
            return None
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE history_jobs SET state='running',attempts=attempts+1,fencing_token=fencing_token+1,"
                "lease_until=clock_timestamp()+(%s*interval '1 second') WHERE source_id=("
                "SELECT source_id FROM history_jobs WHERE state='queued' OR (state='running' AND lease_until<clock_timestamp()) "
                "ORDER BY created_at,source_id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *",
                (self.settings.knowledge_lease_seconds,),
            ).fetchone()

    def _guard(self, conn, job):
        if not conn.execute(
            "SELECT source_id FROM history_jobs WHERE source_id=%s AND fencing_token=%s AND state='running' "
            "AND lease_until>clock_timestamp() FOR UPDATE",
            (job["source_id"], job["fencing_token"]),
        ).fetchone():
            raise AppError("SOURCE_INVALIDATED", "历史索引租约已失效。", 409)

    def _finish(self, job, state, *, error=None, profile=None):
        with self.db.transaction() as conn:
            self._guard(conn, job)
            conn.execute(
                "UPDATE history_jobs SET state=%s,error=%s,profile_id=%s,lease_until=NULL WHERE source_id=%s",
                (state, jsonb(error) if error else None, profile, job["source_id"]),
            )

    def run(self, job):
        try:
            with self.db.transaction(readonly=True) as conn:
                source = conn.execute(
                    "SELECT * FROM sources WHERE source_id=%s", (job["source_id"],)
                ).fetchone()
                if (
                    source["recall_state"] != "active"
                    or source["revision"] != job["source_revision"]
                ):
                    raise AppError("SOURCE_INVALIDATED", "历史来源已更正或抑制。", 409)
                if is_retrieval_output(source):
                    self._finish(job, "skipped")
                    return
                source = read_source(conn, Identity(source["user_id"]), str(source["source_id"]))
                vector_ready = conn.execute(
                    "SELECT to_regclass('public.history_embeddings') AS t"
                ).fetchone()["t"]
            if not vector_ready:
                raise AppError("DEPENDENCY_UNAVAILABLE", "历史向量扩展未安装，原文仍保留。", 503)
            profile, manifest = self.engine.profile()
            chunks = passages(source["content"], self.engine.count)
            vectors = []
            for start in range(0, len(chunks), 4):
                vectors.extend(self.engine.embed([c[2] for c in chunks[start : start + 4]]))
            if len(vectors) != len(chunks):
                raise AppError("DEPENDENCY_UNAVAILABLE", "历史向量未完整返回。", 503)
            literals = [vector_literal(v) for v in vectors]
            with self.db.transaction() as conn:
                conn.execute(
                    "SELECT user_id FROM users WHERE user_id=%s FOR SHARE", (source["user_id"],)
                )
                self._guard(conn, job)
                current = read_source(conn, Identity(source["user_id"]), str(source["source_id"]))
                if (
                    current["revision"] != job["source_revision"]
                    or current["content_hash"] != source["content_hash"]
                ):
                    raise AppError("SOURCE_INVALIDATED", "推理期间历史来源已变化。", 409)
                conn.execute(
                    "INSERT INTO retrieval_profiles(profile_id,manifest,dimension,status) VALUES(%s,%s,1024,'ready') ON CONFLICT DO NOTHING",
                    (profile, jsonb(manifest)),
                )
                conn.execute(
                    "DELETE FROM history_passages WHERE source_id=%s AND profile_id=%s",
                    (source["source_id"], profile),
                )
                for ordinal, ((begin, end, text), vector) in enumerate(
                    zip(chunks, literals, strict=True)
                ):
                    passage = uid()
                    conn.execute(
                        "INSERT INTO history_passages(passage_id,source_id,source_revision,profile_id,chunker,ordinal,char_start,char_end,excerpt,excerpt_hash,lexical_index) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,to_tsvector('simple',%s))",
                        (
                            passage,
                            source["source_id"],
                            source["revision"],
                            profile,
                            CHUNKER,
                            ordinal,
                            begin,
                            end,
                            text,
                            digest(text),
                            " ".join(terms(text)),
                        ),
                    )
                    conn.execute(
                        "INSERT INTO history_embeddings(passage_id,embedding) VALUES(%s,%s::vector)",
                        (passage, vector),
                    )
                conn.execute(
                    "UPDATE history_jobs SET state='done',profile_id=%s,error=NULL,lease_until=NULL WHERE source_id=%s",
                    (profile, source["source_id"]),
                )
        except (AppError, ImportError, OSError, RuntimeError, ValueError) as exc:
            error = (
                exc
                if isinstance(exc, AppError)
                else AppError("DEPENDENCY_UNAVAILABLE", "历史推理依赖不可用，原文仍保留。", 503)
            )
            with suppress(AppError):
                self._finish(
                    job,
                    "cancelled" if error.code == "SOURCE_INVALIDATED" else "blocked",
                    error=error.as_dict(),
                )

    def backfill(self):
        profile, _ = self.engine.profile()
        with self.db.transaction() as conn:
            rows = conn.execute(
                "INSERT INTO history_jobs(source_id,source_revision) SELECT source_id,revision FROM sources WHERE recall_state='active' "
                "ON CONFLICT(source_id) DO UPDATE SET source_revision=excluded.source_revision,state='queued',fencing_token=history_jobs.fencing_token+1,lease_until=NULL,error=NULL "
                "WHERE history_jobs.state IN ('blocked','cancelled') OR history_jobs.source_revision<>excluded.source_revision "
                "OR (history_jobs.state='done' AND history_jobs.profile_id IS DISTINCT FROM %s) RETURNING source_id",
                (profile,),
            ).fetchall()
        return {"queued": len(rows), "profile_id": profile}

    async def start(self):
        if self.settings.history_enabled:
            self.task = asyncio.create_task(self._worker())

    async def stop(self):
        if self.current:
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE history_jobs SET state='queued',lease_until=NULL,fencing_token=fencing_token+1 WHERE source_id=%s AND fencing_token=%s AND state='running'",
                    (self.current["source_id"], self.current["fencing_token"]),
                )
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _heartbeat(self, job):
        while True:
            await asyncio.sleep(self.settings.knowledge_lease_seconds / 3)
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE history_jobs SET lease_until=clock_timestamp()+(%s*interval '1 second') WHERE source_id=%s AND fencing_token=%s AND state='running' AND lease_until>clock_timestamp()",
                    (self.settings.knowledge_lease_seconds, job["source_id"], job["fencing_token"]),
                )

    async def _worker(self):
        while True:
            try:
                job = await asyncio.to_thread(self.claim)
                if not job:
                    await asyncio.sleep(1)
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
                await asyncio.sleep(1)
