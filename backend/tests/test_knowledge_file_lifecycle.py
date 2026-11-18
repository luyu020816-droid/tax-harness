from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from test_workbook_query import workbook

from app.identity import Identity
from app.knowledge import Knowledge
from app.knowledge_jobs import KnowledgeJobs
from app.values import uid

AUTH = Identity("U001")


def test_concurrent_renamed_upload_reuses_private_file_within_only_one_scope(db, settings, admin):
    store = Knowledge(db, settings)
    content = workbook()
    barrier = Barrier(2)

    def upload(name, auth=AUTH, company="C001"):
        return store.upload(
            auth, name=name, title=name, company_id=company, content=content, request_id=uid()
        )

    def concurrent(name):
        barrier.wait(timeout=5)
        return upload(name)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(concurrent, ("原文件.xlsx", "改名后.xlsx")))
    original = next(row for row in results if "duplicate" not in row)
    duplicate = next(row for row in results if "duplicate" in row)
    for key in ("document_id", "version_id", "file_id", "job_id", "memory_epoch"):
        assert duplicate[key] == original[key]
    assert duplicate["duplicate"]["state"] == "queued"
    assert not duplicate["duplicate"]["usable"]
    for table in ("knowledge_documents", "knowledge_files", "knowledge_versions", "retrieval_jobs"):
        assert admin.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 1

    other_company = upload("同一文件.xlsx", company="C002")
    admin.execute(
        "UPDATE user_company_grants SET can_upload_knowledge=true "
        "WHERE user_id='U002' AND company_id='C001'"
    )
    other_user = upload("同一文件.xlsx", auth=Identity("U002"))
    assert "duplicate" not in other_company and "duplicate" not in other_user
    assert (
        len({original["document_id"], other_company["document_id"], other_user["document_id"]}) == 3
    )


def test_duplicate_replay_reports_the_original_jobs_current_state(db, settings):
    store = Knowledge(db, settings)
    jobs = KnowledgeJobs(db, settings, None)
    args = dict(
        name="不完整表格.xlsx",
        title="不完整表格",
        company_id="C001",
        content=workbook(headers=("日期", "类别", "未知列")),
        request_id=uid(),
    )
    first = store.upload(AUTH, **args)
    duplicate_args = {**args, "name": "改名的表格.xlsx", "request_id": uid()}
    result = store.upload(AUTH, **duplicate_args)
    assert result["duplicate"] == {"title": args["title"], "state": "queued", "usable": False}
    jobs.run(jobs.claim())
    failed = store.upload(AUTH, **duplicate_args)
    assert failed["job_id"] == first["job_id"] and failed["replayed"]
    assert failed["duplicate"]["state"] == "failed"
    document = store.get(AUTH, first["document_id"])
    store.change(
        AUTH,
        first["document_id"],
        action="disable",
        expected_revision=document["revision"],
        request_id=uid(),
    )
    disabled = store.upload(AUTH, **{**args, "request_id": uid()})
    assert disabled["duplicate"]["state"] == "disabled"
    assert not disabled["duplicate"]["usable"]
    assert disabled["job_id"] == first["job_id"]
