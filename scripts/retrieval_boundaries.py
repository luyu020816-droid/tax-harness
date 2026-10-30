"""Live permission/version/budget probes in the isolated retrieval holdout DB."""

from datetime import date

import psycopg
from app.errors import AppError
from app.identity import Identity
from app.knowledge_admin import publish_company, revoke_read
from app.values import uid
from psycopg.rows import dict_row


def run_boundaries(settings, store, jobs, retrieval, documents, fixture_hash):
    results = []
    owner, other = Identity("U001"), Identity("U002")

    # All uploads are synthetic historical corpus fixtures. Spreading file
    # creation times avoids turning a quality test into a 20-upload/hour test.
    # Runtime quota code is unchanged; its actual behavior has separate tests.
    def historical():
        with psycopg.connect(settings.admin_database_url) as conn:
            conn.execute("UPDATE knowledge_files SET created_at='2026-09-01'::timestamptz")

    def upload(text, *, document=None, company="C001", **kwargs):
        historical()
        result = store.upload(
            owner,
            name="边界.txt",
            content=text.encode(),
            company_id=company,
            title="独立边界资料",
            request_id=uid(),
            document_id=document["document_id"] if document else None,
            expected_revision=store.get(owner, document["document_id"])["revision"]
            if document
            else None,
            **kwargs,
        )
        jobs.run(jobs.claim())
        assert store.get(owner, result["document_id"])["active_version_id"] == result["version_id"]
        return result

    def denied(callback):
        try:
            callback()
        except AppError as error:
            return error.code
        return None

    def search(doc, query="独立边界资料", **kwargs):
        return retrieval.search(
            owner, company_id="C001", query=query, document_ids=[doc["document_id"]], **kwargs
        )

    first = documents["H01"]
    private = retrieval.search(
        other, company_id="C001", query="档案借阅的条件", document_ids=[first["document_id"]]
    )
    results.append(
        {
            "id": "HB01",
            "kind": "private_user",
            "pass": not private["evidence"]
            and denied(lambda: store.get(other, first["document_id"])) is not None,
            "query_id": private["query_id"],
        }
    )
    error = denied(lambda: retrieval.search(other, company_id="C002", query="查询资料"))
    results.append(
        {"id": "HB02", "kind": "wrong_company", "pass": error == "ACCESS_DENIED", "error": error}
    )
    foreign = upload("乙企业的独立库存资料，F003必须经仓库审核。", company="C002")
    scoped = search(foreign, "F003库存审核")
    results.append(
        {
            "id": "HB03",
            "kind": "foreign_document",
            "pass": not scoped["evidence"],
            "query_id": scoped["query_id"],
        }
    )
    wanted = upload("F002的附件归档须财务负责人批准；F002原始记录不得省略。")
    wrong = upload("F003的附件归档须仓库保管员批准；F003原始记录不得省略。")
    constrained = retrieval.search(
        owner,
        company_id="C001",
        query="F002附件归档由谁批准？",
        entities=["F002"],
        document_ids=[wanted["document_id"], wrong["document_id"]],
    )
    results.append(
        {
            "id": "HB04",
            "kind": "exact_entity",
            "pass": bool(constrained["evidence"])
            and all(e["document_id"] == wanted["document_id"] for e in constrained["evidence"]),
            "query_id": constrained["query_id"],
        }
    )
    initial = search(wanted, "F002附件归档由谁批准？")
    assert initial["evidence"]
    citation = initial["evidence"][0]["citation_id"]
    doc = store.get(owner, wanted["document_id"])
    store.change(
        owner,
        wanted["document_id"],
        action="disable",
        expected_revision=doc["revision"],
        request_id=uid(),
    )
    disabled = search(wanted, "F002附件归档")
    results.append(
        {
            "id": "HB05",
            "kind": "disabled_document",
            "pass": not disabled["evidence"],
            "query_id": disabled["query_id"],
        }
    )
    error = denied(lambda: retrieval.read(owner, citation))
    results.append(
        {
            "id": "HB06",
            "kind": "stale_citation",
            "pass": error == "SOURCE_INVALIDATED",
            "error": error,
        }
    )
    old = upload("备用服务器借用必须由信息技术负责人批准，普通员工不得自行取用。")
    before = search(old, "借用备用服务器要谁批准？")
    assert before["evidence"]
    new = upload("备用服务器借用必须由安全负责人批准，新制度已替代原批准岗位。", document=old)
    current = search(new, "借用备用服务器要谁批准？")
    stale = denied(lambda: retrieval.read(owner, before["evidence"][0]["citation_id"]))
    results.append(
        {
            "id": "HB07",
            "kind": "superseded_version",
            "pass": bool(current["evidence"])
            and all(e["version_id"] == new["version_id"] for e in current["evidence"])
            and stale == "SOURCE_INVALIDATED",
            "query_id": current["query_id"],
        }
    )
    with psycopg.connect(settings.admin_database_url, row_factory=dict_row) as conn:
        publish_company(
            conn,
            old["document_id"],
            new["version_id"],
            store.get(owner, old["document_id"])["revision"],
            uid(),
        )
    shared = retrieval.search(
        other,
        company_id="C001",
        query="借用备用服务器要谁批准？",
        document_ids=[old["document_id"]],
    )
    assert shared["evidence"]
    with psycopg.connect(settings.admin_database_url, row_factory=dict_row) as conn:
        revoke_read(conn, "U002", "C001", uid())
    error = denied(lambda: retrieval.read(other, shared["evidence"][0]["citation_id"]))
    results.append(
        {"id": "HB08", "kind": "revoked_access", "pass": error is not None, "error": error}
    )
    timed = upload(
        "合同附件的签署必须由法务负责人审查，不得跳过审核。",
        effective_from=date(2026, 9, 15),
        effective_to=date(2026, 10, 1),
    )
    month = search(timed, "合同附件由谁审查？", period="2026-09")
    point = search(timed, "合同附件由谁审查？", as_of=date(2026, 9, 20))
    results.append(
        {
            "id": "HB09",
            "kind": "whole_month",
            "pass": not month["evidence"] and bool(point["evidence"]),
            "query_id": month["query_id"],
        }
    )
    huge = upload(
        "库房备用钥匙临时借用期限为两小时，须值班负责人批准。\n\n"
        + ("本流程记录应保留登记时间、交接对象、核对人员和返回状态。记录不改变批准要求。" * 120)
        + "\n\n例外：应急处置仍须批准，钥匙用后必须立即归还。"
    )
    budget = search(huge, "库房备用钥匙临时借用期限与批准条件是什么？")
    results.append(
        {
            "id": "HB10",
            "kind": "budget",
            "pass": budget["status"] == "partial"
            and budget["degraded_reason"] == "EVIDENCE_BUDGET_INSUFFICIENT"
            and not budget["evidence"],
            "query_id": budget["query_id"],
        }
    )
    return {
        "fixture_sha256": fixture_hash,
        "cases": results,
        "passed": sum(r["pass"] for r in results),
        "total": len(results),
        "source_metadata_note": "Synthetic historical file timestamps in this isolated DB; no production quota bypass.",
    }
