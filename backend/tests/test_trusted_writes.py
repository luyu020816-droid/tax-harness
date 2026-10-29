import pytest
from test_runtime import ScriptedModel, model_settings, tool

from app import context, invoice_write, mutations
from app.errors import AppError
from app.runtime import Runtime
from app.tool_schemas import FinalReply
from app.tools import ToolExecutor
from app.turns import read_turn
from app.values import canonical, uid
from app.write_commands import authorize_direct

TEXT = "把甲企业F002用途填写为办公耗材采购。"


def candidate(execution, text=TEXT):
    return dict(
        company_id="C001",
        invoice_id="F002",
        patch={"purpose": "办公耗材采购"},
        expected_revision=1,
        source_id=execution.source_id,
        source_refs=[{"source_id": execution.source_id, "quote": text}],
        execution=execution,
    )


@pytest.mark.parametrize(
    "text,status",
    [
        (TEXT, "authorized"),
        ("甲企业F002用途填办公耗材采购", "authorized"),
        ("先只填F002用途为办公耗材采购，其余还没确认。", "authorized"),
        ("只解释这句话，不要执行：把甲企业F002用途填写为办公耗材采购。", "prohibited"),
        ("假如把甲企业F002用途填写为办公耗材采购，会发生什么？", "prohibited"),
        ("请安排把甲企业F002用途填写为办公耗材采购。", "needs_confirmation"),
    ],
)
def test_direct_grammar_is_exact_and_fail_closed(text, status):
    assert authorize_direct(text, ["甲企业", "C001"], "F002", {"purpose": "办公耗材采购"}) == status


def test_unplanned_core_write_has_no_bypass(db, execution_factory):
    ex = execution_factory(TEXT)
    with pytest.raises(AppError) as error:
        mutations.update_invoice(db, ex.auth, **candidate(ex))
    assert error.value.code == "WRITE_AUTHORIZATION_REQUIRED"


def test_authorized_plan_commits_once_and_replays(db, admin, execution_factory):
    ex = execution_factory(TEXT)
    plan = invoice_write.prepare(db, ex.auth, **candidate(ex))
    assert plan["status"] == "authorized"
    first = invoice_write.commit(db, ex.auth, plan["plan_id"], execution=ex)
    second = invoice_write.commit(db, ex.auth, plan["plan_id"], execution=ex)
    assert first["verification_state"] == "verified" and second["replayed"]
    assert first["operation_id"] == second["operation_id"]
    assert (
        admin.execute(
            "SELECT revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
        ).fetchone()["revision"]
        == 2
    )
    assert (
        admin.execute(
            "SELECT status FROM write_plans WHERE plan_id=%s", (plan["plan_id"],)
        ).fetchone()["status"]
        == "consumed"
    )


@pytest.mark.parametrize(
    "text",
    [
        "把甲企业F002用途填写为办公耗材采购，但不要保存。",
        "“把甲企业F002用途填写为办公耗材采购”",
        "我只是想知道把甲企业F002用途填写为办公耗材采购会怎样。",
    ],
)
def test_negative_envelope_never_becomes_a_confirmation(text):
    assert authorize_direct(text, ["甲企业"], "F002", {"purpose": "办公耗材采购"}) == "prohibited"


def test_negative_words_inside_assigned_value_are_not_a_denial():
    assert (
        authorize_direct(
            "甲企业F002用途填不要自动付款", ["甲企业"], "F002", {"purpose": "不要自动付款"}
        )
        == "authorized"
    )


def test_quoted_non_authorization_cannot_be_written(db, admin, execution_factory):
    text = "只解释这句话，不要执行也不要保存：把甲企业F002用途填写为办公耗材采购。"
    ex = execution_factory(text)
    plan = invoice_write.prepare(db, ex.auth, **candidate(ex, text))
    assert plan["status"] == "prohibited"
    with pytest.raises(AppError):
        invoice_write.commit(db, ex.auth, plan["plan_id"], execution=ex)
    assert not admin.execute("SELECT 1 FROM operations").fetchone()
    assert (
        admin.execute(
            "SELECT purpose FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
        ).fetchone()["purpose"]
        is None
    )


def test_patch_cannot_be_changed_after_authorization(db, execution_factory):
    ex = execution_factory(TEXT)
    plan = invoice_write.prepare(db, ex.auth, **candidate(ex))
    args = candidate(ex)
    args["patch"] = {"note": "办公耗材采购"}
    with pytest.raises(AppError) as error:
        mutations.update_invoice(db, ex.auth, **args, plan_id=plan["plan_id"])
    assert error.value.code == "WRITE_NOT_AUTHORIZED"


@pytest.mark.asyncio
async def test_confirmation_is_durable_and_does_not_call_model(db, settings, execution_factory):
    text = "请安排把甲企业F002用途填写为办公耗材采购。"
    ex = execution_factory(text)
    args = candidate(ex, text)
    args.pop("source_id")
    args.pop("execution")
    call = tool("prepare_invoice_write", args)["tool_calls"][0]
    output, ref = ToolExecutor(db).execute(ex, call)
    assert output["ok"], output
    plan = output["data"]
    assert plan["status"] == "needs_confirmation"
    group = {
        "messages": [
            {"role": "assistant", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call["id"], "content": canonical(output)},
        ],
        "sources": [ref],
        "results": [output],
        "memory_epoch": ex.expected_epoch,
    }
    config = model_settings(settings)
    payload, manifest, _ = context.assemble(db, config, ex, [group])
    runtime = Runtime(db, config, ScriptedModel([]))
    runtime.finish(
        ex,
        FinalReply.model_validate(
            {
                "blocks": [{"type": "confirmation", "plan_id": plan["plan_id"]}],
                "status": "waiting_user",
                "clarification": {
                    "intent": "update_invoice",
                    "company_id": "C001",
                    "missing_fields": ["write_confirmation"],
                },
            }
        ),
        payload,
        manifest,
    )
    accepted = invoice_write.confirm(
        db,
        ex.auth,
        plan["plan_id"],
        expected_revision=plan["revision"],
        intent_hash=plan["intent_hash"],
        request_id=uid(),
    )
    replay = invoice_write.confirm(
        db,
        ex.auth,
        plan["plan_id"],
        expected_revision=plan["revision"],
        intent_hash=plan["intent_hash"],
        request_id=uid(),
    )
    assert accepted["turn_id"] == replay["turn_id"]
    claimed = runtime.claim()
    assert claimed.turn_id == accepted["turn_id"]
    await runtime.run(claimed)
    result = read_turn(db, ex.auth, accepted["turn_id"])
    assert result["status"] == "completed", result
    assert result["final_cards"][0]["verification_state"] == "verified"
    assert read_turn(db, ex.auth, ex.turn_id)["status"] == "resolved"
    assert not runtime.model.requests


@pytest.mark.parametrize(
    "change,code",
    [
        ("UPDATE write_plans SET expires_at=now()-interval '1 second'", "WRITE_PLAN_STALE"),
        (
            "UPDATE invoices SET revision=revision+1 WHERE company_id='C001' AND invoice_id='F002'",
            "WRITE_PLAN_STALE",
        ),
        (
            "UPDATE company_heads SET rules_epoch=rules_epoch+1 WHERE company_id='C001'",
            "WRITE_PLAN_STALE",
        ),
        ("UPDATE sources SET revision=revision+1 WHERE role='user'", "SOURCE_INVALIDATED"),
    ],
)
def test_stale_confirmation_never_writes(db, admin, execution_factory, change, code):
    text = "请安排把甲企业F002用途填写为办公耗材采购。"
    ex = execution_factory(text)
    plan = invoice_write.prepare(db, ex.auth, **candidate(ex, text))
    admin.execute(change)
    with pytest.raises(AppError) as error:
        invoice_write.confirm(
            db,
            ex.auth,
            plan["plan_id"],
            expected_revision=plan["revision"],
            intent_hash=plan["intent_hash"],
            request_id=uid(),
        )
    assert error.value.code == code
    assert not admin.execute("SELECT 1 FROM operations").fetchone()


def test_cancel_and_cross_user_confirmation_are_fail_closed(db, admin, execution_factory):
    from app.identity import Identity

    text = "请安排把甲企业F002用途填写为办公耗材采购。"
    ex = execution_factory(text)
    plan = invoice_write.prepare(db, ex.auth, **candidate(ex, text))
    params = dict(
        expected_revision=plan["revision"], intent_hash=plan["intent_hash"], request_id=uid()
    )
    with pytest.raises(AppError):
        invoice_write.confirm(db, Identity("U002"), plan["plan_id"], **params)
    assert invoice_write.cancel(db, ex.auth, plan["plan_id"])["status"] == "revoked"
    with pytest.raises(AppError):
        invoice_write.confirm(db, ex.auth, plan["plan_id"], **params)
    assert not admin.execute("SELECT 1 FROM operations").fetchone()


def test_confirmation_http_binds_owner_hash_and_reuses_turn(db, settings, execution_factory):
    from fastapi.testclient import TestClient

    from app.api import create_app

    text = "请安排把甲企业F002用途填写为办公耗材采购。"
    ex = execution_factory(text)
    plan = invoice_write.prepare(db, ex.auth, **candidate(ex, text))
    with TestClient(create_app(settings, db=db, start_workers=False)) as client:
        assert client.post("/api/v1/demo/login", json={"user_id": "U002"}).status_code == 200
        path = f"/api/v1/write-plans/{plan['plan_id']}"
        assert client.get(path).status_code == 404
        client.post("/api/v1/demo/login", json={"user_id": "U001"})
        params = dict(expected_revision=plan["revision"], intent_hash="0" * 64, request_id=uid())
        assert client.post(path + "/confirm", json=params).status_code == 409
        params["intent_hash"] = plan["intent_hash"]
        accepted = client.post(path + "/confirm", json=params)
        assert accepted.status_code == 200, accepted.text
        repeated = client.post(path + "/confirm", json={**params, "request_id": uid()})
        assert repeated.json()["turn_id"] == accepted.json()["turn_id"]


def test_null_requires_explicit_clear_not_a_missing_value(db, execution_factory):
    ex = execution_factory("甲企业F002用途是什么？")
    args = candidate(ex, "甲企业F002用途是什么？")
    args["patch"] = {"purpose": None}
    with pytest.raises(AppError) as error:
        invoice_write.prepare(db, ex.auth, **args)
    assert error.value.code == "NEEDS_CLARIFICATION"


def test_concurrent_confirmations_create_one_control_turn(db, admin, execution_factory):
    from concurrent.futures import ThreadPoolExecutor

    text = "请安排把甲企业F002用途填写为办公耗材采购。"
    ex = execution_factory(text)
    plan = invoice_write.prepare(db, ex.auth, **candidate(ex, text))

    def confirm_once(_):
        return invoice_write.confirm(
            db,
            ex.auth,
            plan["plan_id"],
            expected_revision=plan["revision"],
            intent_hash=plan["intent_hash"],
            request_id=uid(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm_once, range(2)))
    assert results[0]["turn_id"] == results[1]["turn_id"]
    assert (
        admin.execute(
            "SELECT count(*) AS n FROM turns WHERE request_kind='write_confirmation'"
        ).fetchone()["n"]
        == 1
    )
