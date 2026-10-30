"""Real local-component smoke test in its own database; no fake vectors."""

import json
import time

import psycopg
from app.cli import initialize
from app.config import ROOT, Settings
from app.db import Database
from app.identity import Identity
from app.knowledge import Knowledge
from app.knowledge_jobs import KnowledgeJobs
from app.knowledge_retrieval import KnowledgeRetrieval
from app.retrieval_engine import LocalRetrieval
from app.values import safe_json, uid
from psycopg.conninfo import make_conninfo


def main():
    settings = Settings()
    database = "tax_harness_knowledge_live"
    with psycopg.connect(
        make_conninfo(settings.admin_database_url, dbname="postgres"), autocommit=True
    ) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
            conn.execute("CREATE DATABASE tax_harness_knowledge_live")
    settings = settings.model_copy(
        update={
            "database_url": make_conninfo(settings.database_url, dbname=database),
            "admin_database_url": make_conninfo(settings.admin_database_url, dbname=database),
        }
    )
    initialize(settings)
    db = Database(settings.database_url)
    db.open()
    auth = Identity("U001")
    engine = LocalRetrieval(settings.knowledge_model_directory)
    store, jobs, retrieval = (
        Knowledge(db, settings),
        KnowledgeJobs(db, settings, engine),
        KnowledgeRetrieval(db, settings, engine),
    )
    fixtures = [
        (
            "差旅住宿审批制度（联调样例）",
            "出差产生的住宿费用必须取得部门负责人的批准。未经事先批准，不得报销酒店费用。紧急出行可先向部门负责人申请加急审批，但不得省略批准手续。",
        ),
        (
            "食堂菜单（联调样例）",
            "公司食堂周二午餐提供米饭、青菜和豆腐汤。食堂餐具在指定位置回收。",
        ),
    ]
    created = []
    try:
        for title, text in fixtures:
            result = store.upload(
                auth,
                name=title + ".txt",
                content=text.encode(),
                company_id="C001",
                title=title,
                request_id=uid(),
            )
            job = jobs.claim()
            jobs.run(job)
            actual = store.get(auth, result["document_id"])
            assert actual["active_version_id"] == result["version_id"], store.job(
                auth, result["job_id"]
            )
            created.append(result)
            print("indexed", title, flush=True)
        answers = []
        for query in (
            "出差住店的报销要由谁同意？",
            "临时着急出门可以不走审批吗？",
            "未获批准的酒店开销能报销吗？",
        ):
            started = time.monotonic()
            result = retrieval.search(
                auth,
                company_id="C001",
                query=query,
                document_ids=[d["document_id"] for d in created],
            )
            top = result["evidence"][0] if result["evidence"] else None
            ok = (
                result["retrieval_mode"] == "hybrid"
                and top
                and top["document_id"] == created[0]["document_id"]
            )
            answers.append(
                {
                    "query": query,
                    "pass": bool(ok),
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "result": result,
                }
            )
            print("query", "pass" if ok else "FAIL", query, flush=True)
        profile_id, manifest = engine.profile()
        record = {
            "kind": "real_local_components",
            "profile_id": profile_id,
            "manifest": manifest,
            "documents": created,
            "answers": answers,
        }
        path = ROOT / ".local/evidence/knowledge-real-components.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(safe_json(record), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if not all(a["pass"] for a in answers):
            raise SystemExit("Real component ranking did not pass")
    finally:
        db.close()


if __name__ == "__main__":
    main()
