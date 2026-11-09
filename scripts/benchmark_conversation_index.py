"""SQL/authorization volume benchmark over imported identical synthetic messages.

A real embedding of the identical text is reused, not a synthetic vector. This
does NOT measure 10k distinct-text embedding throughput or natural chat quality.
"""

import argparse
import asyncio
import os
import statistics
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from app import conversation_index as index
from app.config import Settings
from app.conversation_history import search
from app.conversation_jobs import ConversationJobs
from app.identity import Identity, create_session
from app.retrieval_engine import LocalRetrieval, vector_literal
from app.values import digest, uid
from evaluate_conversation_semantics import observation, write
from evaluate_conversation_strategies import reset
from psycopg.conninfo import make_conninfo
from verify_conversations_live import AUTH, seed


async def main(args):
    args.output.mkdir(parents=True, exist_ok=False)
    base = Settings()
    engine = LocalRetrieval(base.knowledge_model_directory)
    report = {
        "scope": "Repeated synthetic text; real same-input vector reused. Not distinct-text embedding throughput or semantic quality.",
        "rows": [],
    }
    stop = threading.Event()
    peak = [0]

    def sample():
        try:
            import psutil

            process = psutil.Process(os.getpid())
            while not stop.wait(0.1):
                total = process.memory_info().rss
                for child in process.children(recursive=True):
                    try:
                        total += child.memory_info().rss
                    except psutil.Error:
                        pass
                peak[0] = max(peak[0], total)
        except ImportError:
            pass

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        raw = "数据库迁移前先做完整备份，保留离线回退副本，不授权修改发票。"
        encoded = index.embedding_text("user", raw)
        vector = vector_literal(engine.embed([encoded])[0])
        historical_time = datetime.now(UTC) - timedelta(days=30)
        for count in (1000, 10000):
            config, db = reset(base)
            config = config.model_copy(
                update={
                    "conversation_semantic_mode": "on",
                    "conversation_selection_policy": args.policy.resolve(),
                }
            )
            jobs = ConversationJobs(db, config, engine)
            jobs.configure()
            sessions = [
                str(create_session(db, AUTH, "C001")["session_id"])
                for _ in range(count // 10)
            ]
            ids = [(uid(), uid(), uid()) for _ in range(count)]
            start = time.monotonic()
            with psycopg.connect(
                make_conninfo(base.admin_database_url, dbname="tax_harness_test")
            ) as conn:
                with conn.cursor().copy(
                    "COPY turns(turn_id,session_id,user_id,client_request_id,request_hash,source_seq,text,status,created_at) FROM STDIN"
                ) as copy:
                    for n, (turn, source, generation) in enumerate(ids, 1):
                        copy.write_row(
                            (
                                turn,
                                sessions[(n - 1) // 10],
                                "U001",
                                turn,
                                digest(raw),
                                n,
                                raw,
                                "completed",
                                historical_time,
                            )
                        )
                with conn.cursor().copy(
                    "COPY sources(source_id,turn_id,user_id,role,kind,scope_kind,content,content_hash,source_seq,created_at) FROM STDIN"
                ) as copy:
                    for n, (turn, source, generation) in enumerate(ids, 1):
                        copy.write_row(
                            (
                                source,
                                turn,
                                "U001",
                                "user",
                                "synthetic_volume_fixture",
                                "company_scoped",
                                raw,
                                digest(raw),
                                n,
                                historical_time,
                            )
                        )
                with conn.cursor().copy(
                    "COPY source_scopes(source_id,company_id) FROM STDIN"
                ) as copy:
                    for turn, source, generation in ids:
                        copy.write_row((source, "C001"))
                with conn.cursor().copy(
                    "COPY conversation_passages(passage_id,source_id,source_revision,source_hash,profile_id,generation_id,ordinal,char_start,char_end,excerpt_hash,embedding_input_hash,embedding) FROM STDIN"
                ) as copy:
                    for turn, source, generation in ids:
                        copy.write_row(
                            (
                                uid(),
                                source,
                                1,
                                digest(raw),
                                jobs.profile_id,
                                generation,
                                0,
                                0,
                                len(raw),
                                digest(raw),
                                digest(encoded),
                                vector,
                            )
                        )
                conn.execute(
                    "UPDATE conversation_index_jobs j SET state='ready',published_generation_id=p.generation_id,target_generation_id=p.generation_id,expected_chunk_count=1 FROM conversation_passages p WHERE p.source_id=j.source_id AND p.profile_id=j.profile_id"
                )
                conn.execute(
                    "UPDATE users SET next_source_seq=%s WHERE user_id='U001'", (count,)
                )
                # COPY compresses weeks of history into a single transaction.
                # Refresh planner statistics before timing the settled corpus.
                conn.execute(
                    "ANALYZE turns, sessions, sources, source_scopes, conversation_events, "
                    "conversation_index_jobs, conversation_passages"
                )
            imported_ms = round((time.monotonic() - start) * 1000)
            durations = []
            for attempt in range(3):
                start = time.monotonic()
                bundle = await asyncio.to_thread(
                    search,
                    db,
                    observation(),
                    query="资料库搬家之前先干什么",
                    settings=config,
                    engine=engine,
                )
                durations.append(round((time.monotonic() - start) * 1000))
                assert (
                    bundle["matches"]
                    and bundle["coverage"]["eligible"] == count
                    and bundle["coverage"]["indexed"] == count
                )
            # One genuine background build competes with a foreground query.
            session = str(create_session(db, AUTH, "C001")["session_id"])
            seed(db, session, "并发后台构建仅讨论资料迁移，不授权写入。")
            job = jobs.claim()
            start = time.monotonic()
            result, _ = await asyncio.gather(
                asyncio.to_thread(
                    search,
                    db,
                    observation(),
                    query="资料库搬家之前先干什么",
                    settings=config,
                    engine=engine,
                ),
                jobs.process(job),
            )
            competitive_ms = round((time.monotonic() - start) * 1000)
            assert result["matches"]
            from types import SimpleNamespace

            other = SimpleNamespace(auth=Identity("U002"), source_seq=10**9)
            assert not search(
                db,
                other,
                query="资料库搬家之前先干什么",
                settings=config,
                engine=engine,
            )["matches"]
            with db.transaction(readonly=True) as conn:
                assert (
                    conn.execute(
                        "SELECT state FROM conversation_index_jobs WHERE source_id=%s AND profile_id=%s",
                        (job["source_id"], job["profile_id"]),
                    ).fetchone()["state"]
                    == "ready"
                )
            report["rows"].append(
                {
                    "messages": count,
                    "sessions": len(sessions),
                    "import_ms": imported_ms,
                    "query_ms": durations,
                    "p50_ms": statistics.median(durations),
                    "sample_max_ms": max(durations),
                    "competitive_query_and_one_real_build_ms": competitive_ms,
                    "cross_user_leaks": 0,
                    "candidate_limit_reached": bundle["candidate_limit_reached"],
                    "python_and_model_peak_rss": peak[0] or None,
                    "memory_scope": "Python plus child model process; excludes separate PostgreSQL server.",
                    "cold_scope": "First request of three in this process; OS file caches not controlled.",
                }
            )
            write(args.output / "report.json", report)
            print(count, durations, competitive_ms, flush=True)
            db.close()
    finally:
        stop.set()
        sampler.join(timeout=2)
        engine.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
