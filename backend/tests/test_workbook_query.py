import json
from datetime import date
from io import BytesIO

import pytest
from openpyxl import Workbook
from test_runtime import ScriptedModel, model_settings, tool

from app.agent_profiles import AgentDraft, create_draft, publish, set_enabled
from app.errors import AppError
from app.identity import Identity, create_session, switch_company
from app.insight_access import public_run
from app.knowledge import Knowledge
from app.knowledge_jobs import KnowledgeJobs
from app.runtime import Runtime
from app.turns import accept_turn, read_turn
from app.values import jsonb, uid
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


def workbook(rows=ROWS, headers=("报销日期", "费用类别", "实付金额"), formats=None):
    book = Workbook()
    book.active.title = "报销明细"
    book.active.append(headers)
    for row in rows:
        book.active.append(row)
    for address, number_format in (formats or {}).items():
        book.active[address].number_format = number_format
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


@pytest.mark.parametrize("foreign", ["USD", None, "未知币种"])
def test_file02_uses_currency_of_each_participating_row(foreign):
    rows = (
        ("2026-10-01", "交通费", 120, "CNY"),
        ("2026-10-02", "住宿费", 10, foreign),
    )
    headers = ("报销日期", "费用类别", "实付金额", "币种")
    args = dict(
        sheet="报销明细", columns=COLUMNS, start=OCTOBER[0], end=OCTOBER[1], group_by="none"
    )
    snapshot = parse_workbook(workbook(rows, headers))
    with pytest.raises(AppError, match="D3") as problem:
        calculate(snapshot, **args, category=None)
    assert problem.value.details == {"sheet": "报销明细", "cell": "D3"}
    assert calculate(snapshot, **args, category="交通费")["total"] == "120.00"
    fixed = parse_workbook(workbook((rows[0], (*rows[1][:3], "人民币")), headers))
    assert calculate(fixed, **args, category=None)["total"] == "130.00"


@pytest.mark.parametrize(
    ("header", "number_format", "address"),
    [
        ("实付金额", '0.00"C A D"', "C2"),
        ("实付金额", '#,##0.00"CAD"', "C2"),
        ("实付金额", "[$¥-411]#,##0.00", "C2"),
        ("实付金额", "#,##0.0,", "C2"),
        ("实付金额（万元）", "General", "C1"),
    ],
)
def test_file02_validates_amount_header_and_format(header, number_format, address):
    snapshot = parse_workbook(
        workbook(ROWS, ("报销日期", "费用类别", header), {"C2": number_format})
    )
    with pytest.raises(AppError, match=address) as problem:
        calculate(
            snapshot,
            sheet="报销明细",
            columns=COLUMNS,
            start=OCTOBER[0],
            end=OCTOBER[1],
            category=None,
            group_by="none",
        )
    assert problem.value.details == {"sheet": "报销明细", "cell": address}


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
    validate_request({**query, "category": "交通费"}, snapshot, "只看交通费", query)
    with pytest.raises(AppError, match="日期范围"):
        validate_request(query, snapshot, "实付报销有多少", {})
    with pytest.raises(AppError, match="月份.*不符"):
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


@pytest.mark.parametrize(
    ("question", "wrong", "field"),
    [
        (
            "2026-10-01到2026/10/02的实付合计",
            {"date_to_exclusive": "2026-11-01"},
            "date_to_exclusive",
        ),
        ("2026年10月1日至2026年10月2日，交通费合计", {"category": "住宿费"}, "category"),
        ("2026-10-01至2026-10-02按费用类别拆开", {"group_by": "none"}, "group_by"),
    ],
)
def test_file01_rejects_unconfirmed_range_category_and_grouping(question, wrong, field):
    snapshot = parse_workbook(workbook())
    query = dict(
        sheet="报销明细",
        columns=COLUMNS,
        date_from="2026-10-01",
        date_to_exclusive="2026-10-03",
    )
    with pytest.raises(AppError) as problem:
        validate_request({**query, **wrong}, snapshot, question, {})
    assert problem.value.code == "WORKBOOK_SCOPE_MISMATCH"
    assert problem.value.details["field"] == field


def test_file01_accepts_end_date_and_protects_explicit_scope_changes():
    snapshot = parse_workbook(workbook())
    query = dict(
        sheet="报销明细",
        columns=COLUMNS,
        date_from="2026-10-01",
        date_to_exclusive="2026-10-03",
    )
    checked = validate_request(query, snapshot, "2026年10月1日到10月2日的实付合计", {})
    assert checked["date_to_exclusive"] == "2026-10-03"
    month = {**query, "date_to_exclusive": "2026-11-01"}
    for ambiguous in (
        "10月1日",
        "2026年10月或11月",
        "2026年10月，按日期分组",
        "2026年10月，用 D 列",
    ):
        with pytest.raises(AppError) as problem:
            validate_request(month, snapshot, ambiguous, {})
        assert problem.value.code == "NEEDS_CLARIFICATION"
    first = {**checked, "category": "交通费", "group_by": "category"}
    all_rows = {**query, "all_dates": True, "date_from": None, "date_to_exclusive": None}
    checked = validate_request(all_rows, snapshot, "全表所有类别，只要总计", first)
    assert checked["category"] is None and checked["group_by"] == "none" and checked["all_dates"]
    with pytest.raises(AppError, match="日期范围"):
        validate_request(all_rows, snapshot, "换成2026年11月", checked)


@pytest.fixture
def excel_turn(db, settings):
    auth = Identity("U001")
    session = str(create_session(db, auth, "C001")["session_id"])

    def attach(content):
        file = Knowledge(db, settings).upload(
            auth,
            name="报销.xlsx",
            title="报销明细",
            company_id="C001",
            content=content,
            request_id=uid(),
        )
        jobs = KnowledgeJobs(db, settings, None)
        jobs.run(jobs.claim())
        args = dict(
            document_id=file["document_id"],
            version_id=file["version_id"],
            sheet="报销明细",
            columns=COLUMNS,
            date_from="2026-10-01",
            date_to_exclusive="2026-11-01",
        )

        async def query(text, *, attached=True, changes=None, reply=_sheet_reply):
            runtime = Runtime(
                db,
                model_settings(settings),
                ScriptedModel(
                    [
                        tool("query_workbook", {**args, **(changes or {})}),
                        reply,
                    ]
                ),
            )
            turn = accept_turn(
                db,
                auth,
                session,
                uid(),
                text,
                knowledge_upload_ids=[file["file_id"]] if attached else [],
            )
            await runtime.run(runtime.claim())
            return read_turn(db, auth, turn["turn_id"])

        return file, query

    return auth, attach


def _tool_denied(code, **details):
    def reply(payload):
        output = json.loads(payload["messages"][-1]["content"])
        assert output["ok"] is False and output["error"]["code"] == code, output
        assert output["error"]["details"] == details
        plan = {"blocks": [{"type": "explanation", "code": "workbook_query_failed"}]}
        if code == "NEEDS_CLARIFICATION":
            plan = {
                "status": "waiting_user",
                "blocks": [],
                "clarification": {"intent": "other", "missing_fields": ["target"]},
            }
        return {"role": "assistant", "content": json.dumps(plan)}

    return reply


def _result_card(turn):
    return next(card for card in turn["final_cards"] if card["kind"] == "workbook_result")


@pytest.mark.asyncio
async def test_file01_runtime_returns_only_verified_scope_and_original_rows(db, excel_turn):
    auth, attach = excel_turn
    _, query = attach(
        workbook(
            (
                ("2026-10-01", "交通费", 12.30),
                ("2026-10-02", "住宿费", 45.12),
                ("2026-11-02", "交通费", 123.40),
            )
        )
    )
    for text, changes, field, expected, actual in (
        (
            "2026年10月实付合计",
            {"date_to_exclusive": "2026-10-03"},
            "date_to_exclusive",
            "2026-11-01",
            "2026-10-03",
        ),
        ("2026年10月只看交通费", {"category": "住宿费"}, "category", "交通费", "住宿费"),
        ("2026年10月按费用类别拆开", {}, "group_by", "category", "none"),
    ):
        denied = await query(
            text,
            changes=changes,
            reply=_tool_denied(
                "WORKBOOK_SCOPE_MISMATCH",
                field=field,
                expected=expected,
                actual=actual,
            ),
        )
        assert denied["status"] == "completed", denied
        assert all(card["kind"] != "workbook_result" for card in denied["final_cards"])
    with db.transaction(readonly=True) as conn:
        assert conn.execute("SELECT count(*) AS n FROM workbook_results").fetchone()["n"] == 0

    need_date = await query(
        "请按费用类别拆开，计算实付金额",
        changes={"group_by": "category"},
        reply=_tool_denied("NEEDS_CLARIFICATION"),
    )
    assert need_date["status"] == "waiting_user", need_date
    assert "请先提供日期范围" in need_date["final_message"]
    clarified = await query("2026年10月", attached=False, changes={"group_by": "category"})
    with db.transaction(readonly=True) as conn:
        page = read_result(conn, auth, _result_card(clarified)["result_id"])
    assert page["total"] == "57.42" and page["query"]["group_by"] == "category"

    for text, attached, changes, total, numbers in (
        (
            "2026-10-01至2026年10月2日实付合计",
            True,
            {"date_to_exclusive": "2026-10-03"},
            "57.42",
            [2, 3],
        ),
        ("2026年10月按费用类别拆开", True, {"group_by": "category"}, "57.42", [2, 3]),
        ("只看交通费", False, {"group_by": "category", "category": "交通费"}, "12.30", [2]),
        (
            "只看十一月",
            False,
            {
                "date_from": "2026-11-01",
                "date_to_exclusive": "2026-12-01",
                "group_by": "category",
                "category": "交通费",
            },
            "123.40",
            [4],
        ),
    ):
        answered = await query(text, attached=attached, changes=changes)
        with db.transaction(readonly=True) as conn:
            page = read_result(conn, auth, _result_card(answered)["result_id"])
        assert page["total"] == total and page["query"]["columns"]["amount"] == "C"
        assert [row[0] for row in page["rows"]] == numbers
        if numbers == [2, 3] and changes.get("group_by"):
            assert {g["category"]: g["total"] for g in page["groups"]} == {
                "交通费": "12.30",
                "住宿费": "45.12",
            }


@pytest.mark.asyncio
async def test_file02_runtime_shows_original_cell_for_mixed_currency(db, excel_turn):
    _, attach = excel_turn
    _, query = attach(
        workbook(
            (("2026-10-01", "交通费", 120, "CNY"), ("2026-10-02", "住宿费", 10, "USD")),
            ("报销日期", "费用类别", "实付金额", "币种"),
        )
    )
    answer = await query(
        "2026年10月实付报销合计",
        reply=_tool_denied("WORKBOOK_UNIT_UNSUPPORTED", sheet="报销明细", cell="D3"),
    )
    assert answer["status"] == "completed", answer
    assert "D3" in answer["final_message"] and "130.00" not in answer["final_message"]
    assert not answer["final_cards"]
    with db.transaction(readonly=True) as conn:
        assert conn.execute("SELECT count(*) AS n FROM workbook_results").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_file02_old_snapshots_use_original_bytes_and_old_answers_require_new_query(
    db,
    admin,
    excel_turn,
):
    auth, attach = excel_turn
    file, query = attach(workbook())
    old_turn = await query("2026年10月实付报销合计")
    old_result = _result_card(old_turn)["result_id"]
    admin.execute(
        "UPDATE workbook_results SET result=result-'parser_version' WHERE result_id=%s",
        (old_result,),
    )
    projected = read_turn(db, auth, old_turn["turn_id"])
    assert all(card["kind"] != "workbook_result" for card in projected["final_cards"])
    run = public_run(db, auth, old_turn["runs"][0]["run_id"])
    assert run["contexts"] and all(item["payload"].get("redacted") for item in run["contexts"])
    with db.transaction(readonly=True) as conn, pytest.raises(AppError, match="重新"):
        read_result(conn, auth, old_result)
    with db.transaction(readonly=True) as conn:
        old_snapshot = conn.execute(
            "SELECT snapshot FROM workbook_snapshots WHERE version_id=%s", (file["version_id"],)
        ).fetchone()["snapshot"]
    old_snapshot["parser_version"] = "workbook-1"
    for sheet in old_snapshot["sheets"]:
        sheet.pop("currency_columns", None)
        sheet.pop("amount_units", None)
        sheet["rows"] = [row[:2] for row in sheet["rows"]]
    admin.execute(
        "UPDATE workbook_snapshots SET snapshot=%s WHERE version_id=%s",
        (jsonb(old_snapshot), file["version_id"]),
    )
    admin.execute(
        "UPDATE knowledge_versions SET parser_version='workbook-1' WHERE version_id=%s",
        (file["version_id"],),
    )

    reparsed = await query("2026年10月实付报销合计")
    with db.transaction(readonly=True) as conn:
        page = read_result(conn, auth, _result_card(reparsed)["result_id"])
        assert page["currency"] == "CNY" and page["total"] == "617.00"
        assert page["parser_version"] == "workbook-2"
    changed = workbook(ROWS[:1])
    admin.execute(
        "UPDATE knowledge_files SET file_bytes=%s,byte_size=%s WHERE file_id=%s",
        (changed, len(changed), file["file_id"]),
    )
    denied = await query("2026年10月实付报销合计", reply=_tool_denied("WORKBOOK_RECHECK_REQUIRED"))
    assert denied["status"] == "completed", denied
    assert all(card["kind"] != "workbook_result" for card in denied["final_cards"])


@pytest.mark.asyncio
async def test_kb01_workbook_version_day_is_distinct_from_row_filter(db, settings, excel_turn, monkeypatch):
    from app import business_dates

    auth, attach = excel_turn
    today = date(2026, 11, 19)
    monkeypatch.setattr(business_dates, "business_today", lambda _at=None: today)
    old, query = attach(workbook())
    store = Knowledge(db, settings)
    document = store.get(auth, old["document_id"])
    changed = (*ROWS[:2], ("2026-10-08", "交通费", 179.90), *ROWS[3:])
    newest = store.upload(
        auth, name="新版.xlsx", title="报销明细", company_id="C001", content=workbook(changed),
        request_id=uid(), document_id=old["document_id"], expected_revision=document["revision"],
        effective_from=date(2026, 12, 1),
    )
    jobs = KnowledgeJobs(db, settings, None)
    jobs.run(jobs.claim())
    assert store.get(auth, old["document_id"])["active_version_id"] == old["version_id"]
    first = await query("2026年10月实付报销合计")
    old_result = _result_card(first)["result_id"]
    today = date(2026, 12, 1)
    assert store.get(auth, old["document_id"])["active_version_id"] == newest["version_id"]
    assert str(store.file(auth, old["document_id"])["file_id"]) == newest["file_id"]
    with db.transaction(readonly=True) as conn:
        saved = read_result(conn, auth, old_result)
        assert saved["version_timing"]["as_of"] == "2026-11-19"
        assert saved["version_timing"]["applicability"] == "historical"
        assert saved["total"] == "617.00"
    historical = await query(
        "使用工作簿版本日期为2026/09/20，统计2026年10月实付金额",
        changes={"version_as_of": "2026-09-20"},
    )
    future = await query(
        "按2026年12月1日有效的工作簿版本，统计2026年10月实付金额",
        changes={"version_as_of": "2026-12-01", "version_id": newest["version_id"]},
    )
    for turn, version, day, total in (
        (historical, old["version_id"], "2026-09-20", "617.00"),
        (future, newest["version_id"], "2026-12-01", "717.00"),
    ):
        with db.transaction(readonly=True) as conn:
            result = read_result(conn, auth, _result_card(turn)["result_id"])
        assert result["version_id"] == version
        assert result["version_timing"]["as_of"] == day
        assert result["query"]["date_from"] == "2026-10-01"
        assert result["total"] == total


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
    reused = store.upload(auth, **{**parameters, "request_id": uid(), "name": "同内容.xlsx"})
    assert reused["duplicate"] == {"title": "报销明细", "state": "ready", "usable": True}
    assert reused["version_id"] == attached["version_id"]
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
    failed_version = store.upload(
        auth,
        **{
            **parameters,
            "request_id": uid(),
            "content": workbook(headers=("日期", "类别", "未知列")),
        },
        document_id=attached["document_id"],
        expected_revision=doc["revision"],
    )
    jobs.run(jobs.claim())
    after_failure = store.get(auth, attached["document_id"])
    assert after_failure["active_version_id"] == attached["version_id"]
    assert after_failure["versions"][0]["version_id"] == failed_version["version_id"]
    assert after_failure["versions"][0]["state"] == "failed"
    with db.transaction(readonly=True) as conn:
        assert read_result(conn, auth, first_id)["total"] == "617.00"
    adjusted = (*ROWS[:2], ("2026-10-08", "交通费", 179.90), *ROWS[3:])
    new_version = store.upload(
        auth,
        **{**parameters, "request_id": uid(), "content": workbook(adjusted)},
        document_id=attached["document_id"],
        expected_revision=after_failure["revision"],
    )
    jobs.run(jobs.claim())
    updated = store.get(auth, attached["document_id"])
    assert updated["active_version_id"] == new_version["version_id"]
    with db.transaction(readonly=True) as conn, pytest.raises(AppError, match="换版"):
        read_result(conn, auth, first_id)
    run = public_run(db, auth, execution.run_id)
    assert all(item["payload"].get("redacted") for item in run["contexts"])

    with pytest.raises(AppError, match="换版"):
        accept_turn(
            db,
            auth,
            session,
            uid(),
            "继续查这个表",
            knowledge_upload_ids=[attached["file_id"]],
            agent_id=agent["agent_id"],
        )

    def stale_reply(payload):
        state = json.loads(payload["messages"][0]["content"].split("\n正式状态：\n", 1)[1])
        assert state["workbook_followup_unavailable"]
        assert "workbook_candidates" not in state
        assert "query_workbook" not in {
            tool["function"]["name"] for tool in payload.get("tools", [])
        }
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "blocks": [{"type": "chat_answer", "text": "请从知识资料重新选择有效 Excel。"}],
                }
            ),
        }

    new_model = ScriptedModel(
        [
            stale_reply,
            tool("query_workbook", {**args, "version_id": new_version["version_id"]}),
            _sheet_reply,
        ]
    )
    runtime = Runtime(db, model_settings(settings), new_model, agent_runtime_enabled=True)
    stale = accept_turn(db, auth, session, uid(), "继续只看交通费", agent_id=agent["agent_id"])
    await runtime.run(runtime.claim())
    assert "重新选择有效" in read_turn(db, auth, stale["turn_id"])["final_message"]
    current = accept_turn(
        db,
        auth,
        session,
        uid(),
        "2026年10月实付报销合计多少，按费用类别拆开",
        knowledge_upload_ids=[new_version["file_id"]],
        agent_id=agent["agent_id"],
    )
    await runtime.run(runtime.claim())
    returned = read_turn(db, auth, current["turn_id"])
    result_id = next(
        card["result_id"] for card in returned["final_cards"] if card["kind"] == "workbook_result"
    )
    with db.transaction(readonly=True) as conn:
        assert read_result(conn, auth, result_id)["total"] == "717.00"

    for company, next_agent in (("C002", agent["agent_id"]), ("C001", None)):
        scoped_session = str(create_session(db, auth, "C001")["session_id"])
        scope_model = ScriptedModel(
            [
                tool(
                    "query_workbook",
                    {**args, "version_id": new_version["version_id"], "group_by": "none"},
                ),
                _sheet_reply,
                stale_reply,
            ]
        )
        runtime = Runtime(db, model_settings(settings), scope_model, agent_runtime_enabled=True)
        initial = accept_turn(
            db,
            auth,
            scoped_session,
            uid(),
            "2026年10月实付报销合计",
            knowledge_upload_ids=[new_version["file_id"]],
            agent_id=agent["agent_id"],
        )
        await runtime.run(runtime.claim())
        assert read_turn(db, auth, initial["turn_id"])["status"] == "completed"
        if company != "C001":
            switch_company(db, auth, scoped_session, company)
        followup = accept_turn(
            db,
            auth,
            scoped_session,
            uid(),
            "继续只看交通费",
            agent_id=next_agent,
        )
        await runtime.run(runtime.claim())
        answer = read_turn(db, auth, followup["turn_id"])
        assert "重新选择有效" in answer["final_message"]
        assert not any(card["kind"] == "workbook_result" for card in answer["final_cards"])

    store.change(
        auth,
        attached["document_id"],
        action="disable",
        expected_revision=updated["revision"],
        request_id=uid(),
    )
    with pytest.raises(AppError):
        store.file(auth, attached["document_id"], new_version["version_id"])
    with db.transaction(readonly=True) as conn, pytest.raises(AppError, match="停用"):
        read_result(conn, auth, result_id)
