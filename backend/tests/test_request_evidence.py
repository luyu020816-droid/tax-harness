from datetime import UTC

import pytest
from test_runtime import ScriptedModel, final, model_settings, tool

from app.errors import AppError
from app.identity import Identity, create_session
from app.request_evidence import bind_scope, explicit_periods, request_periods, validate_scope
from app.runtime import Runtime
from app.tools import ToolExecutor
from app.turns import accept_turn, read_turn
from app.values import uid


@pytest.mark.parametrize(
    "text,expected",
    [
        ("查询甲企业总额", set()),
        ("查询2026年9月", {"2026-09"}),
        ("查询2026-09", {"2026-09"}),
        ("查询2026年8月和9月", {"2026-08", "2026-09"}),
        ("查询9月", set()),
    ],
)
def test_explicit_months_do_not_default_year_or_month(text, expected):
    assert explicit_periods(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("不要查2026年8月，查2026年9月", {"2026-09"}),
        ("解释“查询2026年9月”这句话", set()),
        ("比如2026年9月，请问需要什么资料", set()),
        ("查“2026年9月”的总额", {"2026-09"}),
    ],
)
def test_scope_ignores_negative_examples_and_quoted_commands(text, expected):
    assert request_periods(text) == expected


def test_relative_month_uses_persisted_receive_time():
    from datetime import datetime

    received = datetime(2026, 12, 31, 16, 30, tzinfo=UTC)
    assert request_periods("查询本月和上个月", received) == {"2027-01", "2026-12"}


def test_scope_identity_cannot_cross_turn(db, execution_factory):
    first = execution_factory("查甲企业2026年9月总额")
    with db.transaction() as conn:
        scope = bind_scope(conn, first, "C001", "2026-09")
    second = execution_factory("查甲企业2026年9月总额")
    with db.transaction(readonly=True) as conn, pytest.raises(AppError):
        validate_scope(conn, second, scope["scope_ref"], "C001", "2026-09")


def test_model_cannot_invent_missing_month(db, execution_factory):
    execution = execution_factory("查一下甲企业发票总额。")
    output, _ = ToolExecutor(db).execute(
        execution, tool("query_month", {"company_id": "C001", "period": "2026-09"})["tool_calls"][0]
    )
    assert not output["ok"]
    assert output["error"]["code"] == "NEEDS_SCOPE_CLARIFICATION"


def test_current_explicit_month_overrides_previous_task(db, execution_factory):
    execution = execution_factory("查询甲企业2026年8月。")
    output, _ = ToolExecutor(db).execute(
        execution, tool("query_month", {"company_id": "C001", "period": "2026-09"})["tool_calls"][0]
    )
    assert not output["ok"] and output["error"]["code"] == "NEEDS_SCOPE_CLARIFICATION"


def test_bare_month_does_not_authorize_different_month(db, execution_factory):
    execution = execution_factory("查甲企业8月的发票总额。")
    output, _ = ToolExecutor(db).execute(
        execution, tool("query_month", {"company_id": "C001", "period": "2026-09"})["tool_calls"][0]
    )
    assert not output["ok"] and output["error"]["code"] == "NEEDS_SCOPE_CLARIFICATION"


def test_model_cannot_launder_guessed_month_through_task(db, execution_factory):
    text = "查一下甲企业发票总额。"
    execution = execution_factory(text)
    executor = ToolExecutor(db)
    created, _ = executor.execute(
        execution,
        tool(
            "change_task",
            {
                "company_id": "C001",
                "action": "start",
                "intent": "query_month",
                "period": "2026-09",
                "source_refs": [{"source_id": execution.source_id, "quote": text}],
            },
        )["tool_calls"][0],
    )
    assert not created["ok"] and created["error"]["code"] == "NEEDS_SCOPE_CLARIFICATION"
    output, _ = executor.execute(
        execution,
        tool("query_month", {"company_id": "C001", "period": "2026-09"})["tool_calls"][0],
    )
    assert not output["ok"] and output["error"]["code"] == "NEEDS_SCOPE_CLARIFICATION"


@pytest.mark.asyncio
async def test_same_session_confirmed_month_can_be_reused(db, settings):
    auth = Identity("U001")
    session = create_session(db, auth, "C001")
    for text in ["查询甲企业2026年9月发票总额。", "再查一下甲企业发票总额。"]:
        turn = accept_turn(db, auth, str(session["session_id"]), uid(), text)
        runtime = Runtime(
            db,
            model_settings(settings),
            ScriptedModel(
                [
                    tool("query_month", {"company_id": "C001", "period": "2026-09"}),
                    final("查询完成。"),
                ]
            ),
        )
        await runtime.run(runtime.claim())
        result = read_turn(db, auth, turn["turn_id"])
        assert result["status"] == "completed"
        assert result["final_cards"][0]["invoice_total"] == "9622.06"


@pytest.mark.asyncio
async def test_month_correction_reuses_only_confirmed_year(db, settings):
    auth = Identity("U001")
    session = create_session(db, auth, "C001")
    for text, period in [
        ("查甲企业2026年8月总额", "2026-08"),
        ("改查9月", "2026-09"),
        ("再查一次总额", "2026-09"),
    ]:
        turn = accept_turn(db, auth, str(session["session_id"]), uid(), text)
        runtime = Runtime(
            db,
            model_settings(settings),
            ScriptedModel(
                [tool("query_month", {"company_id": "C001", "period": period}), final("查询完成")]
            ),
        )
        await runtime.run(runtime.claim())
        result = read_turn(db, auth, turn["turn_id"])
        assert result["status"] == "completed", result
        assert result["final_cards"][0]["period"] == period


def test_history_excludes_current_question_and_its_own_tool_results(
    db, execution_factory, source_factory
):
    old = source_factory("以前确认过发票用途是办公采购")
    ex = execution_factory("我去年提过的发票用途具体是什么？")
    executor = ToolExecutor(db)
    for _ in range(2):
        output, _ = executor.execute(
            ex, tool("search_history", {"company_id": "C001", "query": "用途"})["tool_calls"][0]
        )
        assert output["ok"]
        assert [m["source_id"] for m in output["data"]["matches"]] == [str(old["source_id"])]
