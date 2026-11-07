"""Real pinned local embeddings/reranker, frozen development policy, untouched new pack.

All data is synthetic in tax_harness_test. Exact-input real-inference caching is
disclosed; timings are not production latency measurements. Never tune on review.
"""

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from app.config import ROOT, Settings
from app.conversation_history import search
from app.conversation_jobs import ConversationJobs
from app.conversation_retrieval import select_candidates
from app.retrieval_engine import LocalRetrieval
from app.telemetry import code_fingerprint
from app.values import digest, safe_json
from evaluate_conversation_strategies import reset
from evaluate_retrieval import CachedInference
from verify_conversations_live import AUTH, seed


def write(path, value):
    path.write_text(
        json.dumps(safe_json(value), ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def seed_pack(db, jobs, suite):
    from datetime import datetime

    from app.identity import create_session

    session = str(create_session(db, AUTH, "C001")["session_id"])
    sources = {}
    for item in suite["fixtures"]:
        when = datetime.fromisoformat(item["when"]) if item.get("when") else None
        sources[item["id"]] = seed(db, session, item["text"], when)
    while job := jobs.claim():
        await jobs.process(job)
    with db.transaction(readonly=True) as conn:
        incomplete = conn.execute(
            "SELECT count(*) n FROM conversation_index_jobs WHERE profile_id=%s AND state<>'ready'",
            (jobs.profile_id,),
        ).fetchone()["n"]
        if incomplete:
            raise RuntimeError(
                f"Index has {incomplete} unfinished jobs; do not claim a full evaluation"
            )
    return sources


def observation():
    return SimpleNamespace(auth=AUTH, source_seq=10**9)


def quality(case, sources, ids):
    gold = {sources[key] for key in case["gold"]}
    rank = [ids.index(key) + 1 for key in gold if key in ids]
    return {
        "correct": gold <= set(ids) if gold else not ids,
        "recall_at_5": len(rank) / len(gold) if gold else None,
        "reciprocal_rank": 1 / min(rank) if rank else 0 if gold else None,
        "false_positive": bool(ids) if not gold else None,
    }


def calibrate(rows):
    def grid(key):
        values = sorted(
            {
                float(item[key])
                for row in rows
                for item in row["candidates"]
                if key in item
            }
        )
        if not values:
            raise RuntimeError("No actual development scores")
        return sorted(
            set(
                [values[0] - 1e-6, values[-1] + 1e-6]
                + [values[n * (len(values) - 1) // 32] for n in range(33)]
            )
        )

    best, selected = None, None
    for dense_min in grid("dense_score"):
        for rerank_min in grid("rerank_score"):
            p = {"dense_min": dense_min, "rerank_min": rerank_min}
            hit, count, false = 0, 0, 0
            for row in rows:
                ids = [
                    c["source_id"] for c in select_candidates(row["candidates"], p)[:5]
                ]
                gold = set(row["gold"])
                if not gold and ids:
                    false += 1
                hit += bool(gold and gold <= set(ids))
                count += len(ids)
            if false:
                continue
            score = (hit, -count, -dense_min, -rerank_min)
            if best is None or score > best:
                best, selected = score, p
    if selected is None:
        raise RuntimeError("No development policy satisfies no-answer guard")
    return selected, {
        "positive_hits": best[0],
        "candidate_count": -best[1],
        "no_answer_false_positives": 0,
    }


def summarize(rows):
    result = {}
    for mode in dict.fromkeys(row["mode"] for row in rows):
        result[mode] = {}
        for category in dict.fromkeys(row["category"] for row in rows):
            group = [
                row
                for row in rows
                if row["mode"] == mode and row["category"] == category
            ]
            if not group:
                continue
            positives = [r for r in group if r["recall_at_5"] is not None]
            result[mode][category] = {
                "count": len(group),
                "correct": sum(r["correct"] for r in group),
                "recall_at_5": sum(r["recall_at_5"] for r in positives) / len(positives)
                if positives
                else None,
                "mrr": sum(r["reciprocal_rank"] for r in positives) / len(positives)
                if positives
                else None,
                "false_positives": sum(bool(r["false_positive"]) for r in group),
            }
    return result


async def main(args):
    args.output.mkdir(parents=True, exist_ok=False)
    base = Settings()
    settings, db = reset(base)
    engine = LocalRetrieval(settings.knowledge_model_directory)
    inference = CachedInference(engine)
    jobs = ConversationJobs(db, settings, inference)
    try:
        jobs.configure()
        development = json.loads(
            (ROOT / "evals/conversation-v1/cases.json").read_text(encoding="utf-8")
        )
        review = json.loads(
            (ROOT / "evals/conversation-semantic-v1/review-cases.json").read_text(
                encoding="utf-8"
            )
        )
        write(
            args.output / "frozen-inputs.json",
            {"development": development, "review": review},
        )
        sources = await seed_pack(db, jobs, development)
        # Cache only actual model outputs; no synthetic replacement and no
        # implication that cached query timing equals the production path.
        queries = [
            c["arguments"].get("query", "")
            for s in (development, review)
            for c in s["cases"]
            if c["arguments"].get("query")
        ]
        inference.embed(queries)
        shadow = settings.model_copy(update={"conversation_semantic_mode": "shadow"})
        calibration = []
        for case in development["cases"]:
            if not case["arguments"].get("query"):
                continue
            bundle = search(
                db,
                observation(),
                settings=shadow,
                engine=inference,
                **case["arguments"],
            )
            with db.transaction(readonly=True) as conn:
                row = conn.execute(
                    "SELECT query_plan FROM history_queries WHERE query_id=%s",
                    (bundle["query_id"],),
                ).fetchone()
                diagnostic = conn.execute(
                    "SELECT candidates FROM history_queries WHERE query_id=%s",
                    (row["query_plan"]["shadow_query_id"],),
                ).fetchone()
            calibration.append(
                {
                    "id": case["id"],
                    "gold": [sources[k] for k in case["gold"]],
                    "candidates": diagnostic["candidates"],
                }
            )
            print("development", case["id"], flush=True)
        write(args.output / "development-scores.json", calibration)
        thresholds, metrics = calibrate(calibration)
        policy = {
            "profile_id": jobs.profile_id,
            "algorithm": "dense-rerank-v1",
            "development_hash": digest(development),
            "status": "candidate_pending_human_review",
            **thresholds,
        }
        path = args.output / "candidate-policy.json"
        write(path, policy)
        config = settings.model_copy(
            update={
                "conversation_semantic_mode": "on",
                "conversation_selection_policy": path,
            }
        )
        write(args.output / "policy-selection.json", metrics)
        # Separate reset removes development dialogue from review search. The
        # profile and real inference cache are reused without changing policy.
        db.close()
        settings, db = reset(base)
        jobs = ConversationJobs(db, settings, inference)
        jobs.configure()
        sources = await seed_pack(db, jobs, review)
        rows = []
        for mode in ("lexical", "semantic", "auto"):
            for case in review["cases"]:
                # Empty date browse has a single chronological path in all modes.
                bundle = search(
                    db,
                    observation(),
                    mode=mode,
                    settings=config,
                    engine=inference,
                    **case["arguments"],
                )
                ids = [m["source_id"] for m in bundle["matches"]]
                rows.append(
                    {
                        "id": case["id"],
                        "category": case["category"],
                        "mode": mode,
                        **quality(case, sources, ids),
                        "bundle": bundle,
                    }
                )
                write(
                    args.output / "report.json",
                    {
                        "backend_fingerprint": code_fingerprint(),
                        "profile": jobs.profile_id,
                        "development_hash": digest(development),
                        "review_hash": digest(review),
                        "review_label_status": review["label_status"],
                        "policy": policy,
                        "cache_counts": inference.counts,
                        "rows": rows,
                        "summary": summarize(rows),
                        "timing_scope": "Exact-input real-inference cache; not production latency or human acceptance.",
                    },
                )
                print("review", mode, case["id"], rows[-1]["correct"], flush=True)
        with db.transaction(readonly=True) as conn:
            assert (
                conn.execute("SELECT count(*) n FROM knowledge_documents").fetchone()[
                    "n"
                ]
                == 0
            )
    finally:
        engine.close()
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
