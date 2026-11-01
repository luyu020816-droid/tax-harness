"""Official advanced challenges: evidence retention, state changes and recovery."""

import asyncio
import json

import pytest
from test_compaction import closed_group
from test_runtime import ScriptedModel, model_settings

from app import business, context, invoice_write, rules
from app.compaction import compact
from app.errors import AppError
from app.experiment_metrics import aggregate, usage_cost
from app.identity import Identity, create_session, switch_company
from app.preferences import change_preference, list_preferences
from app.runtime import Runtime
from app.summary_evidence import unpack
from app.turns import accept_turn, read_turn, retry_turn
from app.values import uid

AUTH = Identity("U001")


def lossy(payload):
    data = json.loads(payload["messages"][-1]["content"])
    return {
        "content": json.dumps(
            {"summary": "用户提供了项目背景。", "covered_sources": data["source_ids"]}
        )
    }


@pytest.mark.asyncio
async def test_lossy_navigation_cannot_remove_negation_ids_and_unfinished_work(
    db, settings, source_factory, execution_factory, admin
):
    text = "甲企业样品只允许放在西侧柜，不得外发。柜号为Q7421。范围改为2026年9月，不是8月。F002仍待确认，不授权保存。"
    source = source_factory(text)
    ex = execution_factory("继续查询")
    await compact(
        db, model_settings(settings), ScriptedModel([lossy]), ex, [closed_group(db, source)], 1
    )
    row = admin.execute("SELECT * FROM summaries WHERE status='active'").fetchone()
    packet = unpack(row["content"])
    assert packet["navigation"] == "用户提供了项目背景。"
    quotes = "".join(u["quote"] for u in packet["protected_excerpts"])
    assert quotes == text
    payload, manifest, _ = context.assemble(db, model_settings(settings), ex, [])
    assert manifest["summary_id"] == str(row["summary_id"])
    assert "Q7421" in payload["messages"][1]["content"]
    assert "不授权保存" in payload["messages"][1]["content"]


@pytest.mark.asyncio
async def test_missing_protected_sentence_makes_summary_unusable(
    db, settings, source_factory, execution_factory, admin
):
    source = source_factory("柜号Q7421，不得外发。尚有两项未完成。")
    ex = execution_factory("继续")
    await compact(
        db, model_settings(settings), ScriptedModel([lossy]), ex, [closed_group(db, source)], 1
    )
    packet = unpack(
        admin.execute("SELECT content FROM summaries WHERE status='active'").fetchone()["content"]
    )
    packet["protected_excerpts"].pop()
    admin.execute("UPDATE summaries SET content=%s WHERE status='active'", (json.dumps(packet),))
    _, manifest, _ = context.assemble(db, model_settings(settings), ex, [])
    assert manifest["summary_id"] is None


@pytest.mark.asyncio
async def test_protected_evidence_over_budget_is_not_silently_truncated(
    db, settings, source_factory, execution_factory, admin
):
    source = source_factory("不可丢弃的不同原话" * 1000)
    with pytest.raises(AppError) as error:
        await compact(
            db,
            model_settings(settings),
            ScriptedModel([]),
            execution_factory("继续"),
            [closed_group(db, source)],
            1,
        )
    assert error.value.code == "CONTEXT_CAPACITY_EXCEEDED"
    assert not admin.execute("SELECT 1 FROM summaries WHERE status='active'").fetchone()


def test_edit_preference_switch_company_change_invoice_then_replace_rule(
    db, admin, execution_factory
):
    assert business.check_purposes(db, AUTH, "C001", "2026-09")["missing_count"] == 5
    pref = change_preference(
        db, AUTH, action="remember", key="response_style", value="简洁", request_id=uid()
    )
    pref = change_preference(
        db,
        AUTH,
        action="update",
        target_id=str(pref["preference_id"]),
        expected_revision=1,
        value="详细",
        request_id=uid(),
    )
    ex = execution_factory("甲企业F002用途填办公资料整理")
    with db.transaction(readonly=True) as conn:
        source = conn.execute(
            "SELECT * FROM sources WHERE source_id=%s", (ex.source_id,)
        ).fetchone()
    plan = invoice_write.prepare(
        db,
        AUTH,
        company_id="C001",
        invoice_id="F002",
        patch={"purpose": "办公资料整理"},
        expected_revision=1,
        source_id=ex.source_id,
        source_refs=[{"source_id": ex.source_id, "quote": source["content"]}],
        execution=ex,
    )
    saved = invoice_write.commit(db, AUTH, str(plan["plan_id"]), execution=ex)
    assert saved["verification_state"] == "verified"
    assert business.check_purposes(db, AUTH, "C001", "2026-09")["missing_count"] == 4
    switch_company(db, AUTH, ex.session_id, "C002")
    assert business.check_purposes(db, AUTH, "C002", "2026-09")["missing_count"] == 3

    change_preference(
        db,
        AUTH,
        action="delete",
        target_id=str(pref["preference_id"]),
        expected_revision=2,
        request_id=uid(),
    )
    assert not list_preferences(db, AUTH)
    old = admin.execute(
        "SELECT rule_version_id FROM rule_versions WHERE rule_code='R002' AND status='active'"
    ).fetchone()
    condition = {"kind": "purpose_threshold", "operator": "gte", "threshold": "1000.00"}
    with admin.transaction():
        new = rules.create_version(
            admin,
            {
                "rule_code": "R002",
                "display_name": "甲企业发票用途规则",
                "business_version": "3.0",
                "effective_from": "2026-09-01",
                "companies": ["C001"],
                "condition_json": condition,
                "full_text": rules.template(condition),
            },
        )
        rules.activate(admin, str(new["rule_version_id"]), str(old["rule_version_id"]))
    checked = business.check_purposes(db, AUTH, "C001", "2026-09")
    assert checked["missing_count"] == 1
    assert checked["rule_refs"][0]["business_version"] == "3.0"
    assert business.check_purposes(db, AUTH, "C002", "2026-09")["missing_count"] == 3


@pytest.mark.asyncio
async def test_summary_read_checks_exact_quote_scope_epoch_and_source(
    db, settings, source_factory, execution_factory, admin
):
    source = source_factory("柜号Q7421，不得外发。")
    execution = execution_factory("甲企业之前柜号是多少？")
    runtime = Runtime(db, model_settings(settings), ScriptedModel([lossy]))
    await compact(db, runtime.settings, runtime.model, execution, [closed_group(db, source)], 1)
    found = runtime.tools.history.read_summary(
        execution, str(source["source_id"]), "柜号Q7421，不得外发。"
    )
    assert found["retrieval_mode"] == "summary_source_read"
    assert found["matches"][0]["content"] == "柜号Q7421，不得外发。"
    with pytest.raises(AppError) as error:
        runtime.tools.history.read_summary(execution, str(source["source_id"]), "可以外发")
    assert error.value.code == "SOURCE_INVALIDATED"
    admin.execute("UPDATE summaries SET memory_epoch=memory_epoch-1")
    with pytest.raises(AppError) as error:
        runtime.tools.history.read_summary(execution, str(source["source_id"]), "柜号Q7421")
    assert error.value.code == "SOURCE_INVALIDATED"


@pytest.mark.asyncio
async def test_timeout_resume_reads_committed_operation_before_model_without_new_save(
    db, settings, admin
):
    class CommitThenDelay(Runtime):
        async def _loop(self, execution):
            plan = invoice_write.prepare(
                db,
                AUTH,
                company_id="C001",
                invoice_id="F002",
                patch={"purpose": "办公资料整理"},
                expected_revision=1,
                source_id=execution.source_id,
                source_refs=[
                    {"source_id": execution.source_id, "quote": "甲企业F002用途填办公资料整理"}
                ],
                execution=execution,
            )
            invoice_write.commit(db, AUTH, str(plan["plan_id"]), execution=execution)
            await asyncio.sleep(5)

    session = str(create_session(db, AUTH, "C001")["session_id"])
    turn = accept_turn(db, AUTH, session, uid(), "甲企业F002用途填办公资料整理")
    # The shortened deadline is only fault injection; production minimum stays 10s.
    actual = model_settings(settings)
    slow = CommitThenDelay(db, actual.model_copy(update={"turn_timeout": 0.5}), ScriptedModel([]))
    old = slow.claim()
    await slow.run(old)
    assert read_turn(db, AUTH, turn["turn_id"])["status"] == "failed"
    request_id = uid()
    retry_turn(db, AUTH, turn["turn_id"], request_id, old.generation)
    assert retry_turn(db, AUTH, turn["turn_id"], request_id, old.generation)["status"] == "queued"
    with pytest.raises(AppError):
        retry_turn(db, Identity("U002"), turn["turn_id"], uid(), old.generation)

    def reply(payload):
        tools = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
        assert tools and tools[0]["data"]["verification_state"] == "verified"
        return {
            "content": json.dumps(
                {
                    "blocks": [
                        {"type": "operation", "operation_id": tools[0]["data"]["operation_id"]}
                    ],
                    "status": "completed",
                }
            )
        }

    def contradictory(payload):
        message = reply(payload)
        plan = json.loads(message["content"])
        plan["blocks"].append({"type": "explanation", "code": "no_changes"})
        return {"content": json.dumps(plan)}

    def corrected(payload):
        feedback = next(
            m["content"] for m in reversed(payload["messages"]) if m["role"] == "system"
        )
        assert "drop_explanation_codes" in feedback and "keep_operation_blocks" in feedback
        return reply(payload)

    model = ScriptedModel([contradictory, corrected])
    resumed = Runtime(db, actual, model)
    new = resumed.claim()
    assert new.source_id == old.source_id and new.generation > old.generation
    await resumed.run(new)
    assert read_turn(db, AUTH, turn["turn_id"])["status"] == "completed"
    assert admin.execute("SELECT count(*) AS n FROM operations").fetchone()["n"] == 1
    assert (
        admin.execute(
            "SELECT revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
        ).fetchone()["revision"]
        == 2
    )
    assert admin.execute(
        "SELECT name FROM tool_executions WHERE run_id=%s", (new.run_id,)
    ).fetchall() == [{"name": "get_invoice_operation"}]
    with pytest.raises(AppError) as error:
        retry_turn(db, AUTH, turn["turn_id"], uid(), old.generation)
    assert error.value.code == "VERSION_CONFLICT"


def test_cost_accounts_for_cache_retries_and_unknown_usage():
    from decimal import Decimal

    price = {
        "model": "m",
        "input_cache_hit_per_million": "0.003",
        "input_cache_miss_per_million": "0.15",
        "output_per_million": "0.6",
    }
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "prompt_cache_hit_tokens": 800,
        "prompt_cache_miss_tokens": 200,
    }
    observed = usage_cost(usage, price, "m")
    assert Decimal(observed["usd"]) == Decimal("0.0000924")
    assert usage_cost({**usage, "prompt_cache_hit_tokens": 1001}, price, "m")["usd"] is None
    assert usage_cost(None, price, "m")["usd"] is None
    assert usage_cost(usage, price, "another-model")["usd"] is None
    result = aggregate(
        [
            {
                "correct": False,
                "total_ms": 42,
                "tool_calls": 1,
                "calls": [
                    {"usage": usage, "cost": observed},
                    {"usage": None, "cost": usage_cost(None, price, "m")},
                ],
            }
        ]
    )
    assert result["model_attempts"] == 2 and result["missing_usage_calls"] == 1
    assert result["known_cost_usd"] == observed["usd"] and result["estimated_cost_usd"] is None


def test_retry_api_keeps_original_turn_and_checks_identity(db, settings, admin):
    from fastapi.testclient import TestClient

    from app.api import create_app

    session = str(create_session(db, AUTH, "C001")["session_id"])
    turn = accept_turn(db, AUTH, session, uid(), "查询甲企业2026年9月资料")
    runtime = Runtime(db, model_settings(settings), ScriptedModel([]))
    execution = runtime.claim()
    runtime.fail(execution, AppError("MODEL_UNAVAILABLE", "连接超时", 503, True))
    with TestClient(create_app(settings=settings, db=db, start_workers=False)) as client:
        url = f"/api/v1/turns/{turn['turn_id']}/retry"
        payload = {"request_id": uid(), "expected_generation": execution.generation}
        assert client.post(url, json=payload).status_code == 401
        client.post("/api/v1/demo/login", json={"user_id": "U002"})
        assert client.post(url, json=payload).status_code == 404
        client.post("/api/v1/demo/login", json={"user_id": "U001"})
        assert client.post(url, json={"request_id": uid()}).status_code == 422
        first = client.post(url, json=payload)
        assert first.status_code == 202 and first.json()["turn_id"] == turn["turn_id"]
        assert client.post(url, json=payload).json()["status"] == "queued"
        assert (
            client.post(
                url, json={**payload, "expected_generation": execution.generation + 1}
            ).status_code
            == 409
        )
    assert admin.execute("SELECT count(*) AS n FROM turns").fetchone()["n"] == 1


def test_context_ablation_cannot_call_disabled_search(db, settings, execution_factory):
    from test_runtime import tool

    actual = model_settings(settings).model_copy(
        update={"context_history_search": False, "context_use_summary": False}
    )
    runtime = Runtime(db, actual, ScriptedModel([]))
    execution = execution_factory("甲企业过去的用途是什么？")
    payload, manifest, _ = context.assemble(db, actual, execution, [])
    assert manifest["context_policy"]["history_search"] is False
    names = {d["function"]["name"] for d in payload["tools"]}
    assert "search_history" not in names and "read_summary_source" not in names
    output, _ = runtime.tools.execute(
        execution, tool("search_history", {"company_id": "C001", "query": "用途"})["tool_calls"][0]
    )
    assert not output["ok"] and output["error"]["code"] == "TOOL_DISABLED"


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_single_wrapper_summary_labels_still_require_exact_known_coverage(
    db, settings, source_factory, execution_factory, unknown
):
    source = source_factory("柜号Q7421，不得外发。")
    execution = execution_factory("继续")

    def wrapped(payload):
        data = json.loads(payload["messages"][-1]["content"])
        labels = ["unknown"] if unknown else data["source_ids"]
        return {"content": json.dumps({"summary": "只记录背景。", "covered_sources": [labels]})}

    candidate = compact(
        db,
        model_settings(settings),
        ScriptedModel([wrapped]),
        execution,
        [closed_group(db, source)],
        1,
    )
    if unknown:
        with pytest.raises(AppError) as error:
            await candidate
        assert error.value.code == "SUMMARY_INVALID"
    else:
        assert await candidate
