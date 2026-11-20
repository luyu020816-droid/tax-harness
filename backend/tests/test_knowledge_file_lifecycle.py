from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest
from test_knowledge import knowledge, uploaded  # noqa: F401
from test_workbook_query import workbook

from app import business_dates
from app.errors import AppError
from app.identity import Identity
from app.knowledge import Knowledge
from app.knowledge_admin import publish_company, revoke_read
from app.knowledge_jobs import KnowledgeJobs
from app.values import uid

AUTH = Identity("U001")


def test_kb01_future_publication_preserves_earlier_applicable_version(knowledge, monkeypatch):  # noqa: F811
    store, jobs, retrieval = knowledge
    today = date(2026, 11, 19)
    monkeypatch.setattr(business_dates, "business_today", lambda _at=None: today)
    first = uploaded(knowledge, "发票用途旧条款。", effective_to=date(2026, 12, 1))
    old = retrieval.search(AUTH, company_id="C001", query="发票用途")["evidence"][0]
    document = store.get(AUTH, first["document_id"])
    second = uploaded(
        knowledge, "发票用途新条款。", document_id=first["document_id"],
        expected_revision=document["revision"], effective_from=date(2026, 12, 1),
    )
    draft = store.upload(
        AUTH, name="待发布.txt", content="发票用途未发布版本。".encode(),
        company_id="C001", title="企业制度", request_id=uid(), document_id=first["document_id"],
        expected_revision=document["revision"] + 1, effective_from=date(2026, 9, 1),
    )
    current = store.get(AUTH, first["document_id"])
    # A publish raced ahead while this upload was parsing. It may finish
    # indexing, but it cannot become effective without publication.
    store.change(AUTH, first["document_id"], action="publish", version_id=second["version_id"],
                 expected_revision=current["revision"], request_id=uid())
    jobs.run(jobs.claim())
    assert store.job(AUTH, draft["job_id"])["progress"]["published"] is False
    for day, expected in (
        (date(2026, 9, 20), first),
        (date(2026, 11, 19), first),
        (date(2026, 12, 1), second),
    ):
        found = retrieval.search(
            AUTH, company_id="C001", query="发票用途", as_of=day,
            document_ids=[first["document_id"]],
        )
        assert [row["version_id"] for row in found["evidence"]] == [expected["version_id"]]
        assert store.get(AUTH, first["document_id"], as_of=day)["active_version_id"] == expected["version_id"]
        assert store.file(AUTH, first["document_id"], expected["version_id"], as_of=day)["file_id"]
    assert store.list(AUTH, "C001")["documents"][0]["active_version_id"] == first["version_id"]
    today = date(2026, 12, 1)
    assert store.list(AUTH, "C001")["documents"][0]["active_version_id"] == second["version_id"]
    historical = retrieval.read(AUTH, old["citation_id"])
    assert historical["version_id"] == first["version_id"]
    assert historical["version_timing"]["applicability"] == "historical"
    assert historical["version_timing"]["as_of"] == "2026-11-19"
    with pytest.raises(AppError):
        store.file(AUTH, first["document_id"], first["version_id"])
    current = store.get(AUTH, first["document_id"])
    store.change(AUTH, first["document_id"], action="disable",
                 expected_revision=current["revision"], request_id=uid())
    with pytest.raises(AppError):
        retrieval.read(AUTH, old["citation_id"])
    with pytest.raises(AppError):
        store.file(AUTH, first["document_id"], first["version_id"], as_of=date(2026, 11, 19))


def test_kb01_highest_publication_wins_and_requires_whole_month(knowledge):  # noqa: F811
    store, _, retrieval = knowledge
    first = uploaded(knowledge, "发票用途第一版。")
    for content in ("发票用途第二版。", "发票用途第三版。"):
        current = store.get(AUTH, first["document_id"])
        latest = uploaded(
            knowledge, content, document_id=first["document_id"],
            expected_revision=current["revision"], effective_from=date(2026, 11, 20),
        )
    point = retrieval.search(AUTH, company_id="C001", query="发票用途", as_of=date(2026, 11, 20))
    assert point["evidence"][0]["version_id"] == latest["version_id"]
    whole = retrieval.search(AUTH, company_id="C001", query="发票用途", period="2026-11")
    assert whole["status"] == "partial" and whole["evidence"] == []
    assert whole["degraded_reason"] == "KNOWLEDGE_VERSION_BOUNDARY"


def test_kb01_sharing_a_future_version_keeps_private_history_private(knowledge, admin):  # noqa: F811
    store, _, retrieval = knowledge
    owner_day, shared_day = date(2026, 11, 19), date(2026, 12, 1)
    first = uploaded(knowledge, "发票用途私人条款。")
    doc = first["document_id"]
    private_citation = retrieval.search(
        AUTH, company_id="C001", query="发票用途", as_of=owner_day, document_ids=[doc],
    )["evidence"][0]
    second = uploaded(
        knowledge, "发票用途共享条款。", document_id=doc,
        expected_revision=store.get(AUTH, doc)["revision"], effective_from=shared_day,
    )
    with admin.transaction():
        publish_company(admin, doc, second["version_id"], store.get(AUTH, doc)["revision"], uid())

    colleague = Identity("U002")
    assert store.get(AUTH, doc, as_of=owner_day)["active_version_id"] == first["version_id"]
    assert store.file(AUTH, doc, first["version_id"], as_of=owner_day)["file_id"]
    assert retrieval.read(AUTH, private_citation["citation_id"])["version_id"] == first["version_id"]
    assert not retrieval.search(
        colleague, company_id="C001", query="发票用途", as_of=owner_day, document_ids=[doc],
    )["evidence"]
    with pytest.raises(AppError):
        retrieval.read(colleague, private_citation["citation_id"])
    with pytest.raises(AppError):
        store.file(colleague, doc, first["version_id"], as_of=owner_day)
    result = retrieval.search(
        colleague, company_id="C001", query="发票用途", as_of=shared_day, document_ids=[doc],
    )
    citation = result["evidence"][0]
    assert citation["version_id"] == second["version_id"]
    with admin.transaction():
        revoke_read(admin, colleague.user_id, "C001", uid())
    with pytest.raises(AppError):
        retrieval.read(colleague, citation["citation_id"])
    with pytest.raises(AppError):
        store.file(colleague, doc, second["version_id"], as_of=shared_day)


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
