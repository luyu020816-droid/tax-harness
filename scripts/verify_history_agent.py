"""Live Agent rehearsal in a new database cloned from the frozen synthetic corpus."""

import argparse
import asyncio
import ctypes
import faulthandler
import json
import sys
from pathlib import Path

from app.config import ROOT, Settings
from app.evidence_selection import load_policy
from app.history_jobs import HistoryJobs
from app.history_retrieval import VERSION, HistoryRetrieval
from app.identity import Identity, create_session
from app.knowledge_retrieval import KnowledgeRetrieval
from app.model import HttpModel
from app.runtime import Runtime
from app.trace import read_run
from app.turns import accept_turn, read_turn
from app.values import digest, uid
from evaluate_history import ingest
from evaluate_retrieval import CachedInference, LocalRetrieval, database, write


async def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    original = json.loads((args.evaluation / "report.json").read_text(encoding="utf-8"))
    assert original["split"] == "holdout"
    s = Settings()
    assert s.model_configured
    s, db = database(s, "holdout", args.output)
    try:
        engine = CachedInference(LocalRetrieval(s.knowledge_model_directory))
        cache = json.loads(
            (args.evaluation / "real-inference-cache.json").read_text(encoding="utf-8")
        )
        engine.vectors, engine.scores = cache["vectors"], cache["rerank_scores"]
        profile, _ = engine.profile()
        assert profile == original["profile_id"] == cache["profile_id"]
        policy, error = load_policy(
            ROOT / "evals/history-v1/selection-policy.candidate.json", profile, version=VERSION
        )
        assert policy and policy["policy_id"] == original["policy"]["policy_id"], error
        history = HistoryRetrieval(db, s, engine, selection_policy=policy)
        runtime = Runtime(db, s, HttpModel(s), retrieval=KnowledgeRetrieval(db, s, engine))
        runtime.tools.history = history
        suite = json.loads((args.evaluation / "frozen-suite.json").read_text(encoding="utf-8"))
        source_ids = ingest(db, suite["sources"])
        jobs = HistoryJobs(db, s, engine)
        while job := jobs.claim():
            jobs.run(job)
        probes = [
            {
                "id": q["id"],
                "text": "请查本人甲企业过去的对话原文回答：" + q["query"],
                "source": source_ids.get(q.get("source")),
                "quote": q.get("required_quote"),
            }
            for q in suite["questions"]
            if q["id"] in ("H02Q1", "H05Q1", "H09Q1", "HU01")
        ]
        probes += [
            {
                "id": "current_business_truth",
                "text": "以前助手说F004用途保存为办公用品采购，现在甲企业数据库里F004的用途到底是什么？请核对历史原话和当前数据。",
            },
            {
                "id": "date_browse",
                "text": "请按日期列出甲企业2026年9月3日的历史原话，先给第一页，不需要关键词搜索。",
            },
            {"id": "date_next", "text": "继续下一页，保持刚才的日期和企业范围。"},
            {"id": "date_end", "text": "再下一页，仍保持刚才的范围。"},
        ]
        records = []
        auth = Identity("U001")
        date_session, previous_date_query, first_date_sources = None, None, set()
        for probe in probes:
            session = (
                date_session
                if probe["id"] in ("date_next", "date_end")
                else create_session(db, auth, "C001")
            )
            turn = accept_turn(db, auth, str(session["session_id"]), uid(), probe["text"])
            ex = runtime.claim()
            assert ex and ex.turn_id == turn["turn_id"]
            await runtime.run(ex)
            result = read_turn(db, auth, turn["turn_id"])
            trace = read_run(db, auth, ex.run_id)
            cards = [c for c in result["final_cards"] if c["kind"] == "history_quote"]
            quotes = [history.read(auth, c["citation_id"]) for c in cards]
            used = any(t["name"] == "search_history" for t in trace["tools"])
            with db.transaction(readonly=True) as conn:
                writes = conn.execute(
                    "SELECT count(*) AS n FROM operations o JOIN sources s ON s.source_id=o.source_id WHERE s.turn_id=%s",
                    (turn["turn_id"],),
                ).fetchone()["n"]
            supported = (
                any(
                    q["source_id"] == probe.get("source") and probe["quote"] in q["excerpt"]
                    for q in quotes
                )
                if probe.get("source")
                else True
            )
            if probe["id"] == "HU01":
                supported = not cards
            if probe["id"] == "current_business_truth":
                supported = (
                    any(t["name"] == "find_invoices" for t in trace["tools"])
                    and any(q["role"] == "assistant" for q in quotes)
                    and any(c["kind"] == "invoices" for c in result["final_cards"])
                )
            if probe["id"] == "date_browse":
                supported = len(cards) == 5 and any(
                    t["name"] == "search_history" and t["arguments"].get("date_from")
                    for t in trace["tools"]
                )
                date_session = session
                first_date_sources = {q["source_id"] for q in quotes}
            if probe["id"] in ("date_next", "date_end"):
                continued = any(
                    t["name"] == "search_history"
                    and t["arguments"].get("cursor") == previous_date_query
                    for t in trace["tools"]
                )
                supported = continued and (
                    len(cards) == 5
                    and first_date_sources.isdisjoint({q["source_id"] for q in quotes})
                    if probe["id"] == "date_next"
                    else not cards and "末页" in (result["final_message"] or "")
                )
            if probe["id"].startswith("date_"):
                searches = [
                    json.loads(t["actual_result"])["data"]
                    for t in trace["tools"]
                    if t["name"] == "search_history" and json.loads(t["actual_result"]).get("ok")
                ]
                if searches:
                    previous_date_query = searches[-1]["query_id"]
            row = {
                "id": probe["id"],
                "pass": result["status"] == "completed" and used and supported and writes == 0,
                "invoice_writes": writes,
                "human_review": "pending",
                "result": result,
                "trace": trace,
            }
            write(args.output / (probe["id"] + ".json"), row)
            records.append({k: row[k] for k in ("id", "pass", "invoice_writes", "human_review")})
            print(probe["id"], "pass" if row["pass"] else "FAIL", flush=True)
        write(
            args.output / "report.json",
            {
                "passed": sum(r["pass"] for r in records),
                "total": len(records),
                "records": records,
                "code_fingerprint": digest(
                    {
                        p.name: p.read_text(encoding="utf-8")
                        for p in sorted((ROOT / "backend/app").glob("*.py"))
                    }
                ),
                "policy_id": policy["policy_id"],
                "inference_counts": engine.counts,
                "native_restarts": engine.engine.process.restarts if engine.engine.process else 0,
                "scope": "Supplemental live Agent rehearsal in a fresh database with the same frozen synthetic corpus and actual profile-bound inference cache; not a new independent holdout score. Final natural-language semantic acceptance is pending.",
            },
        )
        if not all(r["pass"] for r in records):
            raise SystemExit(1)
    finally:
        if "engine" in locals():
            engine.engine.close()
        db.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)
    faulthandler.enable()
    p = argparse.ArgumentParser()
    p.add_argument("--evaluation", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    asyncio.run(run(p.parse_args()))
