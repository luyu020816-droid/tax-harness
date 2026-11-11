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

    def __init__(self, steps, *, auto_summary=False):
        self.steps = iter(steps)
        self.auto_summary = auto_summary
        self.requests = []
        self.pending_write = False

    async def complete(self, payload):
        self.requests.append(payload)
        if self.auto_summary and payload["messages"][0].get("content", "").startswith(
            "你只生成有来源的历史导航摘要"
        ):
            data = json.loads(payload["messages"][-1]["content"])
            return {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "程序测试导航，事实请核对原文与正式状态。",
                            "covered_sources": data["source_ids"],
                        }
                    )
                },
                "usage": None,
                "finish_reason": "stop",
            }
        if self.pending_write:
            from scripted_protocol import actual_outputs

            self.pending_write = False
            output = actual_outputs(payload)[-1]
            if output.get("ok") and output["data"].get("status") == "authorized":
                return {
                    "message": tool("update_invoice", {"plan_id": output["data"]["plan_id"]}),
                    "usage": None,
                    "finish_reason": "stop",
                }
        step = next(self.steps)
        result = step(payload) if callable(step) else step
        if asyncio.iscoroutine(result):
            result = await result
        for call in result.get("tool_calls", []):
            if call["function"]["name"] in ("query_month", "get_rules", "check_purposes"):
                state = json.loads(payload["messages"][0]["content"].split("\n正式状态：\n", 1)[1])
                arguments = json.loads(call["function"]["arguments"])
                binding = next(
                    (
                        b
                        for b in state["request_frame"]["available_scope_refs"]
                        if b["company_id"] == arguments["company_id"]
                        and b["period"] == arguments["period"]
                    ),
                    None,
                )
                arguments["scope_ref"] = (
                    binding["scope_ref"] if binding else "00000000-0000-0000-0000-000000000001"
                )
                call["function"]["arguments"] = json.dumps(arguments, ensure_ascii=False)
            if call["function"]["name"] == "update_invoice":
                arguments = json.loads(call["function"]["arguments"])
                if "plan_id" not in arguments:
                    call["function"]["name"] = "prepare_invoice_write"
                    self.pending_write = True
        if result.get("content"):
            try:
                legacy = json.loads(result["content"])
            except json.JSONDecodeError:
                legacy = None
            if isinstance(legacy, dict) and "message" in legacy and "blocks" not in legacy:
                from scripted_protocol import migrate_answer

                result = migrate_answer(legacy, payload)
        return {"message": result, "usage": None, "finish_reason": "stop"}


def tool(name, args):
    if name in ("query_month", "get_rules", "check_purposes") and "scope_ref" not in args:
        args = {**args, "scope_ref": "00000000-0000-0000-0000-000000000001"}
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
            {
                "message": text,
                "status": "completed",
                "preference_action": None,
                "preference_status": "not_requested",
                **extra,
            },
            ensure_ascii=False,
        ),
    }


def model_settings(settings):
    return settings.model_copy(
        update={
            "model_id": "scripted-test",
            "model_base_url": "http://test.invalid",
            "model_api_key": "test-not-a-real-key",
            "model_context_limit": 64000,
            "input_budget": None,
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
async def test_calendar_question_does_not_dispatch_search(db, settings):
    session = str(create_session(db, AUTH, "C001")["session_id"])
    turn = accept_turn(db, AUTH, session, uid(), "今天星期几，具体日期")
    model = ScriptedModel([])
    runtime = Runtime(db, model_settings(settings), model)
    await runtime.run(runtime.claim())
    result = read_turn(db, AUTH, turn["turn_id"])
    assert result["status"] == "completed", result
    assert result["final_message"].startswith("今天是 ")
    assert "星期" in result["final_message"]
    assert not model.requests


def answer_plan(**plan):
    return {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)}


@pytest.mark.asyncio
async def test_chat_question_retries_irrelevant_acknowledgement(db, settings):
    session = create_session(db, AUTH, "C001")
    turn = accept_turn(db, AUTH, str(session["session_id"]), uid(), "我有点无聊，推荐我做点事")
    model = ScriptedModel(
        [
            answer_plan(blocks=[{"type": "explanation", "code": "acknowledged"}]),
            answer_plan(blocks=[{"type": "chat_answer", "text": "可以听首歌，或者出门散散步。"}]),
        ]
    )
    runtime = Runtime(db, model_settings(settings), model)
    await runtime.run(runtime.claim())
    row = read_turn(db, AUTH, turn["turn_id"])
    assert row["status"] == "completed", row
    assert row["final_message"] == "可以听首歌，或者出门散散步。"
    assert len(model.requests) == 2


@pytest.mark.asyncio
async def test_public_weather_asks_for_city_then_searches_and_follows_up(db, settings, monkeypatch):
    from app import public_web

    requests = []

    def search(request):
        requests.append(request)
        forecast = request["weather_date"]
        summary = (
            "Overcast, 48°F (8.9°C), feels like 44°F. Humidity 66%."
            if request["relative"] == "今天"
            else "Overcast."
        )
        source = f"https://exa.ai/library/weather/test?date={forecast}"
        content = f"Title: Weather forecast\nURL: {source}\nHighlights:\n{summary}\n- High: 64°F (17.6°C) / Low: 46°F (7.5°C)"
        return {
            **request,
            "kind": "public_web",
            "results": public_web._results(
                {"content": [{"type": "text", "text": content}]}, forecast
            ),
        }

    monkeypatch.setattr(public_web, "search", search)
    session = str(create_session(db, AUTH, "C001")["session_id"])
    runtime = Runtime(
        db,
        model_settings(settings).model_copy(
            update={
                "public_web_enabled": True,
                "context_auto_compact": False,
                "input_budget": 32000,
            }
        ),
        ScriptedModel([]),
    )

    def delivered(payload):
        output = json.loads(payload["messages"][-1]["content"])
        assert output["ok"], output
        return answer_plan(
            blocks=[{"type": "result", "view": "public_web", "result_ref": output["result_ref"]}],
        )

    async def ask(
        text, *, city=None, days_ahead=0, metric="overview", answer=delivered, kind="weather"
    ):
        runtime.model.steps = iter(
            [
                tool(
                    "search_public_web",
                    {
                        "kind": kind,
                        "question": text,
                        "city": city,
                        "days_ahead": days_ahead,
                        "metric": metric,
                    },
                ),
                answer,
            ]
        )
        turn = accept_turn(db, AUTH, session, uid(), text)
        await runtime.run(runtime.claim())
        return read_turn(db, AUTH, turn["turn_id"])

    clarification = await ask(
        "今天天气怎么样",
        answer=answer_plan(
            blocks=[],
            status="waiting_user",
            clarification={"intent": "other", "missing_fields": ["location"]},
        ),
    )
    assert clarification["status"] == "waiting_user"
    assert clarification["final_message"] == "你想查哪个城市的天气？"
    assert not requests
    result = await ask("南京", city="南京")
    assert result["status"] == "completed"
    assert result["final_message"].startswith("南京今天是阴天，气温约 9°C，预计 8–18°C。")
    assert result["continuation_of_turn_id"] == clarification["turn_id"]
    for question, city, days, metric, expected in (
        ("最低温度", "南京", 0, "low", "南京今天最低气温约 8°C。"),
        ("那最高呢", "南京", 0, "high", "南京今天最高气温约 18°C。"),
        ("那湿度呢", "南京", 0, "humidity", "南京今天湿度为 66%。"),
        ("那明天呢", "南京", 1, "overview", "南京明天是阴天，预计 8–18°C。"),
        ("最低温度", "南京", 1, "low", "南京明天最低气温约 8°C。"),
        ("会不会下雨", "南京", 1, "rain", "这份预报没有给出南京明天的降雨概率。"),
        ("换成上海呢", "上海", 1, "overview", "上海明天是阴天，预计 8–18°C。"),
        ("现在多少度", "上海", 0, "current", "上海今天气温约 9°C。"),
    ):
        result = await ask(question, city=city, days_ahead=days, metric=metric)
        assert result["status"] == "completed", result
        assert result["final_message"].startswith(expected), result
        assert "[天气来源]" in result["final_message"]
    count = len(requests)
    result = await ask(
        "上海下周天气怎么样",
        city="上海",
        answer=answer_plan(
            blocks=[{"type": "explanation", "code": "public_weather_period"}],
        ),
    )
    assert "今天、明天和后天" in result["final_message"]
    result = await ask(
        "不要联网查一下公司内部口令",
        kind="search",
        answer=answer_plan(
            blocks=[{"type": "explanation", "code": "public_web_scope_required"}],
        ),
    )
    assert result["status"] == "completed"
    assert len(requests) == count

    def unavailable(_request):
        raise AppError("PUBLIC_WEB_UNAVAILABLE", "联网查询暂时不可用，请稍后再试。", 503)

    monkeypatch.setattr(public_web, "search", unavailable)
    result = await ask(
        "南京明天最低温度",
        city="南京",
        days_ahead=1,
        answer=answer_plan(
            blocks=[{"type": "explanation", "code": "public_web_unavailable"}],
        ),
    )
    assert result["status"] == "completed", result
    assert result["final_message"] == "联网查询暂时不可用，请稍后再试。"


@pytest.mark.asyncio
async def test_rule_followup_refreshes_historical_tool_result(db, settings):
    session = str(create_session(db, AUTH, "C001")["session_id"])

    def select_latest_rules(payload):
        for message in reversed(payload["messages"]):
            if message["role"] != "tool":
                continue
            result = json.loads(message["content"])
            if (result.get("data") or {}).get("kind") == "rules":
                return {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "blocks": [
                                {
                                    "type": "result",
                                    "view": "rules",
                                    "result_ref": result["result_ref"],
                                }
                            ]
                        }
                    ),
                }
        pytest.fail("Model did not receive a rule result")

    first = accept_turn(db, AUTH, session, uid(), "甲企业2026年9月发票用途有什么要求？")
    model = ScriptedModel(
        [
            tool("get_rules", {"company_id": "C001", "period": "2026-09", "topics": ["purpose"]}),
            select_latest_rules,
            select_latest_rules,
            select_latest_rules,
        ]
    )
    runtime = Runtime(db, model_settings(settings), model)
    await runtime.run(runtime.claim())
    assert read_turn(db, AUTH, first["turn_id"])["status"] == "completed"
    followup = accept_turn(db, AUTH, session, uid(), "为什么？")
    execution = runtime.claim()
    await runtime.run(execution)
    result = read_turn(db, AUTH, followup["turn_id"])
    assert result["status"] == "completed", result
    assert "2.0版" in result["final_message"]
    with db.transaction(readonly=True) as conn:
        calls = conn.execute(
            "SELECT name,state FROM tool_executions WHERE run_id=%s", (execution.run_id,)
        ).fetchall()
    assert calls == [{"name": "get_rules", "state": "done"}]


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
    from app.request_evidence import bind_scope

    with db.transaction() as conn:
        scope = bind_scope(conn, execution, "C001", "2026-09")
    checked, _ = executor.execute(
        execution,
        tool(
            "check_purposes",
            {"company_id": "C001", "period": "2026-09", "scope_ref": scope["scope_ref"]},
        )["tool_calls"][0],
    )
    assert checked["data"]["missing_count"] == 5
    with db.transaction(readonly=True) as conn:
        invoice = conn.execute(
            "SELECT revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
        ).fetchone()
    prepared, _ = executor.execute(
        execution,
        tool(
            "prepare_invoice_write",
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
    assert prepared["ok"] and prepared["data"]["status"] == "authorized", prepared
    saved, _ = executor.execute(
        execution, tool("update_invoice", {"plan_id": prepared["data"]["plan_id"]})["tool_calls"][0]
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
