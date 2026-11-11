"""A committed save survives broken summaries without pretending the whole request succeeded."""

import pytest
from test_conversation_context import AUTH, settled
from test_judge_boundaries import ExactModel, raw_final
from test_runtime import model_settings, tool
from test_trusted_writes import TEXT, candidate

from app import context, invoice_write
from app.errors import AppError
from app.identity import create_session
from app.runtime import Runtime
from app.tools import ToolExecutor
from app.turns import accept_turn, read_turn
from app.values import uid


def committed(db, execution):
    plan = invoice_write.prepare(db, execution.auth, **candidate(execution))
    output, _ = ToolExecutor(db).execute(
        execution, tool("update_invoice", {"plan_id": plan["plan_id"]})["tool_calls"][0]
    )
    assert output["ok"]
    return output["data"]["operation_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_code",
    [
        "SUMMARY_INVALID",
        "REPLY_EVIDENCE_MISSING",
        "MODEL_PROTOCOL_UNSUPPORTED",
        "MODEL_UNAVAILABLE",
        "RUN_LIMIT_EXCEEDED",
    ],
)
async def test_summary_failure_after_commit_delivers_receipt_and_keeps_failed_state(
    db, settings, admin, execution_factory, failure_code
):
    ex = execution_factory(TEXT)
    operation_id = committed(db, ex)
    runtime = Runtime(db, model_settings(settings), ExactModel([]))

    async def fail_summary(_):
        raise AppError(failure_code, "后续答复失败，原文保留。", 422)

    runtime._loop = fail_summary
    await runtime.run(ex)
    result = read_turn(db, ex.auth, ex.turn_id)
    assert result["status"] == "failed" and result["error"]["code"] == failure_code
    assert result["final_cards"][0]["operation_id"] == operation_id
    assert result["final_cards"][0]["verification_state"] == "verified"
    assert "已保存并核验" in result["final_message"]
    assert "不表示本轮其他事项已完成" in result["final_message"]
    assert result["final_response_ref"]
    # Retrying failure delivery itself never repeats the business write.
    runtime.fail(ex, AppError("SUMMARY_INVALID", "again", 422))
    assert admin.execute("SELECT count(*) n FROM operations").fetchone()["n"] == 1
    assert (
        admin.execute(
            "SELECT revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
        ).fetchone()["revision"]
        == 2
    )


@pytest.mark.parametrize(
    "change,expected", [("pending", "pending"), ("changed", "changed_since_commit")]
)
def test_failure_receipt_does_not_upgrade_pending_or_hide_later_changes(
    db, settings, admin, execution_factory, change, expected
):
    ex = execution_factory(TEXT)
    committed(db, ex)
    if change == "pending":
        admin.execute("UPDATE operations SET verification_state='pending'")
    else:
        admin.execute(
            "UPDATE invoices SET purpose='后来更正的用途',revision=revision+1 WHERE company_id='C001' AND invoice_id='F002'"
        )
    Runtime(db, model_settings(settings), ExactModel([])).fail(
        ex, AppError("SUMMARY_INVALID", "摘要失败", 422)
    )
    result = read_turn(db, ex.auth, ex.turn_id)
    assert result["final_cards"][0]["verification_state"] == expected
    assert "已保存并核验" not in result["final_message"]


@pytest.mark.parametrize("invalidator", ["permission", "epoch", "fence"])
@pytest.mark.parametrize(
    "failure_code", ["SUMMARY_INVALID", "REPLY_EVIDENCE_MISSING", "MODEL_UNAVAILABLE"]
)
def test_receipt_failure_path_does_not_bypass_revocation_or_stale_execution(
    db, settings, admin, execution_factory, invalidator, failure_code
):
    ex = execution_factory(TEXT)
    committed(db, ex)
    if invalidator == "permission":
        admin.execute(
            "UPDATE user_company_grants SET can_read=false,can_update=false,can_import_invoices=false,can_upload_knowledge=false WHERE user_id='U001' AND company_id='C001'"
        )
    elif invalidator == "epoch":
        admin.execute("UPDATE users SET memory_epoch=memory_epoch+1 WHERE user_id='U001'")
    else:
        admin.execute(
            "UPDATE work_items SET fencing_token=fencing_token+1 WHERE work_id=%s", (ex.work_id,)
        )
    Runtime(db, model_settings(settings), ExactModel([])).fail(
        ex, AppError(failure_code, "后续答复失败", 422)
    )
    result = admin.execute(
        "SELECT final_cards,final_message FROM turns WHERE turn_id=%s", (ex.turn_id,)
    ).fetchone()
    assert result["final_cards"] == [] and result["final_message"] is None


@pytest.mark.parametrize("failure_code", ["STALE_CONTEXT", "SOURCE_INVALIDATED"])
def test_invalidated_execution_never_emits_a_saved_receipt(
    db, settings, admin, execution_factory, failure_code
):
    ex = execution_factory(TEXT)
    committed(db, ex)
    Runtime(db, model_settings(settings), ExactModel([])).fail(
        ex, AppError(failure_code, "来源失效", 409)
    )
    result = admin.execute(
        "SELECT final_cards,final_message FROM turns WHERE turn_id=%s", (ex.turn_id,)
    ).fetchone()
    assert result["final_cards"] == [] and result["final_message"] is None


def test_summary_failure_without_a_commit_has_no_success_receipt(db, settings, execution_factory):
    ex = execution_factory(TEXT)
    Runtime(db, model_settings(settings), ExactModel([])).fail(
        ex, AppError("SUMMARY_INVALID", "摘要失败", 422)
    )
    result = read_turn(db, ex.auth, ex.turn_id)
    assert not result["final_cards"] and not result["final_message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fits", [True, False])
async def test_invalid_summary_reuses_full_history_only_when_hard_budget_allows(
    db, settings, monkeypatch, fits
):
    import app.compaction

    session = create_session(db, AUTH, "C001")
    secret = "原话标记KEEP-HISTORY-7421，不得外发。"
    settled(db, str(session["session_id"]), secret * 180)
    for index in range(5):
        settled(db, str(session["session_id"]), f"近期对话 {index}，不修改数据。")
    accepted = accept_turn(
        db, AUTH, str(session["session_id"]), uid(), "只记录本轮背景，不修改数据。"
    )
    wide = model_settings(settings).model_copy(
        update={"input_budget": 100000, "model_context_limit": 200000}
    )
    runtime = Runtime(db, wide, ExactModel([]))
    ex = runtime.claim()
    _, initial, dropped = context.assemble(db, wide, ex, [], proactive_compact=False)
    assert not dropped
    limit = initial["token_count"] + (128 if fits else -2000)
    config = wide.model_copy(update={"input_budget": limit})
    if fits:
        _, probe, probe_dropped = context.assemble(db, config, ex, [], proactive_compact=False)
        assert not probe_dropped, {
            "initial": initial["token_count"],
            "probe": probe["token_count"],
            "limit": limit,
        }
    attempts = []

    async def invalid(*args):
        attempts.append(True)
        raise AppError("SUMMARY_INVALID", "故障注入：来源标签无效。", 422)

    monkeypatch.setattr(app.compaction, "compact", invalid)

    def answer(payload):
        assert secret in str(payload)
        assert context.estimate_tokens(payload) <= limit
        return raw_final([{"type": "chat_answer", "text": "收到，本轮只讨论背景。"}])

    model = ExactModel([answer])
    runtime = Runtime(db, config, model)
    await runtime.run(ex)
    result = read_turn(db, AUTH, accepted["turn_id"])
    assert attempts
    if fits:
        assert result["status"] == "completed" and len(model.requests) == 1, {
            "error": result["error"],
            "requests": len(model.requests),
        }
    else:
        assert result["status"] == "failed" and not model.requests
        assert not result["final_cards"]
