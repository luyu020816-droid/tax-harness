import json
from datetime import date
from io import BytesIO

import pytest
from openpyxl import Workbook
from test_runtime import ScriptedModel, model_settings, tool

from app.agent_profiles import AgentDraft, create_draft, publish, set_enabled
from app.errors import AppError
from app.identity import Identity, create_session
from app.insight_access import public_run
from app.knowledge import Knowledge
from app.knowledge_jobs import KnowledgeJobs
from app.runtime import Runtime
from app.turns import accept_turn, read_turn
from app.values import uid
from app.workbook_queries import read_result, validate_request
from app.xlsx_workbooks import calculate, parse_workbook

OCTOBER = date(2026, 10, 1), date(2026, 11, 1)
COLUMNS = {"date": "A", "category": "B", "amount": "C"}
ROWS = (
    ("2026-10-03", "交通费", 120.10),
    ("2026-10-05", "住宿费", 437),
    ("2026-10-08", "交通费", 79.90),
    ("2026-10-20", "交通费", -20),
    ("2026-11-01", "交通费", 88.50),
    ("2026-11-15", "住宿费", 300),
)


def workbook(rows=ROWS, headers=("报销日期", "费用类别", "实付金额")):
    book = Workbook()
    book.active.title = "报销明细"
    book.active.append(headers)
    for row in rows:
        book.active.append(row)
    data = BytesIO()
    book.save(data)
    book.close()
    return data.getvalue()


def test_workbook_totals_preserve_row_locations():
    result = calculate(
        parse_workbook(workbook()),
        sheet="报销明细",
        columns=COLUMNS,
        start=OCTOBER[0],
        end=OCTOBER[1],
        category=None,
        group_by="category",
    )
    assert (result["total"], result["count"]) == ("617.00", 4)
    assert {group["category"]: group["total"] for group in result["groups"]} == {
        "交通费": "180.00",
        "住宿费": "437.00",
    }
    assert [row[0] for row in result["rows"] if row[2] == "交通费"] == [2, 4, 5]


def test_decimal_sum_with_reordered_aliases_and_empty_range():
    content = workbook(
        (("交通费", "0.10", "2026/10/03"), ("住宿费", 0.20, date(2026, 10, 5))),
        ("费用类型", "实际支付金额", "支付日期"),
    )
    snapshot = parse_workbook(content)
    args = dict(
        sheet="报销明细",
        columns={"date": "C", "category": "A", "amount": "B"},
        category=None,
        group_by="none",
    )
    result = calculate(snapshot, **args, start=OCTOBER[0], end=OCTOBER[1])
    assert (result["total"], result["count"]) == ("0.30", 2)
    result = calculate(snapshot, **args, start=date(2026, 12, 1), end=date(2027, 1, 1))
    assert (result["total"], result["count"]) == ("0.00", 0)


@pytest.mark.parametrize("invalid", [None, "not money", "=1+2", "1.0001", "USD 1"])
def test_invalid_target_amount_fails_instead_of_summing_partial_rows(invalid):
    snapshot = parse_workbook(workbook((("2026-10-03", "交通费", invalid), *ROWS)))
    with pytest.raises(AppError, match="C2"):
        calculate(
            snapshot,
            sheet="报销明细",
            columns=COLUMNS,
            start=OCTOBER[0],
            end=OCTOBER[1],
            category=None,
            group_by="none",
        )


def _sheet_reply(payload):
    output = json.loads(payload["messages"][-1]["content"])
    assert output.get("ok"), output
    return {
        "role": "assistant",
        "content": json.dumps(
            {
                "blocks": [
                    {
                        "type": "result",
                        "view": "workbook_result",
                        "result_ref": output["result_ref"],
                    }
                ],
            }
        ),
    }


def test_workbook_scope_requires_user_evidence():
    snapshot = parse_workbook(workbook())
    query = dict(
        sheet="报销明细", columns=COLUMNS, date_from="2026-10-01", date_to_exclusive="2026-11-01"
    )
    validate_request(query, snapshot, "2026年10月实付报销", {})
    validate_request(query, snapshot, "只看交通费", query)
    with pytest.raises(AppError, match="日期范围"):
        validate_request(query, snapshot, "实付报销有多少", {})
    with pytest.raises(AppError, match="月份不符"):
        validate_request(query, snapshot, "2026年11月实付报销", {})
    with pytest.raises(AppError, match="年份"):
        validate_request(query, snapshot, "10月实付报销", {})
    snapshot["sheets"][0]["columns"]["amount"]["D"] = "实际支付金额"
    with pytest.raises(AppError, match="哪一列"):
        validate_request(query, snapshot, "2026年10月实付报销", {})
    validate_request(query, snapshot, "2026年10月实付报销，用 C 列", {})
    snapshot["sheets"].append({**snapshot["sheets"][0], "name": "另一张表"})
    with pytest.raises(AppError, match="哪一个工作表"):
        validate_request(query, snapshot, "2026年10月实付报销，用 C 列", {})


@pytest.mark.asyncio
async def test_workbook_upload_agent_followup_and_evidence_revocation(db, settings):
    auth = Identity("U001")
    store = Knowledge(db, settings)
    parameters = dict(
        name="报销.xlsx", title="报销明细", company_id="C001", content=workbook(), request_id=uid()
    )
    attached = store.upload(auth, **parameters)
    assert store.upload(auth, **parameters) == {**attached, "replayed": True}
    jobs = KnowledgeJobs(db, settings, None)
    jobs.run(jobs.claim())
    doc = store.get(auth, attached["document_id"])
    assert doc["active_version_id"] == attached["version_id"], doc["versions"]
    agent = create_draft(
        db,
        auth,
        AgentDraft(
            name="表格 Agent",
            model_profile_id="default",
            knowledge_mode="selected",
            knowledge_document_ids=[attached["document_id"]],
            tool_grants={"inspect_workbook": ["invoke"], "query_workbook": ["invoke"]},
        ),
    )
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    session = str(create_session(db, auth, "C001")["session_id"])
    args = dict(
        document_id=attached["document_id"],
        version_id=attached["version_id"],
        sheet="报销明细",
        columns=COLUMNS,
        date_from="2026-10-01",
        date_to_exclusive="2026-11-01",
        group_by="category",
    )
    model = ScriptedModel(
        [
            tool("inspect_workbook", {"document_id": attached["document_id"]}),
            tool("query_workbook", args),
            _sheet_reply,
            tool(
                "query_workbook",
                {
                    **args,
                    "date_from": "2026-11-01",
                    "date_to_exclusive": "2026-12-01",
                    "category": "交通费",
                },
            ),
            _sheet_reply,
        ]
    )
    runtime = Runtime(db, model_settings(settings), model, agent_runtime_enabled=True)
    first_id = None
    for text, files, expected, rows in [
        (
            "2026年10月实付报销合计多少，按费用类别拆开",
            [attached["file_id"]],
            "617.00",
            [2, 3, 4, 5],
        ),
        ("改成11月，只看交通费", [], "88.50", [6]),
    ]:
        turn = accept_turn(
            db, auth, session, uid(), text, knowledge_upload_ids=files, agent_id=agent["agent_id"]
        )
        execution = runtime.claim()
        await runtime.run(execution)
        current = read_turn(db, auth, turn["turn_id"])
        assert current["status"] == "completed", current
        result_id = next(
            card["result_id"]
            for card in current["final_cards"]
            if card["kind"] == "workbook_result"
        )
        with db.transaction(readonly=True) as conn:
            page = read_result(conn, auth, result_id)
            assert page["total"] == expected
            assert [item[0] for item in page["rows"]] == rows
            with pytest.raises(AppError):
                read_result(conn, Identity("U002"), result_id)
        first_id = first_id or result_id
    new_version = store.upload(
        auth,
        **{**parameters, "request_id": uid(), "content": workbook(ROWS[:2])},
        document_id=attached["document_id"],
        expected_revision=doc["revision"],
    )
    jobs.run(jobs.claim())
    updated = store.get(auth, attached["document_id"])
    assert updated["active_version_id"] == new_version["version_id"]
    with db.transaction(readonly=True) as conn, pytest.raises(AppError, match="换版"):
        read_result(conn, auth, first_id)
    run = public_run(db, auth, execution.run_id)
    assert all(item["payload"].get("redacted") for item in run["contexts"])
    store.change(
        auth,
        attached["document_id"],
        action="disable",
        expected_revision=updated["revision"],
        request_id=uid(),
    )
    with pytest.raises(AppError):
        store.file(auth, attached["document_id"], new_version["version_id"])
