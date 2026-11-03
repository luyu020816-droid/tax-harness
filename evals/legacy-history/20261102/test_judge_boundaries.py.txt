"""Raw protocol counterexamples: no answer migration or automatic result selection."""

import json

import pytest
from test_answer_renderer import recorded, rendered
from test_history_retrieval import complete_fixture_reply, in_same_session
from test_runtime import model_settings, tool
from test_trusted_writes import TEXT, candidate

from app import context, invoice_write
from app.errors import AppError
from app.tools import ToolExecutor
from app.values import canonical


class ExactModel:
    """Only returns the supplied wire messages, including intentionally bad ones."""

    mode = "raw-protocol-test"

    def __init__(self, steps):
        self.steps = iter(steps)
        self.requests = []

    async def complete(self, payload):
        self.requests.append(payload)
        step = next(self.steps)
        return {"message": step(payload), "usage": None, "finish_reason": "stop"}


def raw_final(blocks):
    return {"role": "assistant", "content": canonical({"blocks": blocks, "status": "completed"})}


def monthly_results(db, settings, execution):
    initial, _, _ = context.assemble(db, model_settings(settings), execution, [])
    state = json.loads(initial["messages"][0]["content"].split("\n正式状态：\n", 1)[1])
    messages = []
    outputs = []
    refs = []
    executor = ToolExecutor(db)
    for period in ("2026-08", "2026-09"):
        binding = next(
            b for b in state["request_frame"]["available_scope_refs"] if b["period"] == period
        )
        call = tool(
            "query_month",
            {"company_id": "C001", "period": period, "scope_ref": binding["scope_ref"]},
        )["tool_calls"][0]
        output, ref = executor.execute(execution, call)
        assert output["ok"], output
        messages.extend(
            [
                {"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "tool", "tool_call_id": call["id"], "content": canonical(output)},
            ]
        )
        outputs.append(output)
        refs.append(ref)
    group = {
        "messages": messages,
        "results": outputs,
        "sources": refs,
        "memory_epoch": execution.expected_epoch,
    }
    payload, manifest, _ = context.assemble(db, model_settings(settings), execution, [group])
    return outputs, payload, manifest, group


def test_two_successful_months_cannot_silently_deliver_only_one(db, settings, execution_factory):
    ex = execution_factory("查询甲企业2026年8月和9月发票总额。")
    outputs, payload, manifest, _ = monthly_results(db, settings, ex)
    with pytest.raises(AppError) as error:
        rendered(
            db,
            ex,
            payload,
            manifest,
            [
                {
                    "type": "result",
                    "result_ref": outputs[-1]["result_ref"],
                    "view": "monthly_summary",
                }
            ],
        )
    assert error.value.code == "REPLY_EVIDENCE_MISSING"
    assert "2026-08" in str(error.value.details)


def test_lost_context_group_does_not_erase_executed_delivery_obligation(
    db, settings, execution_factory
):
    ex = execution_factory("查询甲企业2026年8月和9月发票总额。")
    outputs, _, _, group = monthly_results(db, settings, ex)
    recent = {
        **group,
        "messages": group["messages"][-2:],
        "results": group["results"][-1:],
        "sources": group["sources"][-1:],
    }
    payload, manifest, _ = context.assemble(db, model_settings(settings), ex, [recent])
    with pytest.raises(AppError) as error:
        rendered(
            db,
            ex,
            payload,
            manifest,
            [
                {
                    "type": "result",
                    "result_ref": outputs[-1]["result_ref"],
                    "view": "monthly_summary",
                }
            ],
        )
    assert error.value.code == "REPLY_EVIDENCE_MISSING"


def test_committed_write_cannot_be_hidden_by_unrelated_explanation(db, settings, execution_factory):
    ex = execution_factory(TEXT)
    plan = invoice_write.prepare(db, ex.auth, **candidate(ex))
    output, payload, manifest = recorded(
        db, settings, ex, "update_invoice", {"plan_id": plan["plan_id"]}
    )
    with db.transaction(readonly=True) as conn:
        actual = conn.execute(
            "SELECT purpose,revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
        ).fetchone()
    assert actual == {"purpose": "办公耗材采购", "revision": 2}
    with pytest.raises(AppError) as error:
        rendered(db, ex, payload, manifest, [{"type": "explanation", "code": "independent_totals"}])
    assert error.value.code == "REPLY_EVIDENCE_MISSING"
    assert str(output["data"]["operation_id"]) in str(error.value.details)


def test_saved_preference_cannot_be_hidden_by_unrelated_explanation(
    db, settings, execution_factory
):
    text = "以后回答请简洁，请记住这个偏好。"
    ex = execution_factory(text)
    output, payload, manifest = recorded(
        db,
        settings,
        ex,
        "manage_preference",
        {
            "action": "remember",
            "key": "response_style",
            "value": "简洁",
            "source_refs": [{"source_id": ex.source_id, "quote": text}],
        },
    )
    with pytest.raises(AppError) as error:
        rendered(db, ex, payload, manifest, [{"type": "explanation", "code": "independent_totals"}])
    assert error.value.code == "REPLY_EVIDENCE_MISSING"
    assert str(output["data"]["preference_id"]) in str(error.value.details)


@pytest.mark.parametrize("message", ["继续下一页", "再下一页，仍保持刚才的范围。"])
def test_missing_cursor_cannot_restart_published_history_page(
    db, settings, source_factory, admin, execution_factory, message
):
    for i in range(7):
        source_factory(f"独立原话{i}")
    admin.execute("UPDATE sources SET created_at='2026-09-03 10:00+08'")
    first = execution_factory("列出甲企业9月历史第一页")
    args = {"company_id": "C001", "date_from": "2026-09-01", "date_to": "2026-10-01"}
    original, payload, manifest = recorded(db, settings, first, "search_history", args)
    reply = rendered(
        db,
        first,
        payload,
        manifest,
        [{"type": "result", "result_ref": original["result_ref"], "view": "history_page"}],
    )
    complete_fixture_reply(db, first, reply)
    following = in_same_session(db, execution_factory(message), first.session_id)
    call = tool("search_history", args)["tool_calls"][0]
    actual, _ = ToolExecutor(db).execute(following, call)
    assert not actual["ok"], "Omitted cursor silently replayed the first page"
    assert actual["error"]["code"] == "HISTORY_CONTINUATION_REQUIRED"
    assert actual["error"]["details"]["arguments"]["cursor"] == original["data"]["query_id"]


@pytest.mark.asyncio
async def test_runtime_repairs_omitted_month_without_test_adapter_selecting_results(
    db, settings, execution_factory
):
    from app.runtime import Runtime

    ex = execution_factory("查询甲企业2026年8月和9月发票总额。")

    def query(payload):
        state = json.loads(payload["messages"][0]["content"].split("\n正式状态：\n", 1)[1])
        calls = [
            tool("query_month", {k: b[k] for k in ("company_id", "period", "scope_ref")})[
                "tool_calls"
            ][0]
            for b in state["request_frame"]["available_scope_refs"]
        ]
        return {"role": "assistant", "content": None, "tool_calls": calls}

    def select(payload, all_results):
        outputs = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
        outputs = [
            o for o in outputs if o.get("ok") and o.get("data", {}).get("kind") == "monthly_summary"
        ]
        return raw_final(
            [
                {"type": "result", "result_ref": o["result_ref"], "view": "monthly_summary"}
                for o in (outputs if all_results else outputs[-1:])
            ]
        )

    model = ExactModel([query, lambda p: select(p, False), lambda p: select(p, True)])
    await Runtime(db, model_settings(settings), model).run(ex)
    from app.turns import read_turn

    result = read_turn(db, ex.auth, ex.turn_id)
    assert result["status"] == "completed" and len(model.requests) == 3
    assert {c["period"] for c in result["final_cards"]} == {"2026-08", "2026-09"}
    assert "9046.35" in result["final_message"] and "9622.06" in result["final_message"]
    assert any(
        "REPLY_EVIDENCE_MISSING" in m.get("content", "")
        for m in model.requests[-1]["messages"]
        if m.get("role") == "system"
    )


@pytest.mark.asyncio
async def test_runtime_cursor_feedback_repairs_page_and_does_not_repeat_sources(
    db, settings, source_factory, admin, execution_factory
):
    from app.runtime import Runtime
    from app.turns import read_turn

    for i in range(7):
        source_factory(f"续页控制原话{i}")
    admin.execute("UPDATE sources SET created_at='2026-09-03 10:00+08'")
    args = {"company_id": "C001", "date_from": "2026-09-01", "date_to": "2026-10-01"}
    first = execution_factory("列出甲企业9月原话")
    original, payload, manifest = recorded(db, settings, first, "search_history", args)
    first_reply = rendered(
        db,
        first,
        payload,
        manifest,
        [{"type": "result", "result_ref": original["result_ref"], "view": "history_page"}],
    )
    complete_fixture_reply(db, first, first_reply)
    ex = in_same_session(db, execution_factory("继续下一页"), first.session_id)

    def repair(payload):
        output = json.loads(
            next(m["content"] for m in reversed(payload["messages"]) if m["role"] == "tool")
        )
        assert output["error"]["code"] == "HISTORY_CONTINUATION_REQUIRED"
        return tool("search_history", output["error"]["details"]["arguments"])

    def finish(payload):
        output = json.loads(
            next(m["content"] for m in reversed(payload["messages"]) if m["role"] == "tool")
        )
        assert output["ok"]
        return raw_final(
            [{"type": "result", "result_ref": output["result_ref"], "view": "history_page"}]
        )

    model = ExactModel([lambda p: tool("search_history", args), repair, finish])
    await Runtime(db, model_settings(settings), model).run(ex)
    result = read_turn(db, ex.auth, ex.turn_id)
    assert result["status"] == "completed" and len(model.requests) == 3
    old = {c["source_id"] for c in first_reply.cards if c["kind"] == "history_quote"}
    new = {c["source_id"] for c in result["final_cards"] if c["kind"] == "history_quote"}
    assert len(old) == 5 and len(new) == 2 and not old & new


def test_complete_results_are_accepted_and_not_arbitrarily_expanded(
    db, settings, execution_factory
):
    ex = execution_factory("查询甲企业2026年8月和9月发票总额。")
    outputs, payload, manifest, _ = monthly_results(db, settings, ex)
    reply = rendered(
        db,
        ex,
        payload,
        manifest,
        [
            {"type": "result", "result_ref": o["result_ref"], "view": "monthly_summary"}
            for o in outputs
        ],
    )
    assert len(reply.cards) == 2 and {c["period"] for c in reply.cards} == {"2026-08", "2026-09"}


@pytest.mark.parametrize(
    "text", ["解释“下一页”是什么意思", "不要翻下一页", "重新查第一页", "下一页，改查2026年8月"]
)
def test_page_control_does_not_treat_quotes_negation_or_scope_change_as_continuation(
    db, execution_factory, text
):
    from app.history_retrieval import continuation_in

    ex = execution_factory(text)
    with db.transaction(readonly=True) as conn:
        assert continuation_in(conn, ex) is None


def test_expired_navigation_cannot_fall_back_to_first_page(
    db, settings, source_factory, admin, execution_factory
):
    from app.history_retrieval import continuation_in
    from app.preferences import change_preference
    from app.values import uid

    source_factory("旧页原话")
    admin.execute("UPDATE sources SET created_at='2026-09-03 10:00+08'")
    first = execution_factory("查甲企业九月历史")
    output, payload, manifest = recorded(
        db,
        settings,
        first,
        "search_history",
        {"company_id": "C001", "date_from": "2026-09-01", "date_to": "2026-10-01"},
    )
    reply = rendered(
        db,
        first,
        payload,
        manifest,
        [{"type": "result", "result_ref": output["result_ref"], "view": "history_page"}],
    )
    complete_fixture_reply(db, first, reply)
    change_preference(
        db, first.auth, action="remember", key="response_style", value="简洁", request_id=uid()
    )
    ex = in_same_session(db, execution_factory("继续下一页"), first.session_id)
    with db.transaction(readonly=True) as conn, pytest.raises(AppError) as error:
        continuation_in(conn, ex)
    assert error.value.code == "SOURCE_INVALIDATED"


@pytest.mark.parametrize("action", ["delete", "update"])
def test_preference_change_preserves_unrelated_same_word_business_history(
    db, settings, source_factory, admin, action
):
    from app.history_retrieval import HistoryRetrieval
    from app.identity import Identity
    from app.preferences import change_preference
    from app.retrieval_engine import LocalRetrieval
    from app.sources import read_source
    from app.values import uid

    auth = Identity("U001")
    preference = change_preference(
        db,
        auth,
        scope="C001",
        action="remember",
        key="response_style",
        value="简洁",
        request_id=uid(),
    )
    business = source_factory("乙企业项目名称为简洁办公，档案在东侧柜。", company="C002")
    admin.execute(
        "UPDATE sources SET created_at='2026-09-03 10:00+08' WHERE source_id=%s",
        (business["source_id"],),
    )
    change_preference(
        db,
        auth,
        scope="C001",
        action=action,
        target_id=str(preference["preference_id"]),
        expected_revision=preference["revision"],
        value="详细" if action == "update" else None,
        request_id=uid(),
    )
    with db.transaction(readonly=True) as conn:
        current = read_source(conn, auth, str(business["source_id"]), company_id="C002")
        assert (
            current["content_hash"] == business["content_hash"]
            and current["recall_state"] == "active"
        )
        old = conn.execute(
            "SELECT recall_state FROM sources WHERE source_id=%s",
            (preference["source_refs"][0]["source_id"],),
        ).fetchone()
        assert old["recall_state"] == "suppressed"
    engine = LocalRetrieval(settings.knowledge_model_directory)
    try:
        found = HistoryRetrieval(db, settings, engine).search(
            auth, company_id="C002", date_from="2026-09-03", date_to="2026-09-04"
        )
        assert any(e["source_id"] == str(business["source_id"]) for e in found["evidence"])
    finally:
        engine.close()
