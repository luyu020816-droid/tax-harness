"""Program regressions for failures observed in the first live-model evaluation."""

import importlib.util
import json

import pytest
from pydantic import ValidationError
from test_runtime import ScriptedModel, final, model_settings, tool

from app.config import ROOT
from app.identity import Identity, create_session
from app.preferences import change_preference, list_preferences
from app.runtime import Runtime
from app.tool_schemas import FinalReply
from app.tools import ToolExecutor
from app.turns import accept_turn, read_turn
from app.values import uid

AUTH = Identity("U001")


def test_search_history_omitted_company_reaches_business_layer(db, execution_factory):
    execution = execution_factory("我去年提过的用途是什么？", company=None)
    result, _ = ToolExecutor(db).execute(
        execution, tool("search_history", {"query": "去年用途"})["tool_calls"][0]
    )
    assert result["ok"]
    assert result["data"]["not_found"]


@pytest.mark.asyncio
async def test_closed_multi_tool_batch_survives_own_memory_update(db, settings):
    session = create_session(db, AUTH, "C001")
    turn = accept_turn(
        db, AUTH, str(session["session_id"]), uid(), "查询甲企业2026年9月金额与用途缺项"
    )
    calls = tool("query_month", {"company_id": "C001", "period": "2026-09"})
    calls["tool_calls"] += tool("check_purposes", {"company_id": "C001", "period": "2026-09"})[
        "tool_calls"
    ]

    def check_input(payload):
        results = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
        assert {r["data"]["kind"] for r in results} == {"monthly_summary", "purpose_check"}
        assert (
            next(r for r in results if r["data"]["kind"] == "purpose_check")["data"][
                "missing_count"
            ]
            == 5
        )
        return final("月度金额已查询，还有五张用途待补。")

    model = ScriptedModel([calls, check_input])
    runtime = Runtime(db, model_settings(settings), model)
    await runtime.run(runtime.claim())
    result = read_turn(db, AUTH, turn["turn_id"])
    assert result["status"] == "completed", result
    assert {c["kind"] for c in result["final_cards"]} == {"monthly_summary", "purpose_check"}
    assert len(model.requests) == 2


def test_explicit_company_cannot_be_substituted(db, execution_factory):
    execution = execution_factory("查询乙企业2026年9月发票总额", user="U002", company="C001")
    result, _ = ToolExecutor(db).execute(
        execution, tool("query_month", {"company_id": "C001", "period": "2026-09"})["tool_calls"][0]
    )
    assert not result["ok"]
    assert result["error"]["code"] == "TARGET_SCOPE_MISMATCH"
    assert result["error"]["details"]["requested_company_ids"] == ["C002"]
    assert "data" not in result


def test_typo_preference_id_returns_owned_targets_for_retry(db, execution_factory):
    pref = change_preference(
        db, AUTH, action="remember", key="result_order", value="先列缺项", request_id=uid()
    )
    execution = execution_factory("删除刚才的长期偏好。")
    executor = ToolExecutor(db)
    args = {
        "action": "delete",
        "target_id": uid(),
        "expected_revision": 1,
        "source_refs": [{"source_id": execution.source_id, "quote": "删除刚才的长期偏好。"}],
    }
    failed, _ = executor.execute(execution, tool("manage_preference", args)["tool_calls"][0])
    assert not failed["ok"]
    candidates = failed["error"]["details"]["current_preferences"]
    assert [p["preference_id"] for p in candidates] == [str(pref["preference_id"])]
    assert candidates[0]["revision"] == 1
    saved, _ = executor.execute(
        execution,
        tool("manage_preference", {**args, "target_id": candidates[0]["preference_id"]})[
            "tool_calls"
        ][0],
    )
    assert saved["ok"]
    assert saved["data"]["status"] == "deleted"
    assert not list_preferences(db, AUTH)
    assert not list_preferences(db, Identity("U002"))


@pytest.mark.asyncio
async def test_invalid_waiting_reply_is_repaired_without_losing_denial(db, settings):
    session = create_session(db, Identity("U002"), "C001")
    turn = accept_turn(
        db, Identity("U002"), str(session["session_id"]), uid(), "把甲企业F002用途改为办公耗材采购"
    )
    model = ScriptedModel(
        [
            final("当前身份不能修改。", status="waiting_user", clarification=None),
            final("当前用户只有查询权限，本次未修改。"),
        ]
    )
    runtime = Runtime(db, model_settings(settings), model)
    await runtime.run(runtime.claim())
    assert read_turn(db, Identity("U002"), turn["turn_id"])["status"] == "completed"
    assert "waiting_user" in model.requests[1]["messages"][-1]["content"]
    with db.transaction(readonly=True) as conn:
        assert not conn.execute("SELECT 1 FROM operations WHERE state='committed'").fetchone()


@pytest.mark.asyncio
async def test_preference_success_requires_committed_evidence(db, settings):
    session = create_session(db, AUTH, "C001")
    turn = accept_turn(db, AUTH, str(session["session_id"]), uid(), "以后回复简洁。")

    def remember(payload):
        state = json.loads(payload["messages"][0]["content"].split("\n正式状态：\n", 1)[1])
        assert "没有该偏好操作的成功记录" in payload["messages"][-1]["content"]
        return tool(
            "manage_preference",
            {
                "action": "remember",
                "key": "response_style",
                "value": "回复简洁",
                "source_refs": [
                    {"source_id": state["current_source"]["source_id"], "quote": "以后回复简洁。"}
                ],
            },
        )

    claim = final("已记住回复简洁。", preference_action="remember", preference_status="committed")
    model = ScriptedModel([claim, remember, claim])
    runtime = Runtime(db, model_settings(settings), model)
    await runtime.run(runtime.claim())
    result = read_turn(db, AUTH, turn["turn_id"])
    assert result["status"] == "completed", result
    assert len(list_preferences(db, AUTH)) == 1
    assert len(model.requests) == 3


def test_final_reply_requires_memory_decision_and_consistent_status():
    with pytest.raises(ValidationError):
        FinalReply.model_validate({"message": "已记住。", "status": "completed"})
    with pytest.raises(ValidationError):
        FinalReply.model_validate_json(final("需要权限", status="waiting_user")["content"])


def test_preference_key_validation_reports_field(db, execution_factory):
    execution = execution_factory("以后先列缺项")
    result, _ = ToolExecutor(db).execute(
        execution,
        tool(
            "manage_preference",
            {
                "action": "remember",
                "key": "中文键",
                "value": "先列缺项",
                "source_refs": [{"source_id": execution.source_id, "quote": "以后先列缺项"}],
            },
        )["tool_calls"][0],
    )
    assert not result["ok"]
    assert result["error"]["details"]["fields"][0]["field"] == "key"


@pytest.mark.asyncio
async def test_multi_tool_batch_does_not_allow_external_memory_change(db, settings):
    session = create_session(db, AUTH, "C001")
    turn = accept_turn(db, AUTH, str(session["session_id"]), uid(), "查询甲企业2026年9月金额和用途")
    calls = tool("query_month", {"company_id": "C001", "period": "2026-09"})
    calls["tool_calls"] += tool("check_purposes", {"company_id": "C001", "period": "2026-09"})[
        "tool_calls"
    ]

    def external_change(payload):
        change_preference(
            db, AUTH, action="remember", key="response_style", value="详细", request_id=uid()
        )
        return final("旧上下文不能提交。")

    runtime = Runtime(db, model_settings(settings), ScriptedModel([calls, external_change]))
    await runtime.run(runtime.claim())
    result = read_turn(db, AUTH, turn["turn_id"])
    assert result["status"] == "interrupted"
    assert result["final_message"] is None


def evaluator():
    spec = importlib.util.spec_from_file_location("live_evaluate", ROOT / "scripts/evaluate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_step_assertion_catches_unsaved_preference(db):
    failures = evaluator().step_assertions(
        db,
        {"active_preferences": 1},
        {"status": "completed", "final_cards": []},
        {"tools": []},
        "U001",
    )
    assert failures == ["active_preferences: expected 1, observed 0"]


def test_step_assertion_catches_wrong_company_without_invoice_write():
    failures = evaluator().step_assertions(
        None,
        {"allowed_result_companies": ["C002"]},
        {"status": "completed", "final_cards": []},
        {"tools": [{"actual_result": json.dumps({"ok": True, "data": {"company_id": "C001"}})}]},
        "U002",
    )
    assert failures == ["Successful tool result used wrong company: C001"]
