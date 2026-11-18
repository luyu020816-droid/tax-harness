import json

import pytest
from test_runtime import AUTH, ScriptedModel, answer_plan, model_settings, tool
from test_trusted_writes import candidate

from app import invoice_write, tasks
from app.errors import AppError
from app.identity import create_session, switch_company
from app.request_evidence import bind_scope
from app.runtime import Runtime
from app.tools import ToolExecutor
from app.turns import accept_turn, read_turn
from app.values import uid

PERIOD = "2026-09"
CONFIRMABLE = "请安排把甲企业F002用途填写为办公耗材采购。"
DIRECT = "把甲企业F002用途填写为办公耗材采购。"


def start_task(db):
    return tasks.change_task(
        db,
        AUTH,
        company_id="C001",
        action="start",
        request_id=uid(),
        intent="complete_purposes",
        period=PERIOD,
    )


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("get_task", {"company_id": "C001"}),
        ("resolve_scope", {"company_id": "C001", "period": PERIOD}),
    ],
)
def test_implicit_resume_cannot_use_another_company_task(db, execution_factory, tool_name, args):
    start_task(db)
    ex = execution_factory("继续上次用途补充", company="C002")
    result, _ = ToolExecutor(db).execute(ex, tool(tool_name, args)["tool_calls"][0])
    assert result["ok"] is False, result
    assert result["error"]["code"] == "TARGET_SCOPE_MISMATCH"


def test_explicit_month_does_not_override_selected_company(db, execution_factory):
    start_task(db)
    ex = execution_factory("继续上次用途补充，2026年9月。", company="C002")
    with db.transaction() as conn:
        binding = bind_scope(conn, ex, "C001", PERIOD)
    call = tool(
        "check_purposes",
        {
            "company_id": "C001",
            "period": PERIOD,
            "scope_ref": binding["scope_ref"],
        },
    )
    result, _ = ToolExecutor(db).execute(ex, call["tool_calls"][0])
    assert not result["ok"] and result["error"]["code"] == "TARGET_SCOPE_MISMATCH"


@pytest.mark.asyncio
async def test_previous_session_query_does_not_override_company_switch(db, settings):
    session = str(create_session(db, AUTH, "C001")["session_id"])
    config = model_settings(settings)
    first = await run(
        Runtime(
            db,
            config,
            ScriptedModel(
                [
                    tool("check_purposes", {"company_id": "C001", "period": PERIOD}),
                    result_block("purpose_check"),
                ]
            ),
        ),
        accept_turn(db, AUTH, session, uid(), "检查甲企业2026年9月的用途缺项。"),
    )
    assert first["status"] == "completed"
    switch_company(db, AUTH, session, "C002")

    def verify_denial(payload):
        output = json.loads(payload["messages"][-1]["content"])
        assert not output["ok"] and output["error"]["code"] == "TARGET_SCOPE_MISMATCH"
        return answer_plan(blocks=[{"type": "explanation", "code": "no_tasks"}])

    result = await run(
        Runtime(
            db,
            config,
            ScriptedModel(
                [
                    tool("resolve_scope", {"company_id": "C001", "period": PERIOD}),
                    verify_denial,
                ]
            ),
        ),
        accept_turn(db, AUTH, session, uid(), "继续上次用途补充。"),
    )
    assert result["status"] == "completed" and not result["final_cards"]


def test_confirmation_replay_checks_current_company(db, admin, execution_factory):
    start_task(db)
    ex = execution_factory(CONFIRMABLE)
    plan = invoice_write.prepare(db, AUTH, **candidate(ex, CONFIRMABLE))
    params = dict(
        expected_revision=plan["revision"], intent_hash=plan["intent_hash"], request_id=uid()
    )
    initial = invoice_write.confirm(db, AUTH, plan["plan_id"], **params)
    # Force the stale selector through the same check used by confirmation replay.
    admin.execute(
        "UPDATE sessions SET selected_company_id='C002' WHERE session_id=%s", (ex.session_id,)
    )
    with pytest.raises(AppError) as error:
        invoice_write.confirm(db, AUTH, plan["plan_id"], **params)
    assert error.value.code == "WRITE_PLAN_STALE"
    admin.execute(
        "UPDATE sessions SET selected_company_id='C001' WHERE session_id=%s", (ex.session_id,)
    )
    replay = invoice_write.confirm(db, AUTH, plan["plan_id"], **params)
    assert replay == {"turn_id": initial["turn_id"], "replayed": True}
    assert not admin.execute("SELECT 1 FROM operations").fetchone()


@pytest.mark.asyncio
async def test_queued_confirmation_rechecks_company_before_writing(
    db, admin, settings, execution_factory
):
    start_task(db)
    ex = execution_factory(CONFIRMABLE)
    plan = invoice_write.prepare(db, AUTH, **candidate(ex, CONFIRMABLE))
    admin.execute("UPDATE turns SET status='completed' WHERE turn_id=%s", (ex.turn_id,))
    admin.execute("UPDATE work_items SET state='done' WHERE work_id=%s", (ex.work_id,))
    accepted = invoice_write.confirm(
        db,
        AUTH,
        plan["plan_id"],
        expected_revision=plan["revision"],
        intent_hash=plan["intent_hash"],
        request_id=uid(),
    )
    admin.execute(
        "UPDATE sessions SET selected_company_id='C002' WHERE session_id=%s", (ex.session_id,)
    )
    turn = await run(Runtime(db, model_settings(settings), ScriptedModel([])), accepted)
    assert turn["status"] == "failed" and turn["error"]["code"] == "WRITE_PLAN_STALE"
    assert not admin.execute("SELECT 1 FROM operations").fetchone()


def result_block(view):
    def reply(payload):
        output = json.loads(payload["messages"][-1]["content"])
        assert output["ok"], output
        return answer_plan(
            blocks=[{"type": "result", "view": view, "result_ref": output["result_ref"]}]
        )

    return reply


def prepare_direct(payload):
    state = json.loads(payload["messages"][0]["content"].split("\n正式状态：\n", 1)[1])
    return tool(
        "update_invoice",
        {
            "company_id": "C001",
            "invoice_id": "F002",
            "patch": {"purpose": "办公耗材采购"},
            "expected_revision": 1,
            "source_refs": [{"source_id": state["current_source"]["source_id"], "quote": DIRECT}],
        },
    )


def render_receipt(payload):
    output = json.loads(payload["messages"][-1]["content"])
    assert output["ok"], output
    return answer_plan(
        blocks=[{"type": "operation", "operation_id": output["data"]["operation_id"]}]
    )


async def run(runtime, accepted):
    ex = runtime.claim()
    assert ex and ex.turn_id == accepted["turn_id"]
    await runtime.run(ex)
    return read_turn(runtime.db, AUTH, ex.turn_id)


@pytest.mark.asyncio
async def test_restart_runtime_resumes_same_task_in_new_session(db, admin, settings):
    admin.execute(
        "UPDATE invoices SET purpose='验收夹具' "
        "WHERE company_id='C001' AND invoice_id IN ('F004','F006','F009')"
    )
    task = start_task(db)
    config = model_settings(settings)
    first = str(create_session(db, AUTH, "C001")["session_id"])
    saved = await run(
        Runtime(db, config, ScriptedModel([prepare_direct, render_receipt])),
        accept_turn(db, AUTH, first, uid(), DIRECT),
    )
    assert saved["status"] == "completed", saved["error"]

    # The new Runtime uses a fresh model instance and only stored task evidence.
    second = str(create_session(db, AUTH, "C001")["session_id"])
    verifier = ScriptedModel(
        [
            tool("check_purposes", {"company_id": "C001", "period": PERIOD}),
            result_block("purpose_check"),
        ]
    )
    request_id = uid()
    resumed = await run(
        Runtime(db, config, verifier),
        accept_turn(db, AUTH, second, request_id, "继续上次用途补充"),
    )
    assert resumed["status"] == "completed", resumed["error"]
    assert resumed["final_cards"][0]["missing_count"] == 1
    assert [row["invoice_id"] for row in resumed["final_cards"][0]["rows"]] == ["F003"]
    with db.transaction(readonly=True) as conn:
        current = tasks.current_in(conn, AUTH, "C001")
    assert current["task_id"] == task["task_id"] and current["generation"] == task["generation"]
    replay = accept_turn(db, AUTH, second, request_id, "继续上次用途补充")
    assert replay["turn_id"] == resumed["turn_id"] and replay["status"] == "completed"
    assert admin.execute("SELECT count(*) AS n FROM operations").fetchone()["n"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("after_commit", [False, True])
async def test_model_timeout_delivers_only_durable_save_and_retries_same_source(
    db, admin, settings, after_commit
):
    from app.turns import retry_turn

    task = start_task(db)
    session = str(create_session(db, AUTH, "C001")["session_id"])
    config = model_settings(settings)

    def timeout(_):
        raise AppError("MODEL_UNAVAILABLE", "验收注入：模型调用超时。", 503, True)

    steps = [prepare_direct] if after_commit else []
    model = ScriptedModel([*steps, timeout, timeout])
    failed = await run(Runtime(db, config, model), accept_turn(db, AUTH, session, uid(), DIRECT))
    assert failed["status"] == "failed" and failed["error"]["code"] == "MODEL_UNAVAILABLE"
    cards = [card for card in failed["final_cards"] if card["kind"] == "operation"]
    actual = admin.execute(
        "SELECT count(*) AS n FROM operations WHERE state='committed'"
    ).fetchone()
    assert actual["n"] == len(cards) == int(after_commit)
    assert len(model.requests) == (4 if after_commit else 2)
    if after_commit:
        assert cards[0]["saved_values"] == {"purpose": "办公耗材采购"}
        assert "本轮后续处理未能完成" in failed["final_message"]
    with db.transaction(readonly=True) as conn:
        assert tasks.current_in(conn, AUTH, "C001")["task_id"] == task["task_id"]

    request_id = uid()
    params = (failed["turn_id"], request_id, failed["active_run_generation"])
    accepted = retry_turn(db, AUTH, *params)
    assert retry_turn(db, AUTH, *params)["turn_id"] == accepted["turn_id"]
    steps = [render_receipt] if after_commit else [prepare_direct, render_receipt]
    recovered = await run(Runtime(db, config, ScriptedModel(steps)), accepted)
    assert recovered["status"] == "completed", recovered
    assert len(recovered["runs"]) == 2 and len(recovered["operations"]) == 1
    assert recovered["operations"][0]["after_revision"] == 2
    if after_commit:
        assert recovered["operations"][0]["operation_id"] == cards[0]["operation_id"]
