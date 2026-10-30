"""Post-selection integration probes, separate from the frozen retrieval score."""

import argparse
import json
from pathlib import Path

from app.config import ROOT, Settings
from app.db import Database
from app.evidence_selection import load_policy
from app.knowledge_retrieval import KnowledgeRetrieval
from app.values import digest
from evaluate_retrieval import CachedInference, LocalRetrieval, write
from psycopg.conninfo import make_conninfo
from retrieval_model_checks import run_model_checks


def main(args):
    args.output.mkdir(parents=True, exist_ok=False)
    source = args.evaluation.resolve()
    original = json.loads((source / "report.json").read_text(encoding="utf-8"))
    assert original["split"] == "holdout"
    s = Settings()
    name = "tax_harness_retrieval_holdout_v1_" + digest(str(source))[:8]
    s = s.model_copy(
        update={
            "database_url": make_conninfo(s.database_url, dbname=name),
            "admin_database_url": make_conninfo(s.admin_database_url, dbname=name),
        }
    )
    db = Database(s.database_url)
    db.open()
    try:
        engine = CachedInference(LocalRetrieval(s.knowledge_model_directory))
        profile, _ = engine.profile()
        cache = json.loads((source / "real-inference-cache.json").read_text(encoding="utf-8"))
        assert cache["profile_id"] == profile
        engine.vectors, engine.scores = cache["vectors"], cache["rerank_scores"]
        policy, error = load_policy(
            ROOT / "evals/retrieval-v1/selection-policy.candidate.json", profile
        )
        assert policy and policy["policy_id"] == original["policy"]["policy_id"], error
        retrieval = KnowledgeRetrieval(db, s, engine, selection_policy=policy)
        suite = json.loads((source / "frozen-suite.json").read_text(encoding="utf-8"))
        result = run_model_checks(s, db, retrieval, suite, args.output, question_ids=set(args.case))
        record = {
            "kind": "post_group_closure_agent_integration",
            "original_evaluation": str(source),
            "source_report_sha256": digest(original),
            "policy_id": policy["policy_id"],
            "result": result,
            "inference_counts": engine.counts,
            "corpus_note": "Same holdout DB after boundary tests; includes auxiliary boundary documents. These probes do not replace or increase the 70-item frozen held-out score.",
            "code_fingerprint": digest(
                {
                    p.name: p.read_text(encoding="utf-8")
                    for p in sorted((ROOT / "backend/app").glob("*.py"))
                }
            ),
        }
        write(args.output / "report.json", record)
        print(
            json.dumps(
                {"passed": result.get("passed"), "total": result.get("total")}, ensure_ascii=False
            ),
            flush=True,
        )
    finally:
        db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--evaluation", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--case", action="append", required=True)
    main(p.parse_args())
