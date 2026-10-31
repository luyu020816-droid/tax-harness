"""Executed by the new environment, not imported from the development package."""

import argparse
import json
from pathlib import Path

from app import business
from app.config import ROOT, Settings
from app.db import Database
from app.errors import AppError
from app.identity import Identity
from app.knowledge import Knowledge
from app.knowledge_jobs import KnowledgeJobs
from app.knowledge_retrieval import KnowledgeRetrieval
from app.retrieval_engine import LocalRetrieval
from app.values import uid


def main(args):
    s = Settings()
    assert ROOT == Path.cwd(), (ROOT, Path.cwd())
    db = Database(s.database_url)
    db.open()
    engine = LocalRetrieval(s.knowledge_model_directory)
    try:
        assert (
            str(business.query_month(db, Identity("U001"), "C001", "2026-09")["invoice_total"])
            == "9622.06"
        )
        try:
            business.query_month(db, Identity("U003"), "C001", "2026-09")
            raise AssertionError("Scope leaked")
        except AppError as error:
            assert error.code == "ACCESS_DENIED"
        knowledge = Knowledge(db, s)
        doc = knowledge.upload(
            Identity("U001"),
            name="安装自检.txt",
            content="未经批准不得外发内部资料。".encode(),
            company_id="C001",
            title="安装自检",
            request_id=uid(),
        )
        jobs = KnowledgeJobs(db, s, engine)
        jobs.run(jobs.claim())
        assert knowledge.job(Identity("U001"), doc["job_id"])["state"] == "done"
        retrieval = KnowledgeRetrieval(db, s, engine)
        answer = retrieval.search(
            Identity("U001"),
            company_id="C001",
            query="内部资料可以未经批准就外发吗？",
            document_ids=[doc["document_id"]],
        )
        assert answer["evidence"], answer
        ready = retrieval.ready(validate=True)
        assert ready["ready"] and ready["ocr"], ready
        args.output.write_text(
            json.dumps(
                {"pass": True, "root": str(ROOT), "knowledge_ready": ready},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    finally:
        engine.close()
        db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    main(p.parse_args())
