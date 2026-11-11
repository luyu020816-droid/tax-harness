"""Regression for explicit-month check followed by two independent saves."""

import json

import pytest
from test_judge_boundaries import ExactModel
from test_runtime import ScriptedModel, final, model_settings, tool

from app.errors import AppError
from app.identity import Identity, create_session
from app.request_evidence import require_period
from app.runtime import Runtime
from app.turns import accept_turn, read_turn
from app.values import canonical, uid


def _state(payload):
    return json.loads(payload["messages"][0]["content"].split("\n正式状态：\n", 1)[1])


def _prepare(invoice_id, text):
    def respond(payload):
        return tool(
            "prepare_invoice_write",
            {
                "company_id": "C001",
                "invoice_id": invoice_id,
                "expected_revision": 1,
                "patch": {"purpose": "测试用途"},
                "source_refs": [
                    {"source_id": _state(payload)["current_source"]["source_id"], "quote": text}
                ],
            },
        )

    return respond


def _save(payload):
    result = json.loads(payload["messages"][-1]["content"])
    assert result["ok"] and result["data"]["status"] == "authorized"
    return tool("update_invoice", {"plan_id": result["data"]["plan_id"]})


def _respond_with_receipt_and_remaining_task(payload):
    outputs = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
    output = next(
        item for item in reversed(outputs) if item.get("data", {}).get("kind") == "operation"
    )
    operation = output["data"]
    task = _state(payload)["task"]
    return {
        "role": "assistant",
        "content": canonical(
            {
                "blocks": [
                    {"type": "operation", "operation_id": operation["operation_id"]},
                    {
                        "type": "result",
                        "result_ref": operation["post_save_check"]["result_ref"],
                        "view": "purpose_check",
                    },
                ],
                "status": "waiting_user",
                "clarification": {
                    "intent": "complete_purposes",
                    "company_id": "C001",
                    "period": "2026-09",
                    "missing_fields": ["purpose"],
                    "task_id": task["task_id"],
                    "task_generation": task["generation"],
                },
                "resume_turn_id": None,
            }
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt_recheck", [None, "conflicting_period", "wrong_source"])
async def test_same_session_two_saves_keep_verified_month_and_deliver_receipts(
    db, settings, admin, corrupt_recheck
):
    auth = Identity("U001")
    config = model_settings(settings)
    earlier_session = str(create_session(db, auth, "C001")["session_id"])
    earlier = accept_turn(db, auth, earlier_session, uid(), "查询甲企业2026年8月发票总额")
    runtime = Runtime(
        db,
        config,
        ScriptedModel(
            [tool("query_month", {"company_id": "C001", "period": "2026-08"}), final("查询完成。")]
        ),
    )
    await runtime.run(runtime.claim())
    assert read_turn(db, auth, earlier["turn_id"])["status"] == "completed"
    earlier = accept_turn(db, auth, earlier_session, uid(), "9月的票是不是都得填东西？")
    runtime = Runtime(
        db,
        config,
        ScriptedModel(
            [
                tool("check_purposes", {"company_id": "C001", "period": "2026-09"}),
                final(
                    "还有发票需要补充用途。",
                    status="waiting_user",
                    clarification={
                        "intent": "complete_purposes",
                        "company_id": "C001",
                        "period": "2026-09",
                        "missing_fields": ["purpose"],
                    },
                ),
            ]
        ),
    )
    await runtime.run(runtime.claim())
    assert read_turn(db, auth, earlier["turn_id"])["status"] == "waiting_user"
    session_id = str(create_session(db, auth, "C001")["session_id"])
    initial = accept_turn(db, auth, session_id, uid(), "检查甲企业2026年9月发票用途")
    model = ScriptedModel(
        [
            tool("check_purposes", {"company_id": "C001", "period": "2026-09"}),
            final(
                "还有发票需要补充用途。",
                status="waiting_user",
                clarification={
                    "intent": "complete_purposes",
                    "company_id": "C001",
                    "period": "2026-09",
                    "missing_fields": ["purpose"],
                },
            ),
        ]
    )
    runtime = Runtime(db, config, model)
    await runtime.run(runtime.claim())
    assert read_turn(db, auth, initial["turn_id"])["status"] == "waiting_user"
    previous_turn_id = None
    for invoice_id in ("F002", "F004"):
        text = f"把甲企业{invoice_id}用途补为测试用途"
        if invoice_id == "F004" and corrupt_recheck:
            if corrupt_recheck == "conflicting_period":
                admin.execute(
                    "UPDATE tool_executions SET arguments=jsonb_set(arguments,'{period}',to_jsonb('2026-08'::text)) "
                    "WHERE name='check_purposes' AND run_id IN (SELECT run_id FROM runs WHERE turn_id=%s)",
                    (previous_turn_id,),
                )
            else:
                source = admin.execute(
                    "SELECT source_id FROM sources WHERE turn_id=%s AND role='user'",
                    (previous_turn_id,),
                ).fetchone()["source_id"]
                admin.execute(
                    "UPDATE tool_executions SET result_source_id=%s WHERE name='check_purposes' "
                    "AND run_id IN (SELECT run_id FROM runs WHERE turn_id=%s)",
                    (source, previous_turn_id),
                )
        turn = accept_turn(db, auth, session_id, uid(), text)
        steps = [_prepare(invoice_id, text), _save]
        if invoice_id == "F004":
            # Match the observed initial malformed model reply before a valid one.
            steps.append(lambda _: {"role": "assistant", "content": " "})
        steps += [_respond_with_receipt_and_remaining_task] * 3
        runtime = Runtime(db, config, ExactModel(steps))
        execution = runtime.claim()
        if invoice_id == "F004" and corrupt_recheck:
            with db.transaction(readonly=True) as conn, pytest.raises(AppError) as error:
                require_period(conn, execution, "C001", "2026-09")
            assert error.value.code == "NEEDS_SCOPE_CLARIFICATION"
            assert (
                admin.execute(
                    "SELECT count(*) n FROM operations WHERE state='committed'"
                ).fetchone()["n"]
                == 1
            )
            return
        await runtime.run(execution)
        result = read_turn(db, auth, turn["turn_id"])
        assert result["status"] == "waiting_user", result["error"]
        assert any(
            card["kind"] == "operation" and card["invoice_id"] == invoice_id
            for card in result["final_cards"]
        )
        previous_turn_id = turn["turn_id"]
    assert (
        admin.execute("SELECT count(*) n FROM operations WHERE state='committed'").fetchone()["n"]
        == 2
    )
    assert (
        admin.execute(
            "SELECT revision FROM invoices WHERE company_id='C001' AND invoice_id='F004'"
        ).fetchone()["revision"]
        == 2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_kind", ["invalid_json", "missing_receipt"])
async def test_committed_save_is_visible_even_when_all_final_replies_fail(
    db, settings, admin, reply_kind
):
    auth = Identity("U001")
    session_id = str(create_session(db, auth, "C001")["session_id"])
    text = "把甲企业F002用途补为测试用途"
    turn = accept_turn(db, auth, session_id, uid(), text)

    def bad_reply(_):
        content = (
            " "
            if reply_kind == "invalid_json"
            else canonical(
                {"blocks": [{"type": "explanation", "code": "no_changes"}], "status": "completed"}
            )
        )
        return {"role": "assistant", "content": content}

    runtime = Runtime(
        db, model_settings(settings), ExactModel([_prepare("F002", text), _save, *[bad_reply] * 3])
    )
    await runtime.run(runtime.claim())
    result = read_turn(db, auth, turn["turn_id"])
    assert result["status"] == "failed"
    assert result["error"]["code"] == (
        "MODEL_PROTOCOL_UNSUPPORTED" if reply_kind == "invalid_json" else "REPLY_EVIDENCE_MISSING"
    )
    assert "已保存并核验" in result["final_message"]
    assert "不表示本轮其他事项已完成" in result["final_message"]
    assert result["final_cards"][0]["invoice_id"] == "F002"
    assert (
        admin.execute("SELECT count(*) n FROM operations WHERE state='committed'").fetchone()["n"]
        == 1
    )
    assert (
        admin.execute(
            "SELECT revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
        ).fetchone()["revision"]
        == 2
    )
