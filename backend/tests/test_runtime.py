import asyncio
import json

import pytest

from app.context import assemble, validate
from app.errors import AppError
from app.identity import Identity, create_session
from app.preferences import change_preference
from app.runtime import Runtime
from app.turns import accept_turn, cancel_turn, read_turn
from app.values import digest, uid

AUTH = Identity("U001")


class ScriptedModel:
    """Program-test adapter. It does not validate natural-language model quality."""

    mode = "test"

    def __init__(self, steps):
        self.steps = iter(steps)
        self.requests = []

    async def complete(self, payload):
        self.requests.append(payload)
        step = next(self.steps)
        result = step(payload) if callable(step) else step
        if asyncio.iscoroutine(result):
            result = await result
        return {"message": result, "usage": None, "finish_reason": "stop"}


def tool(name, args):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": uid(),
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        ],
    }


def final(text, **extra):
    return {
        "role": "assistant",
        "content": json.dumps(
            {"message": text, "status": "completed", **extra}, ensure_ascii=False
        ),
    }


def model_settings(settings):
    return settings.model_copy(
        update={
            "model_id": "scripted-test",
            "model_base_url": "http://test.invalid",
            "model_api_key": "test-not-a-real-key",
            "model_context_limit": 64000,
        }
    )


@pytest.mark.asyncio
async def test_B15_actual_query_tool_loop_and_trace(db, settings, admin):
    session = create_session(db, AUTH, "C001")
    turn = accept_turn(db, AUTH, str(session["session_id"]), uid(), "查询甲企业2026-09月度资料")
    model = ScriptedModel(
        [
            tool("query_month", {"company_id": "C001", "period": "2026-09"}),
            final("查询完成，三项金额分别列在结果中。"),
        ]
    )
    runtime = Runtime(db, model_settings(settings), model)
    execution = runtime.claim()
    await runtime.run(execution)
    result = read_turn(db, AUTH, turn["turn_id"])
    assert result["status"] == "completed", result
    assert result["final_cards"][0]["invoice_total"] == "9622.06"
    snapshots = admin.execute(
        "SELECT * FROM context_snapshots WHERE run_id=%s ORDER BY model_step", (execution.run_id,)
    ).fetchall()
    assert len(snapshots) == 2
    assert all(
        s["payload"] == model.requests[i] and s["payload_hash"] == digest(model.requests[i])
        for i, s in enumerate(snapshots)
    )
    assert model.requests[-1]["messages"][-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_M04_check_creates_task_and_new_session_recovers(db, settings):
    session = create_session(db, AUTH, "C001")
    turn = accept_turn(db, AUTH, str(session["session_id"]), uid(), "检查甲企业2026-09用途缺项")
    model = ScriptedModel(
        [
            tool("check_purposes", {"company_id": "C001", "period": "2026-09"}),
            final(
                "有5张需要补用途。",
                status="waiting_user",
                clarification={
                    "intent": "complete_purposes",
                    "company_id": "C001",
                    "period": "2026-09",
                    "missing_fields": ["purpose"],
                    "summary": "等待用户逐张提供实际用途",
                },
            ),
        ]
    )
    runtime = Runtime(db, model_settings(settings), model)
    await runtime.run(runtime.claim())
    result = read_turn(db, AUTH, turn["turn_id"])
    assert result["status"] == "waiting_user", result
    other = create_session(db, AUTH, "C001")
    from app.memory import load_core

    core = load_core(db, AUTH, str(other["session_id"]))
    assert core["task"]["period"] == "2026-09"
    assert core["task"]["unresolved_json"]["missing_count"] == 5


@pytest.mark.asyncio
async def test_M04_query_missing_period_is_persistent(db, settings):
    first = create_session(db, AUTH, "C001")
    request = accept_turn(db, AUTH, str(first["session_id"]), uid(), "查询甲企业发票总额")
    asking = ScriptedModel(
        [
            final(
                "请问查询哪个月份？",
                status="waiting_user",
                clarification={
                    "intent": "query_month",
                    "company_id": "C001",
                    "period": None,
                    "missing_fields": ["period"],
                    "summary": "甲企业发票总额缺少月份",
                },
            )
        ]
    )
    runtime = Runtime(db, model_settings(settings), asking)
    await runtime.run(runtime.claim())
    assert read_turn(db, AUTH, request["turn_id"])["status"] == "waiting_user"
    second = create_session(db, AUTH, "C001")
    next_turn = accept_turn(db, AUTH, str(second["session_id"]), uid(), "2026年9月")
    answering = ScriptedModel(
        [
            tool("query_month", {"company_id": "C001", "period": "2026-09"}),
            final("甲企业9月发票总额见结果。", resume_turn_id=request["turn_id"]),
        ]
    )
    runtime = Runtime(db, model_settings(settings), answering)
    await runtime.run(runtime.claim())
    assert read_turn(db, AUTH, next_turn["turn_id"])["status"] == "completed"


def test_F02_acceptance_idempotence(db):
    session = create_session(db, AUTH, "C001")
    request_id = uid()
    a = accept_turn(db, AUTH, str(session["session_id"]), request_id, "查询资料")
    b = accept_turn(db, AUTH, str(session["session_id"]), request_id, "查询资料")
    assert a["turn_id"] == b["turn_id"]
    with pytest.raises(AppError) as exc:
        accept_turn(db, AUTH, str(session["session_id"]), request_id, "不同内容")
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_F34_delete_while_model_inflight_blocks_answer(db, settings):
    pref = change_preference(
        db, AUTH, action="remember", key="reply_style", value="简洁", request_id=uid()
    )
    session = create_session(db, AUTH, "C001")
    turn = accept_turn(db, AUTH, str(session["session_id"]), uid(), "你好")

    async def delete_then_answer(payload):
        change_preference(
            db,
            AUTH,
            action="delete",
            target_id=str(pref["preference_id"]),
            expected_revision=pref["revision"],
            request_id=uid(),
        )
        return final("仍然照旧使用简洁偏好")

    runtime = Runtime(db, model_settings(settings), ScriptedModel([delete_then_answer]))
    await runtime.run(runtime.claim())
    result = read_turn(db, AUTH, turn["turn_id"])
    assert result["status"] == "interrupted"
    assert result["final_message"] is None


def test_F06_cancel_fences_old_run(db, settings):
    session = create_session(db, AUTH, "C001")
    turn = accept_turn(db, AUTH, str(session["session_id"]), uid(), "查询甲企业2026-09")
    runtime = Runtime(db, model_settings(settings), ScriptedModel([]))
    execution = runtime.claim()
    payload, manifest, _ = assemble(db, runtime.settings, execution, [])
    cancel_turn(db, AUTH, turn["turn_id"], uid())
    with pytest.raises(AppError):
        validate(db, execution, payload, manifest)


def test_F18_budget_does_not_silently_drop_critical_state(db, settings, execution_factory):
    execution = execution_factory("当前问题" * 3000)
    with pytest.raises(AppError) as exc:
        assemble(db, model_settings(settings), execution, [], budget=512)
    assert exc.value.code == "CONTEXT_CAPACITY_EXCEEDED"


def test_M12_manifest_cannot_hide_missing_actual_fields(db, settings, execution_factory):
    execution = execution_factory("你好")
    payload, manifest, _ = assemble(db, model_settings(settings), execution, [])
    payload["messages"] = [payload["messages"][0]]
    manifest["payload_hash"] = digest(payload)
    with pytest.raises(AppError) as exc:
        validate(db, execution, payload, manifest)
    assert exc.value.code == "CONTEXT_INCOMPLETE"


def test_B12_save_rechecks_real_task_progress(db, settings, execution_factory):
    from app.memory import load_core
    from app.tools import ToolExecutor

    execution = execution_factory("检查甲企业2026年9月用途；F002用途补为办公耗材采购")
    executor = ToolExecutor(db)
    checked, _ = executor.execute(
        execution,
        tool("check_purposes", {"company_id": "C001", "period": "2026-09"})["tool_calls"][0],
    )
    assert checked["data"]["missing_count"] == 5
    with db.transaction(readonly=True) as conn:
        invoice = conn.execute(
            "SELECT revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
        ).fetchone()
    saved, _ = executor.execute(
        execution,
        tool(
            "update_invoice",
            {
                "company_id": "C001",
                "invoice_id": "F002",
                "expected_revision": invoice["revision"],
                "patch": {"purpose": "办公耗材采购"},
                "source_refs": [
                    {"source_id": execution.source_id, "quote": "F002用途补为办公耗材采购"}
                ],
            },
        )["tool_calls"][0],
    )
    assert saved["ok"], saved
    assert saved["data"]["verification_state"] == "verified"
    assert saved["data"]["post_save_check"]["data"]["missing_count"] == 4
    core = load_core(db, AUTH, execution.session_id)
    assert core["task"]["snapshot_current"] is True
    assert core["task_evidence"]["missing_count"] == 4
    assert "F002" not in core["task_evidence"]["missing_invoice_ids"]
    payload, manifest, _ = assemble(db, model_settings(settings), execution, [])
    assert manifest["rule_refs"]
    assert "办公耗材采购" in payload["messages"][-1]["content"]
