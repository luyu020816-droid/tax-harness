import json

import pytest
from test_answer_renderer import recorded, rendered
from test_judge_boundaries import ExactModel, raw_final
from test_runtime import model_settings, tool

from app import context
from app.errors import AppError
from app.identity import Identity, create_session
from app.request_checklist import compile_items, verify_items
from app.runtime import Runtime
from app.sources import read_source
from app.tool_schemas import Clarification
from app.turns import accept_turn, read_turn
from app.values import uid

COMPANIES = [
    {"company_id": "C001", "aliases": ["甲企业"]},
    {"company_id": "C002", "aliases": ["乙企业"]},
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,targets",
    [
        (
            "查询甲企业2026年8月发票总额；查询乙企业2026年9月发票总额",
            (("C001", "2026-08"), ("C002", "2026-09")),
        ),
        (
            "查询乙企业2026年8月发票总额，并查询2026年9月发票总额",
            (("C002", "2026-08"), ("C002", "2026-09")),
        ),
    ],
)
async def test_real_multicompany_source_keeps_scopes_isolated(db, settings, text, targets):
    auth = Identity("U001")
    session = create_session(db, auth, "C001")
    turn = accept_turn(
        db,
        auth,
        str(session["session_id"]),
        uid(),
        text,
    )

    def queries(payload):
        state = json.loads(payload["messages"][0]["content"].split("\n正式状态：\n")[1])
        calls = []
        for company, period in targets:
            scope = next(
                s
                for s in state["request_frame"]["available_scope_refs"]
                if s["company_id"] == company and s["period"] == period
            )
            calls.extend(
                tool(
                    "query_month",
                    {"company_id": company, "period": period, "scope_ref": scope["scope_ref"]},
                )["tool_calls"]
            )
        return {"role": "assistant", "content": None, "tool_calls": calls}

    def final(payload):
        outputs = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
        return raw_final(
            [
                {"type": "result", "result_ref": o["result_ref"], "view": "monthly_summary"}
                for o in outputs
                if o.get("ok")
            ]
        )

    runtime = Runtime(db, model_settings(settings), ExactModel([queries, final]))
    ex = runtime.claim()
    with db.transaction(readonly=True) as conn:
        scopes = conn.execute(
            "SELECT company_id FROM source_scopes WHERE source_id=%s", (ex.source_id,)
        ).fetchall()
        assert {s["company_id"] for s in scopes} == {company for company, _ in targets}
        # General evidence reads retain the original single-company boundary.
        with pytest.raises(AppError):
            read_source(conn, auth, ex.source_id, company_id="C001")
    await runtime.run(ex)
    result = read_turn(db, auth, turn["turn_id"])
    assert result["status"] == "completed", result["error"]
    assert {
        (c["company_id"], c["period"])
        for c in result["final_cards"]
        if c["kind"] == "monthly_summary"
    } == set(targets)


def compile_text(text):
    return compile_items(text, COMPANIES, "C001", [])


@pytest.mark.parametrize(
    "text,company,available",
    [
        ("查询月度发票总额", None, False),
        ("查询甲企业月度发票总额", "C001", False),
        ("查询甲企业2026年9月发票总额", "C001", True),
    ],
)
def test_monthly_tools_require_an_evidenced_scope(
    db, settings, execution_factory, text, company, available
):
    ex = execution_factory(text, company=company)
    payload, _, _ = context.assemble(db, model_settings(settings), ex, [])
    names = {d["function"]["name"] for d in payload["tools"]}
    monthly = {"query_month", "get_rules", "check_purposes", "resolve_scope"}
    assert monthly <= names if available else not monthly & names
    assert "search_conversations" in names


def test_explicit_months_and_following_rule_request_are_separate_obligations():
    items = compile_text("查询甲企业2026年8月和9月发票总额，并解释适用规则")
    assert [(i["tool"], i["company_id"], i["period"]) for i in items] == [
        (t, "C001", p) for t in ("query_month", "get_rules") for p in ("2026-08", "2026-09")
    ]


def test_paired_companies_and_months_are_not_cross_joined():
    assert compile_text("分别查询甲企业2026年8月和乙企业2026年9月发票总额") == []
    items = compile_text("查询甲企业2026年8月发票总额；查询乙企业2026年9月发票总额")
    assert [(i["company_id"], i["period"]) for i in items] == [
        ("C001", "2026-08"),
        ("C002", "2026-09"),
    ]


def test_continuation_month_keeps_named_company_instead_of_ui_selection():
    items = compile_text("查询乙企业2026年8月发票总额，并查询2026年9月发票总额")
    assert [(i["company_id"], i["period"]) for i in items] == [
        ("C002", "2026-08"),
        ("C002", "2026-09"),
    ]
    with pytest.raises(AppError):
        verify_items(
            items,
            [
                {"kind": "monthly_summary", "company_id": "C002", "period": "2026-08"},
                {"kind": "monthly_summary", "company_id": "C001", "period": "2026-09"},
            ],
            None,
            [],
        )


def test_explicit_company_overrides_previous_company():
    items = compile_text("查询乙企业2026年8月发票总额，并查询甲企业2026年9月发票总额")
    assert [(i["company_id"], i["period"]) for i in items] == [
        ("C002", "2026-08"),
        ("C001", "2026-09"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "不要查询甲企业2026年9月发票总额",
        "例如查询甲企业2026年9月发票总额",
        "以前说过查询甲企业2026年9月发票总额",
        "如果查询甲企业2026年9月发票总额会怎样",
        "请引用原话：查询甲企业2026年9月发票总额",
        "仅记录：“查询甲企业2026年9月发票总额”",
        "把甲企业F002用途设为办公用品",
        "列出甲企业2026年9月发票",
    ],
)
def test_non_requests_do_not_become_business_obligations(text):
    assert compile_text(text) == []


def test_correct_result_does_not_cover_an_unexecuted_month():
    items = compile_text("查询甲企业2026年8月和9月发票总额")
    with pytest.raises(AppError) as error:
        verify_items(
            items,
            [{"kind": "monthly_summary", "company_id": "C001", "period": "2026-09"}],
            None,
            [],
        )
    assert error.value.details["request_items"][0]["period"] == "2026-08"


def test_unrelated_failure_and_unnecessary_clarification_cannot_hide_missing_work():
    items = compile_text("查询甲企业2026年9月发票总额")
    clarification = Clarification(
        intent="query_month", company_id="C001", missing_fields=["period"]
    )
    with pytest.raises(AppError):
        verify_items(
            items,
            [],
            clarification,
            [
                {
                    "name": "query_month",
                    "arguments": {"company_id": "C002", "period": "2026-09"},
                    "error": {"message": "没有权限"},
                }
            ],
        )


def test_missing_scope_can_wait_but_does_not_claim_verified():
    items = compile_text("查询甲企业月度发票总额")
    clarification = Clarification(
        intent="query_month", company_id="C001", missing_fields=["period"]
    )
    assert verify_items(items, [], clarification, [])[0]["status"] == "waiting_user"


def test_explicit_terminal_failure_is_reported_as_blocked():
    items = compile_text("检查甲企业2026年8月发票用途")
    result = verify_items(
        items,
        [],
        None,
        [
            {
                "name": "check_purposes",
                "arguments": {"company_id": "C001", "period": "2026-08"},
                "error": {"message": "仅支持九月"},
            }
        ],
    )
    assert result[0]["status"] == "blocked"
    assert result[0]["reason"] == "仅支持九月"


def test_shared_missing_scope_requires_only_one_question():
    items = compile_text("查询甲企业发票总额，并解释适用规则")
    clarification = Clarification(
        intent="query_month", company_id="C001", missing_fields=["period"]
    )
    assert [i["status"] for i in verify_items(items, [], clarification, [])] == [
        "waiting_user",
        "waiting_user",
    ]


@pytest.mark.parametrize(
    "text", ["检查甲企业F002的用途保存状态", "查询甲企业2026年9月第一张发票的用途"]
)
def test_single_invoice_check_does_not_force_a_monthly_check(text):
    assert compile_text(text) == []


def test_host_permission_refusal_does_not_require_a_doomed_tool(db, settings, execution_factory):
    ex = execution_factory("查询乙企业2026年9月发票总额", user="U002", company=None)
    payload, manifest, _ = context.assemble(db, model_settings(settings), ex, [])
    reply = rendered(db, ex, payload, manifest, [{"type": "explanation", "code": "access_denied"}])
    assert reply.cards[-1]["items"][0]["status"] == "blocked"


def test_one_denied_company_cannot_hide_work_for_an_accessible_company(
    db, settings, execution_factory
):
    ex = execution_factory(
        "查询甲企业2026年9月发票总额；查询乙企业2026年9月发票总额", user="U002", company="C001"
    )
    payload, manifest, _ = context.assemble(db, model_settings(settings), ex, [])
    with pytest.raises(AppError) as error:
        rendered(db, ex, payload, manifest, [{"type": "explanation", "code": "access_denied"}])
    assert error.value.details["request_items"][0]["company_id"] == "C001"


def test_renderer_rejects_a_month_that_was_never_executed(db, settings, execution_factory):
    ex = execution_factory("查询甲企业2026年8月和9月发票总额")
    initial, _, _ = context.assemble(db, model_settings(settings), ex, [])
    state = json.loads(initial["messages"][0]["content"].split("\n正式状态：\n")[1])
    scope = next(
        s for s in state["request_frame"]["available_scope_refs"] if s["period"] == "2026-09"
    )
    output, payload, manifest = recorded(
        db,
        settings,
        ex,
        "query_month",
        {
            "company_id": "C001",
            "period": "2026-09",
            "scope_ref": scope["scope_ref"],
        },
    )
    with pytest.raises(AppError) as error:
        rendered(
            db,
            ex,
            payload,
            manifest,
            [{"type": "result", "result_ref": output["result_ref"], "view": "monthly_summary"}],
        )
    assert error.value.details["request_items"][0]["period"] == "2026-08"


@pytest.mark.asyncio
async def test_runtime_repairs_missing_month_without_repeating_existing_query(
    db, settings, execution_factory
):
    ex = execution_factory("查询甲企业2026年8月和9月发票总额")
    refs = {}

    def query(period):
        def step(payload):
            state = json.loads(payload["messages"][0]["content"].split("\n正式状态：\n")[1])
            scope = next(
                s for s in state["request_frame"]["available_scope_refs"] if s["period"] == period
            )
            return tool(
                "query_month",
                {"company_id": "C001", "period": period, "scope_ref": scope["scope_ref"]},
            )

        return step

    def final(payload):
        for message in payload["messages"]:
            if message.get("role") == "tool":
                output = json.loads(message["content"])
                if output.get("ok") and output["data"].get("kind") == "monthly_summary":
                    refs[output["data"]["period"]] = output["result_ref"]
        return raw_final(
            [
                {"type": "result", "result_ref": ref, "view": "monthly_summary"}
                for ref in refs.values()
            ]
        )

    model = ExactModel([query("2026-09"), final, query("2026-08"), final])
    runtime = Runtime(db, model_settings(settings), model)
    await runtime._loop(ex)
    with db.transaction(readonly=True) as conn:
        turn = conn.execute(
            "SELECT status,final_cards,final_message FROM turns WHERE turn_id=%s", (ex.turn_id,)
        ).fetchone()
        calls = conn.execute(
            "SELECT count(*) n FROM tool_executions WHERE run_id=%s AND name='query_month'",
            (ex.run_id,),
        ).fetchone()
    assert turn["status"] == "completed"
    assert calls["n"] == 2
    assert "已核对2项" in turn["final_message"]
    progress = next(c for c in turn["final_cards"] if c["kind"] == "request_progress")
    assert [i["status"] for i in progress["items"]] == ["verified", "verified"]
