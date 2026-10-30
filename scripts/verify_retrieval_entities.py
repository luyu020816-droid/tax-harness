"""Same-pool exact-entity ablation, separate from the frozen held-out score."""

import argparse
import json

from app.config import Settings
from app.identity import Identity
from app.knowledge import Knowledge
from app.knowledge_jobs import KnowledgeJobs
from app.knowledge_retrieval import KnowledgeRetrieval
from app.values import uid
from evaluate_retrieval import CachedInference, LocalRetrieval, database, write


def main(args):
    args.output.mkdir(parents=True, exist_ok=False)
    s, db = database(Settings(), "holdout", args.output)
    try:
        engine = CachedInference(LocalRetrieval(s.knowledge_model_directory))
        store, jobs = Knowledge(db, s), KnowledgeJobs(db, s, engine)
        auth = Identity("U001")
        docs = []
        for entity, approver in (("F012", "财务负责人"), ("F013", "仓库保管员")):
            document = store.upload(
                auth,
                name=entity + ".txt",
                content=f"{entity}的附件归档须{approver}批准；{entity}原始记录不得省略。".encode(),
                company_id="C001",
                title=entity + "附件归档规定",
                request_id=uid(),
            )
            jobs.run(jobs.claim())
            assert store.get(auth, document["document_id"])["active_version_id"]
            docs.append(str(document["document_id"]))
        retrieval = KnowledgeRetrieval(db, s, engine)
        records = []
        for entities in ([], ["F012"]):
            bundle = retrieval.search(
                auth,
                company_id="C001",
                query="F012附件归档由谁批准？",
                document_ids=docs,
                entities=entities,
            )
            with db.transaction(readonly=True) as conn:
                trace = conn.execute(
                    "SELECT coverage,candidates FROM retrieval_queries WHERE query_id=%s",
                    (bundle["query_id"],),
                ).fetchone()
                documents_by_chunk = {
                    str(r["chunk_id"]): str(r["document_id"])
                    for r in conn.execute(
                        "SELECT c.chunk_id,v.document_id FROM knowledge_chunks c JOIN knowledge_versions v USING(version_id) WHERE v.document_id=ANY(%s::uuid[])",
                        (docs,),
                    ).fetchall()
                }
                for candidate in trace["candidates"]:
                    candidate["document_id"] = documents_by_chunk[str(candidate["chunk_id"])]
            records.append({"entities": entities, "bundle": bundle, "trace": trace})
        base, exact = records
        exact_evidence = exact["bundle"]["evidence"]
        checks = {
            "baseline_has_other_entity_candidate": any(
                str(c["document_id"]) == docs[1] for c in base["trace"]["candidates"]
            ),
            "constrained_candidates_exclude_other_entity": bool(exact["trace"]["candidates"])
            and all(str(c["document_id"]) == docs[0] for c in exact["trace"]["candidates"]),
            "constrained_evidence_has_expected_document": bool(exact_evidence)
            and all(str(e["document_id"]) == docs[0] for e in exact_evidence),
        }
        write(
            args.output / "report.json",
            {
                "kind": "same_query_same_pool_entity_ablation",
                "checks": checks,
                "passed": all(checks.values()),
                "records": records,
                "inference_counts": engine.counts,
                "scope": "Two synthetic documents; measures deterministic candidate isolation, not a population-level recall improvement. Does not alter frozen holdout results.",
            },
        )
        print(json.dumps(checks), flush=True)
        assert all(checks.values())
    finally:
        db.close()


if __name__ == "__main__":
    from pathlib import Path

    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True, type=Path)
    main(p.parse_args())
