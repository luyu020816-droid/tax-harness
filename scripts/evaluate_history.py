"""Real, isolated historical-message evaluation against frozen JSON labels."""

import argparse
import json
import subprocess
from pathlib import Path

from app.config import ROOT, Settings
from app.errors import AppError
from app.evidence_selection import load_policy
from app.history_jobs import HistoryJobs
from app.history_retrieval import VERSION, HistoryRetrieval
from app.identity import Identity, create_session
from app.retrieval_engine import LocalRetrieval
from app.retrieval_metrics import calibrate
from app.sources import add_source, next_sequence
from app.values import digest, uid
from evaluate_retrieval import CachedInference, database, write

AUTH = Identity("U001")


def ingest(db, rows):
    identities = {}
    for row in rows:
        session = create_session(db, AUTH, row["company_id"])
        with db.transaction() as conn:
            sequence, turn = next_sequence(conn, AUTH), uid()
            conn.execute(
                "INSERT INTO turns(turn_id,session_id,user_id,client_request_id,request_hash,source_seq,text,status,final_message,created_at) VALUES(%s,%s,%s,%s,'synthetic-history',%s,%s,'completed','合成历史记录',%s)",
                (
                    turn,
                    session["session_id"],
                    AUTH.user_id,
                    uid(),
                    sequence,
                    row["content"] if row["role"] == "user" else "当时的查询",
                    row["created_at"],
                ),
            )
            source = add_source(
                conn,
                AUTH,
                row["content"],
                source_seq=sequence,
                role=row["role"],
                turn_id=turn,
                companies=[row["company_id"]],
            )
            conn.execute(
                "UPDATE sources SET created_at=%s WHERE source_id=%s",
                (row["created_at"], source["source_id"]),
            )
        identities[row["id"]] = str(source["source_id"])
    return identities


def trace(db, query):
    with db.transaction(readonly=True) as conn:
        return conn.execute("SELECT * FROM history_queries WHERE query_id=%s", (query,)).fetchone()


def measure(q, result, recorded, source_ids):
    gold = source_ids.get(q.get("source"))
    scores = recorded["candidates"]
    ranked = list(dict.fromkeys(str(c["source_id"]) for c in scores))
    supported = any(
        e["source_id"] == gold and q.get("required_quote", "") in e["content"]
        for e in result["evidence"]
    )
    return {
        "id": q["id"],
        "answerable": q["answerable"],
        "status": result["status"],
        "candidate_recall20": int(gold in ranked[:20]) if gold else None,
        "complete_quote": supported if gold else None,
        "negative_retained_evidence": bool(result["evidence"]) if not gold else None,
        "gold_seed": max(
            (c.get("rerank_logit", float("-inf")) for c in scores if str(c["source_id"]) == gold),
            default=float("-inf"),
        ),
        "strongest_candidate": max(
            (c.get("rerank_logit", float("-inf")) for c in scores), default=float("-inf")
        ),
        "role_locator_hash_valid": all(
            e["historical_only"]
            and e["excerpt_hash"] == digest(e["excerpt"])
            and e["locator"]["char_end"] > e["locator"]["char_start"]
            for e in result["evidence"]
        ),
    }


def summary(rows):
    positives = [r for r in rows if r["answerable"]]
    negatives = [r for r in rows if not r["answerable"]]
    return {
        "positives": len(positives),
        "negative_questions": len(negatives),
        "candidate_recall20": sum(r["candidate_recall20"] for r in positives) / len(positives),
        "complete_quotes": sum(r["complete_quote"] for r in positives),
        "negative_retained_evidence": sum(r["negative_retained_evidence"] for r in negatives),
        "role_locator_hash_valid": all(r["role_locator_hash_valid"] for r in rows),
    }


def boundaries(db, retrieval, source_ids, baseline):
    results = []

    class Unavailable:
        def profile(self):
            raise RuntimeError("Intentionally unavailable for date-only test")

    browser = HistoryRetrieval(db, retrieval.settings, Unavailable())
    args = {"company_id": "C001", "date_from": "2026-09-01", "date_to": "2026-10-01"}
    page = browser.search(AUTH, **args)
    results.append(
        {
            "id": "date_without_vectors",
            "pass": page["retrieval_mode"] == "date" and len(page["matches"]) == 5,
        }
    )
    second = browser.search(AUTH, **args, cursor=page["next_cursor"])
    got = [r["source_id"] for r in page["matches"] + second["matches"]]
    expected = {v for k, v in source_ids.items() if not k.startswith("HN")}
    results.append(
        {
            "id": "date_pagination",
            "pass": len(got) == 10 and set(got) == expected and not second["has_more"],
        }
    )
    other = retrieval.search(Identity("U002"), company_id="C001", query="访客门禁卡")
    results.append({"id": "private_user", "pass": not other["evidence"]})
    try:
        retrieval.search(Identity("U002"), company_id="C002", query="历史")
        denied = False
    except AppError as error:
        denied = error.code == "ACCESS_DENIED"
    results.append({"id": "wrong_company", "pass": denied})
    source = source_ids["H01"]
    citation = next(
        e["citation_id"] for e in baseline["H01Q1"]["evidence"] if e["source_id"] == source
    )
    with db.transaction() as conn:
        conn.execute("UPDATE sources SET recall_state='suppressed' WHERE source_id=%s", (source,))
        retained = conn.execute(
            "SELECT count(*) AS n FROM history_embeddings e JOIN history_passages p USING(passage_id) WHERE p.source_id=%s",
            (source,),
        ).fetchone()["n"]
    after = retrieval.search(AUTH, company_id="C001", query="访客门禁卡在哪里领取？")
    try:
        retrieval.read(AUTH, citation)
        stale = False
    except AppError:
        stale = True
    results.append(
        {
            "id": "suppressed_vector_and_citation",
            "pass": retained > 0
            and stale
            and all(e["source_id"] != source for e in after["evidence"]),
        }
    )
    with db.transaction(readonly=True) as conn:
        seq = conn.execute(
            "SELECT source_seq FROM sources WHERE source_id=%s", (source_ids["H02"],)
        ).fetchone()["source_seq"]
    bound = retrieval.search(AUTH, company_id="C001", query="远程支持", before_source_seq=seq)
    recorded = trace(db, bound["query_id"])
    results.append(
        {
            "id": "before_input_watermark",
            "pass": all(str(c["source_id"]) != source_ids["H02"] for c in recorded["candidates"])
            and all(e["source_seq"] < seq for e in bound["evidence"]),
        }
    )
    return {"passed": sum(r["pass"] for r in results), "total": len(results), "records": results}


def main(args):
    args.output.mkdir(parents=True, exist_ok=False)
    commit_at_start = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip()
    snapshot = {
        p.name: p.read_text(encoding="utf-8") for p in sorted((ROOT / "backend/app").glob("*.py"))
    }
    write(args.output / "code-snapshot.json", snapshot)
    suite = json.loads(
        (ROOT / "evals/history-v1" / (args.split + ".json")).read_text(encoding="utf-8")
    )
    write(args.output / "frozen-suite.json", suite)
    s, db = database(Settings(), args.split, args.output)
    try:
        engine = CachedInference(LocalRetrieval(s.knowledge_model_directory))
        profile, _ = engine.profile()
        sources = ingest(db, suite["sources"])
        write(args.output / "source-identities.json", sources)
        jobs = HistoryJobs(db, s, engine)
        while job := jobs.claim():
            jobs.run(job)
        with db.transaction(readonly=True) as conn:
            states = conn.execute(
                "SELECT state,count(*) AS n FROM history_jobs GROUP BY state"
            ).fetchall()
        assert all(r["state"] == "done" for r in states), states
        engine.embed([q["query"] for q in suite["questions"]])
        raw = HistoryRetrieval(db, s, engine, selection_policy=False)
        base_rows = []
        bundles = {}
        for q in suite["questions"]:
            result = raw.search(AUTH, company_id="C001", query=q["query"])
            recorded = trace(db, result["query_id"])
            measurement = measure(q, result, recorded, sources)
            base_rows.append(measurement)
            bundles[q["id"]] = result
            write(
                args.output / (q["id"] + "-baseline.json"),
                {"question": q, "bundle": result, "trace": recorded, "measurement": measurement},
            )
            print("measured", q["id"], flush=True)
        if args.split == "development":
            cutoff, fit = calibrate(
                [r["gold_seed"] for r in base_rows if r["answerable"]],
                [r["strongest_candidate"] for r in base_rows if not r["answerable"]],
            )
            write(
                args.output / "policy.json",
                {
                    "version": VERSION,
                    "profile_id": profile,
                    "mode": "hybrid",
                    "development_sha256": digest(suite),
                    "cutoff": cutoff,
                    "calibration": fit,
                },
            )
            policy, error = load_policy(args.output / "policy.json", profile, version=VERSION)
        else:
            policy, error = load_policy(args.policy, profile, version=VERSION)
            assert policy["development_sha256"] == digest(
                json.loads((ROOT / "evals/history-v1/development.json").read_text(encoding="utf-8"))
            )
        assert policy, error
        selected = HistoryRetrieval(db, s, engine, selection_policy=policy)
        rows = []
        for q in suite["questions"]:
            result = selected.search(AUTH, company_id="C001", query=q["query"])
            recorded = trace(db, result["query_id"])
            measurement = measure(q, result, recorded, sources)
            rows.append(measurement)
            for evidence in result["evidence"]:
                assert (
                    selected.read(AUTH, evidence["citation_id"])["excerpt"] == evidence["excerpt"]
                )
            write(
                args.output / (q["id"] + "-selected.json"),
                {"question": q, "bundle": result, "trace": recorded, "measurement": measurement},
            )
        boundary = boundaries(db, selected, sources, bundles) if args.split == "holdout" else None
        report = {
            "split": args.split,
            "suite_sha256": digest(suite),
            "profile_id": profile,
            "policy": policy,
            "baseline": summary(base_rows),
            "selected": summary(rows),
            "measurements": rows,
            "boundaries": boundary,
            "code_fingerprint": digest(snapshot),
            "code_commit_at_start": commit_at_start,
            "inference_counts": engine.counts,
            "human_review": "pending",
            "timing_note": "Exact-input real inference cache used for selection replay; not production latency.",
        }
        write(args.output / "report.json", report)
        write(
            args.output / "real-inference-cache.json",
            {"profile_id": profile, "vectors": engine.vectors, "rerank_scores": engine.scores},
        )
        print(
            json.dumps(
                {
                    "selected": report["selected"],
                    "boundaries": boundary,
                    "policy_id": policy["policy_id"],
                }
            ),
            flush=True,
        )
    finally:
        db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--split", choices=["development", "holdout"], required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--policy", type=Path)
    args = p.parse_args()
    if args.split == "holdout" and not args.policy:
        p.error("Holdout requires a locked development policy")
    main(args)
