import json

import pytest
from test_conversation_context import settled
from test_runtime import ScriptedModel, final, model_settings, tool

from app.context import assemble, estimate_tokens
from app.errors import AppError
from app.identity import Identity, create_session
from app.memory import load_core
from app.request_evidence import prepare_frame, require_period
from app.runtime import Runtime
from app.tools import ToolExecutor
from app.turns import accept_turn, read_turn
from app.values import uid


def test_hard_budget_is_enforced_with_or_without_history(db, settings, execution_factory):
    config = model_settings(settings).model_copy(update={"input_budget": 16000})
    no_history = execution_factory("当前需要处理的内容" * 1200)
    with pytest.raises(AppError) as error:
        assemble(db, config, no_history, [])
    assert error.value.code == "CONTEXT_CAPACITY_EXCEEDED"
    assert error.value.details["estimated_input_tokens"] > 16000
    assert error.value.details["input_budget"] == 16000
    with pytest.raises(AppError) as error:
        assemble(db, config, no_history, [], budget=64000)
    assert error.value.details["input_budget"] == 16000
    with_history = execution_factory("当前需要处理的内容" * 1200)
    settled(db, with_history.session_id, "你好")
    with db.transaction() as conn:
        seq = conn.execute(
            "UPDATE users SET next_source_seq=next_source_seq+1 WHERE user_id='U001' RETURNING next_source_seq"
        ).fetchone()["next_source_seq"]
        conn.execute("UPDATE turns SET source_seq=%s WHERE turn_id=%s", (seq, with_history.turn_id))
    with_history.source_seq = seq
    with pytest.raises(AppError) as error:
        assemble(db, config, with_history, [])
    assert error.value.code == "CONTEXT_CAPACITY_EXCEEDED"
    assert error.value.details["estimated_input_tokens"] > 16000


async def failed_invoice_lookup(db, settings):
    auth = Identity("U001")
    session = str(create_session(db, auth, "C001")["session_id"])
    turn = accept_turn(db, auth, session, uid(), "查一下甲企业2026年9月的发票")
    bad = {"role": "assistant", "content": "{"}
    runtime = Runtime(
        db,
        model_settings(settings),
        ScriptedModel(
            [
                tool("find_invoices", {"company_id": "C001", "period": "2026-09"}),
                *[bad for _ in range(8)],
            ]
        ),
    )
    await runtime.run(runtime.claim())
    assert read_turn(db, auth, turn["turn_id"])["status"] == "failed"
    with db.transaction(readonly=True) as conn:
        row = conn.execute(
            "SELECT e.state,s.content FROM tool_executions e JOIN runs r ON r.run_id=e.run_id "
            "JOIN sources s ON s.source_id=e.result_source_id WHERE r.turn_id=%s AND e.name='find_invoices'",
            (turn["turn_id"],),
        ).fetchone()
        assert row and row["state"] == "done" and json.loads(row["content"])["ok"] is True
    return auth, session, runtime, turn


@pytest.mark.asyncio
async def test_failed_reply_can_recover_confirmed_query_period(db, settings):
    auth, session, runtime, _ = await failed_invoice_lookup(db, settings)
    accept_turn(db, auth, session, uid(), "继续查询")
    execution = runtime.claim()
    with db.transaction(readonly=True) as conn:
        basis = require_period(conn, execution, "C001", "2026-09")
        assert basis["kind"] == "confirmed_history"
        assert basis["context_sources"]
    frame = prepare_frame(db, execution, load_core(db, auth, session))
    assert any(x["period"] == "2026-09" for x in frame["available_scope_refs"])


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["query_month", "get_rules", "check_purposes"])
async def test_other_successful_queries_keep_explicit_scope_after_reply_fails(db, settings, name):
    auth = Identity("U001")
    session = str(create_session(db, auth, "C001")["session_id"])
    first = accept_turn(db, auth, session, uid(), "查询甲企业2026年9月的资料")
    args = {"company_id": "C001", "period": "2026-09"}
    if name == "get_rules":
        args["topics"] = ["purpose"]
    bad = {"role": "assistant", "content": "{"}
    runtime = Runtime(
        db, model_settings(settings), ScriptedModel([tool(name, args), *[bad for _ in range(8)]])
    )
    await runtime.run(runtime.claim())
    assert read_turn(db, auth, first["turn_id"])["status"] == "failed"
    with db.transaction(readonly=True) as conn:
        assert conn.execute(
            "SELECT 1 FROM tool_executions e JOIN runs r ON r.run_id=e.run_id WHERE r.turn_id=%s AND e.name=%s AND e.state='done'",
            (first["turn_id"], name),
        ).fetchone()
    accept_turn(db, auth, session, uid(), "继续查询")
    execution = runtime.claim()
    with db.transaction(readonly=True) as conn:
        assert require_period(conn, execution, "C001", "2026-09")["kind"] == "confirmed_history"


@pytest.mark.asyncio
async def test_failed_query_scope_does_not_override_later_correction(db, settings):
    auth, session, runtime, _ = await failed_invoice_lookup(db, settings)
    later = accept_turn(db, auth, session, uid(), "改查2026年8月")
    with db.transaction() as conn:
        conn.execute("UPDATE turns SET status='failed' WHERE turn_id=%s", (later["turn_id"],))
        conn.execute(
            "UPDATE work_items SET state='failed' WHERE source_id IN (SELECT source_id FROM sources WHERE turn_id=%s)",
            (later["turn_id"],),
        )
    accept_turn(db, auth, session, uid(), "继续查询")
    execution = runtime.claim()
    with pytest.raises(AppError) as error, db.transaction(readonly=True) as conn:
        require_period(conn, execution, "C001", "2026-09")
    assert error.value.code == "NEEDS_SCOPE_CLARIFICATION"


def test_budget_failure_explains_successful_query_without_claiming_delivery(
    db, settings, execution_factory
):
    ex = execution_factory("查询甲企业2026年9月发票")
    output, _ = ToolExecutor(db).execute(
        ex, tool("find_invoices", {"company_id": "C001", "period": "2026-09"})["tool_calls"][0]
    )
    assert output["ok"]
    runtime = Runtime(db, model_settings(settings), ScriptedModel([]))
    runtime.fail(
        ex,
        AppError(
            "CONTEXT_CAPACITY_EXCEEDED",
            "输入超出预算",
            422,
            details={
                "estimated_input_tokens": 17000,
                "input_budget": 16000,
                "stage": "after_tools",
            },
        ),
    )
    result = read_turn(db, ex.auth, ex.turn_id)
    assert result["status"] == "failed"
    assert "发票查询已执行成功" in result["error"]["message"]
    assert result["error"]["details"]["query_succeeded"] is True
    assert result["error"]["details"]["input_budget"] == 16000
    assert result["final_cards"][0]["kind"] == "invoices"
    assert "其他事项不能视为已完成" in result["final_message"]


def test_budget_failure_does_not_deliver_stale_invoice_page(db, settings, admin, execution_factory):
    ex = execution_factory("查询甲企业2026年9月发票")
    output, _ = ToolExecutor(db).execute(
        ex, tool("find_invoices", {"company_id": "C001", "period": "2026-09"})["tool_calls"][0]
    )
    assert output["ok"]
    admin.execute("UPDATE company_heads SET data_revision=data_revision+1 WHERE company_id='C001'")
    runtime = Runtime(db, model_settings(settings), ScriptedModel([]))
    runtime.fail(ex, AppError("CONTEXT_CAPACITY_EXCEEDED", "输入超出预算", 422))
    result = read_turn(db, ex.auth, ex.turn_id)
    assert result["error"]["details"]["query_succeeded"] is True
    assert not result["final_cards"]


def test_budget_failure_does_not_choose_one_of_multiple_queries(db, settings, execution_factory):
    ex = execution_factory("查询甲企业2026年8月和9月发票")
    executor = ToolExecutor(db)
    for period in ("2026-08", "2026-09"):
        output, _ = executor.execute(
            ex, tool("find_invoices", {"company_id": "C001", "period": period})["tool_calls"][0]
        )
        assert output["ok"]
    runtime = Runtime(db, model_settings(settings), ScriptedModel([]))
    runtime.fail(ex, AppError("CONTEXT_CAPACITY_EXCEEDED", "输入超出预算", 422))
    result = read_turn(db, ex.auth, ex.turn_id)
    assert result["error"]["details"]["query_succeeded"] is True
    assert not result["final_cards"]


def test_budget_failure_without_query_does_not_claim_success(db, settings, execution_factory):
    ex = execution_factory("你好")
    runtime = Runtime(db, model_settings(settings), ScriptedModel([]))
    runtime.fail(ex, AppError("CONTEXT_CAPACITY_EXCEEDED", "输入超出预算", 422))
    result = read_turn(db, ex.auth, ex.turn_id)
    assert "查询已执行成功" not in result["error"]["message"]
    assert "query_succeeded" not in result["error"]["details"]


@pytest.mark.asyncio
async def test_query_then_budget_failure_and_followup_requery(db, settings, admin):
    auth = Identity("U001")
    session = str(create_session(db, auth, "C001")["session_id"])
    turn = accept_turn(db, auth, session, uid(), "查一下甲企业2026年9月的发票")
    config = model_settings(settings)
    runtime = Runtime(
        db,
        config,
        ScriptedModel([tool("find_invoices", {"company_id": "C001", "period": "2026-09"})]),
    )
    execution = runtime.claim()
    initial, _, _ = assemble(db, config, execution, [])
    runtime.settings = config.model_copy(update={"input_budget": estimate_tokens(initial) + 128})
    await runtime.run(execution)
    failed = read_turn(db, auth, turn["turn_id"])
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "CONTEXT_CAPACITY_EXCEEDED"
    assert failed["error"]["details"]["stage"] == "after_tools"
    assert failed["error"]["details"]["query_succeeded"] is True
    assert "发票查询已执行成功" in failed["error"]["message"]
    assert failed["final_cards"][0]["kind"] == "invoices"

    admin.execute(
        "UPDATE invoices SET amount=amount+100,revision=revision+1 WHERE company_id='C001' AND invoice_id='F001'"
    )
    admin.execute("UPDATE company_heads SET data_revision=data_revision+1 WHERE company_id='C001'")
    followup = accept_turn(db, auth, session, uid(), "继续查询发票总额")
    runtime.settings = config
    runtime.model = ScriptedModel(
        [
            tool("query_month", {"company_id": "C001", "period": "2026-09"}),
            final("查询完成。"),
        ]
    )
    await runtime.run(runtime.claim())
    result = read_turn(db, auth, followup["turn_id"])
    assert result["status"] == "completed", result
    assert result["final_cards"][0]["period"] == "2026-09"
    assert result["final_cards"][0]["invoice_total"] == "9722.06"
