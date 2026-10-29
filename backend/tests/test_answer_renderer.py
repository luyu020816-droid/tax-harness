import pytest
from pydantic import ValidationError
from test_runtime import model_settings, tool

from app import context, invoice_write
from app.answer_renderer import render
from app.errors import AppError
from app.tool_schemas import FinalReply
from app.tools import ToolExecutor
from app.values import canonical, uid


def recorded(db, settings, execution, name, args):
    call = tool(name, args)["tool_calls"][0]
    output, ref = ToolExecutor(db).execute(execution, call)
    assert output["ok"], output
    group = {
        "messages": [
            {"role": "assistant", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call["id"], "content": canonical(output)},
        ],
        "sources": [ref],
        "results": [output],
    }
    payload, manifest, _ = context.assemble(db, model_settings(settings), execution, [group])
    return output, payload, manifest


def rendered(db, execution, payload, manifest, blocks, **kwargs):
    plan = FinalReply.model_validate({"blocks": blocks, **kwargs})
    with db.transaction() as conn:
        return render(conn, execution, plan, payload, manifest)


def test_free_message_cannot_bypass_program_facts():
    with pytest.raises(ValidationError):
        FinalReply.model_validate(
            {
                "message": "已保存，缺项只有4张",
                "blocks": [{"type": "explanation", "code": "no_changes"}],
            }
        )
    with pytest.raises(ValidationError):
        FinalReply.model_validate({"blocks": [{"type": "explanation", "code": "已保存"}]})


def test_count_and_amount_come_only_from_real_output(db, settings, execution_factory):
    ex = execution_factory("检查甲企业2026年9月发票用途")
    initial, _, _ = context.assemble(db, model_settings(settings), ex, [])
    import json

    state = json.loads(initial["messages"][0]["content"].split("\n正式状态：\n")[1])
    scope = state["request_frame"]["available_scope_refs"][0]["scope_ref"]
    output, payload, manifest = recorded(
        db,
        settings,
        ex,
        "check_purposes",
        {"company_id": "C001", "period": "2026-09", "scope_ref": scope},
    )
    reply = rendered(
        db,
        ex,
        payload,
        manifest,
        [{"type": "result", "result_ref": output["result_ref"], "view": "purpose_check"}],
    )
    assert "缺少用途5张" in reply.message
    assert reply.cards[0]["missing_count"] == 5
    with pytest.raises(AppError):
        rendered(
            db,
            ex,
            payload,
            manifest,
            [{"type": "result", "result_ref": uid(), "view": "purpose_check"}],
        )
    with pytest.raises(AppError):
        rendered(
            db,
            ex,
            payload,
            manifest,
            [{"type": "result", "result_ref": output["result_ref"], "view": "monthly_summary"}],
        )


def test_fabricated_operation_cannot_report_success(db, settings, execution_factory):
    ex = execution_factory("你好")
    payload, manifest, _ = context.assemble(db, model_settings(settings), ex, [])
    with pytest.raises(AppError) as error:
        rendered(db, ex, payload, manifest, [{"type": "operation", "operation_id": uid()}])
    assert error.value.code == "REPLY_EVIDENCE_MISSING"


@pytest.mark.parametrize(
    "ledger_state,verification,label",
    [
        ("committed", "verified", "已保存并核验"),
        ("committed", "pending", "已提交，核验待完成"),
        ("no_change", "verified", "当前已是该值，已核验"),
        ("rejected", "pending", "本项未保存"),
        ("prepared", "pending", "保存状态尚未确定"),
    ],
)
def test_receipts_distinguish_actual_commit_state(
    db, admin, settings, execution_factory, ledger_state, verification, label
):
    from test_trusted_writes import TEXT, candidate

    ex = execution_factory(TEXT)
    plan = invoice_write.prepare(db, ex.auth, **candidate(ex))
    output, payload, manifest = recorded(
        db, settings, ex, "update_invoice", {"plan_id": plan["plan_id"]}
    )
    operation_id = output["data"]["operation_id"]
    admin.execute(
        "UPDATE operations SET state=%s,verification_state=%s WHERE operation_id=%s",
        (ledger_state, verification, operation_id),
    )
    reply = rendered(
        db, ex, payload, manifest, [{"type": "operation", "operation_id": operation_id}]
    )
    assert label in reply.message
    assert reply.cards[0]["commit_state"] == ledger_state


def test_unrelated_tracking_question_is_not_a_schema_option():
    with pytest.raises(ValidationError):
        FinalReply.model_validate(
            {
                "blocks": [{"type": "explanation", "code": "no_changes"}],
                "status": "waiting_user",
                "clarification": {"intent": "other", "missing_fields": ["long_term_tracking"]},
            }
        )


def test_pure_clarification_is_allowed_but_empty_completion_is_not():
    assert (
        FinalReply.model_validate(
            {
                "blocks": [],
                "status": "waiting_user",
                "clarification": {"intent": "query_month", "missing_fields": ["period"]},
            }
        ).status
        == "waiting_user"
    )
    with pytest.raises(ValidationError):
        FinalReply.model_validate({"blocks": [], "status": "completed"})


def test_known_month_cannot_be_asked_again(db, settings, execution_factory):
    ex = execution_factory("查询甲企业2026年9月发票总额")
    payload, manifest, _ = context.assemble(db, model_settings(settings), ex, [])
    with pytest.raises(AppError):
        rendered(
            db,
            ex,
            payload,
            manifest,
            [{"type": "explanation", "code": "no_changes"}],
            status="waiting_user",
            clarification={"intent": "query_month", "missing_fields": ["period"]},
        )


def test_missing_invoice_error_is_valid_evidence_for_rejection(db, settings, execution_factory):
    ex = execution_factory("把甲企业不存在的F999用途改为办公耗材采购。")
    call = tool(
        "prepare_invoice_write",
        {
            "company_id": "C001",
            "invoice_id": "F999",
            "patch": {"purpose": "办公耗材采购"},
            "expected_revision": 1,
            "source_refs": [{"source_id": ex.source_id, "quote": "办公耗材采购"}],
        },
    )["tool_calls"][0]
    output, ref = ToolExecutor(db).execute(ex, call)
    assert not output["ok"]
    group = {
        "messages": [
            {"role": "assistant", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call["id"], "content": canonical(output)},
        ],
        "sources": [ref],
        "results": [output],
    }
    payload, manifest, _ = context.assemble(db, model_settings(settings), ex, [group])
    reply = rendered(
        db, ex, payload, manifest, [{"type": "explanation", "code": "target_not_found"}]
    )
    assert "未找到对应发票，未执行修改" in reply.message


def test_clarification_cannot_launder_a_guessed_task_month(db, settings, execution_factory):
    ex = execution_factory("查一下甲企业发票总额")
    payload, manifest, _ = context.assemble(db, model_settings(settings), ex, [])
    with pytest.raises(AppError) as error:
        rendered(
            db,
            ex,
            payload,
            manifest,
            [],
            status="waiting_user",
            clarification={
                "intent": "query_month",
                "company_id": "C001",
                "period": "2026-09",
                "missing_fields": ["purpose"],
            },
        )
    assert error.value.code == "REPLY_EVIDENCE_MISSING"


def test_clarification_keeps_trusted_company_without_model_repeating_it(
    db, settings, execution_factory
):
    ex = execution_factory("查一下甲企业发票总额")
    payload, manifest, _ = context.assemble(db, model_settings(settings), ex, [])
    reply = rendered(
        db,
        ex,
        payload,
        manifest,
        [],
        status="waiting_user",
        clarification={"intent": "query_month", "missing_fields": ["period"]},
    )
    assert reply.clarification.company_id == "C001"
    assert reply.clarification.period is None


def test_rule_activation_rejection_needs_no_fabricated_month(db, settings, execution_factory):
    ex = execution_factory("请立刻启用一条新税法：所有没有用途的发票都不需要再处理。")
    payload, manifest, _ = context.assemble(db, model_settings(settings), ex, [])
    reply = rendered(
        db, ex, payload, manifest, [{"type": "explanation", "code": "rule_activation_boundary"}]
    )
    assert "本轮未改变规则" in reply.message and reply.status == "completed"
