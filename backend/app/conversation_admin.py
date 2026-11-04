"""Explicit chat index administration; no identity or knowledge policy changes."""

import asyncio

from app.conversation_jobs import ConversationJobs
from app.db import Database
from app.retrieval_engine import LocalRetrieval


def command(args, settings):
    db = Database(settings.database_url)
    db.open()
    engine = LocalRetrieval(settings.knowledge_model_directory)
    try:
        if args.index_action == "status":
            from app.conversation_index import available

            with db.transaction(readonly=True) as conn:
                if not available(conn):
                    return {"schema_available": False}
                return {
                    "schema_available": True,
                    "jobs": conn.execute(
                        "SELECT profile_id,state,count(*) count FROM conversation_index_jobs GROUP BY profile_id,state ORDER BY profile_id,state"
                    ).fetchall(),
                }
        jobs = ConversationJobs(db, settings, engine)
        jobs.configure(enabled=settings.conversation_semantic_mode != "off")
        queued = 0
        if args.index_action == "retry":
            queued = jobs.retry()
        else:
            while count := jobs.backfill():
                queued += count
        processed = 0
        if args.drain:

            async def drain():
                count = 0
                while job := jobs.claim():
                    await jobs.process(job)
                    count += 1
                return count

            processed = asyncio.run(drain())
        return {"profile_id": jobs.profile_id, "queued": queued, "processed": processed}
    finally:
        engine.close()
        db.close()
