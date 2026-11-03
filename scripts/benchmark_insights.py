"""Synthetic SQL/collection benchmark in a fresh isolated DB; no model calls."""

import argparse
import json
import platform
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import psycopg
from advanced_common import database, write
from app import business, insights, telemetry
from app.config import Settings
from app.identity import Identity
from app.insight_metrics import percentile
from app.values import digest, jsonb, uid
from psycopg.rows import dict_row


def benchmark(output, count):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    settings, db = database(Settings(), output)
    auth = Identity("U001")
    now = datetime.now(UTC) - timedelta(minutes=5)
    turns = []
    sources = []
    scopes = []
    runs = []
    contexts = []
    session = uid()
    with psycopg.connect(settings.admin_database_url, row_factory=dict_row) as conn:
        conn.execute(
            "INSERT INTO sessions(session_id,user_id,selected_company_id) VALUES(%s,'U001','C001')",
            (session,),
        )
        for i in range(count):
            tid, sid, rid = uid(), uid(), uid()
            text = f"观测性能合成请求 {i}"
            turns.append((tid, session, uid(), i + 1, text, now))
            sources.append((sid, tid, i + 1, text, digest(text)))
            scopes.append((sid,))
            runs.append((rid, tid, now, now + timedelta(seconds=1)))
            for step in range(3):
                contexts.append(
                    (
                        uid(),
                        rid,
                        step + 1,
                        jsonb({"model": "synthetic-not-provider", "messages": []}),
                        jsonb({"sources": [{"source_id": sid, "revision": 1}]}),
                        jsonb({"prompt_tokens": 100, "completion_tokens": 10}),
                    )
                )
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO turns(turn_id,session_id,user_id,client_request_id,request_hash,source_seq,text,status,created_at) VALUES(%s,%s,'U001',%s,'synthetic',%s,%s,'completed',%s)",
                turns,
            )
            cursor.executemany(
                "INSERT INTO sources(source_id,turn_id,user_id,source_seq,role,kind,content,content_hash,scope_kind) VALUES(%s,%s,'U001',%s,'user','message',%s,%s,'company_scoped')",
                sources,
            )
            cursor.executemany(
                "INSERT INTO source_scopes(source_id,company_id) VALUES(%s,'C001')", scopes
            )
            cursor.executemany(
                "INSERT INTO runs(run_id,turn_id,generation,state,started_at,finished_at) VALUES(%s,%s,1,'completed',%s,%s)",
                runs,
            )
            cursor.executemany(
                "INSERT INTO context_snapshots(context_id,run_id,model_step,payload,payload_hash,manifest,usage,duration_ms,token_count,count_kind) VALUES(%s,%s,%s,%s,'synthetic',%s,%s,100,100,'synthetic')",
                contexts,
            )
    with psycopg.connect(settings.admin_database_url, autocommit=True) as conn:
        conn.execute("ANALYZE")
    f = insights.filters(auth)
    results = {}
    try:
        for name, action in [
            ("list", lambda: insights.requests(db, auth, f)),
            ("summary", lambda: insights.summary(db, auth, f)),
            ("detail", lambda: insights.detail(db, auth, turns[0][0])),
        ]:
            action()
            durations = []
            for _ in range(30):
                started = time.perf_counter()
                value = action()
                durations.append((time.perf_counter() - started) * 1000)
            results[name] = {
                "p50_ms": percentile(durations, 0.5),
                "p95_ms": percentile(durations, 0.95),
                "n": 30,
            }
            if name == "summary":
                assert value["request_count"] == count and value["model_attempts"] == count * 3
        execution = SimpleNamespace(run_id=runs[0][0])
        durations = {}
        for enabled in (False, True):
            values = []
            for _ in range(30):
                started = time.perf_counter()
                if enabled:
                    with telemetry.phase(
                        db, execution, "overhead:" + uid(), "tool", "程序金额查询"
                    ):
                        business.query_month(db, auth, "C001", "2026-09")
                else:
                    business.query_month(db, auth, "C001", "2026-09")
                values.append((time.perf_counter() - started) * 1000)
            durations[str(enabled)] = {
                "p50_ms": percentile(values, 0.5),
                "p95_ms": percentile(values, 0.95),
                "n": 30,
            }
        limits = {"list": 500, "summary": 1000, "detail": 1000}
        report = {
            "code_fingerprint": telemetry.code_fingerprint(),
            "environment": {
                "os": platform.platform(),
                "machine": platform.machine(),
                "processor": platform.processor(),
            },
            "kind": "synthetic_sql_and_collection_not_model_quality",
            "requests": count,
            "model_attempts_synthetic": count * 3,
            "timings": results,
            "collection": durations,
            "reference_targets_ms": limits,
            "timing_reference_met": all(results[k]["p95_ms"] <= v for k, v in limits.items()),
            "performance_is_release_gate": False,
            "functional_checks_passed": True,
            "usability_verdict": "Requires functional and browser validation; latency reference alone is not a failure.",
            "additional_p95_ms": durations["True"]["p95_ms"] - durations["False"]["p95_ms"],
        }
        report["collection_reference_met"] = report["additional_p95_ms"] <= 100
        write(output / "report.json", report)
        print(json.dumps(report, ensure_ascii=True, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--requests", type=int, default=10000, choices=[100, 10000])
    args = parser.parse_args()
    benchmark(args.output, args.requests)
