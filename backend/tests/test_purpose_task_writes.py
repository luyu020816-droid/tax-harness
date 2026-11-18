import pytest
from test_trusted_writes import candidate

from app import invoice_write, tasks
from app.errors import AppError
from app.values import uid

PERIOD = "2026-09"
WITHHELD = "把甲企业 F002 的用途填写为办公耗材采购。F003 先别动。"
CONFIRMABLE = "请安排把甲企业F002用途填写为办公耗材采购。"


def change(db, auth, action, task=None, **fields):
    return tasks.change_task(
        db,
        auth,
        company_id="C001",
        action=action,
        request_id=uid(),
        target_id=str(task["task_id"]) if task else None,
        expected_revision=task["revision"] if task else None,
        **fields,
    )


def test_excluded_invoice_cannot_be_proposed(db, execution_factory):
    ex = execution_factory(WITHHELD)
    with pytest.raises(AppError):
        invoice_write.prepare(db, ex.auth, **{**candidate(ex, WITHHELD), "invoice_id": "F003"})


def test_task_cancel_revokes_its_confirmation(db, admin, execution_factory):
    from app.identity import Identity

    task = change(db, Identity("U001"), "start", intent="complete_purposes", period=PERIOD)
    ex = execution_factory(CONFIRMABLE)
    plan = invoice_write.prepare(db, ex.auth, **candidate(ex, CONFIRMABLE))
    assert plan["status"] == "needs_confirmation"
    change(db, ex.auth, "cancel", task)
    with pytest.raises(AppError) as error:
        invoice_write.confirm(
            db,
            ex.auth,
            plan["plan_id"],
            expected_revision=plan["revision"],
            intent_hash=plan["intent_hash"],
            request_id=uid(),
        )
    assert error.value.code == "WRITE_PLAN_STALE"
    assert invoice_write.get(db, ex.auth, plan["plan_id"])["status"] == "revoked"
    assert not admin.execute("SELECT 1 FROM operations").fetchone()


@pytest.mark.asyncio
async def test_replaced_task_cannot_execute_an_already_queued_confirmation(
    db, admin, settings, execution_factory
):
    from test_runtime import ScriptedModel, model_settings

    from app.identity import Identity
    from app.runtime import Runtime
    from app.turns import read_turn

    auth = Identity("U001")
    task = change(db, auth, "start", intent="complete_purposes", period=PERIOD)
    ex = execution_factory(CONFIRMABLE)
    plan = invoice_write.prepare(db, ex.auth, **candidate(ex, CONFIRMABLE))
    admin.execute("UPDATE turns SET status='completed' WHERE turn_id=%s", (ex.turn_id,))
    admin.execute("UPDATE work_items SET state='done' WHERE work_id=%s", (ex.work_id,))
    params = {
        "expected_revision": plan["revision"],
        "intent_hash": plan["intent_hash"],
        "request_id": uid(),
    }
    accepted = invoice_write.confirm(db, auth, plan["plan_id"], **params)
    replacement = change(db, auth, "replace", task, intent="complete_purposes", period=PERIOD)
    assert replacement["generation"] > task["generation"]
    for identity in (auth, Identity("U002")):
        with pytest.raises(AppError):
            invoice_write.confirm(db, identity, plan["plan_id"], **params)
    runtime = Runtime(db, model_settings(settings), ScriptedModel([]))
    execution = runtime.claim()
    assert execution.turn_id == accepted["turn_id"]
    await runtime.run(execution)
    assert read_turn(db, auth, accepted["turn_id"])["status"] == "failed"
    assert not admin.execute("SELECT 1 FROM operations").fetchone()


@pytest.mark.parametrize("commit_first", [False, True])
def test_task_cancel_and_commit_honor_database_order(db, admin, execution_factory, commit_first):
    from app.identity import Identity

    auth = Identity("U001")
    task = change(db, auth, "start", intent="complete_purposes", period=PERIOD)
    text = "把甲企业F002用途填写为办公耗材采购。"
    ex = execution_factory(text)
    plan = invoice_write.prepare(db, auth, **candidate(ex, text))
    assert plan["status"] == "authorized"
    if commit_first:
        receipt = invoice_write.commit(db, auth, plan["plan_id"], execution=ex)
        assert receipt["commit_state"] == "committed"
        with db.transaction(readonly=True) as conn:
            task = tasks.current_in(conn, auth, "C001")
    change(db, auth, "cancel", task)
    if not commit_first:
        with pytest.raises(AppError):
            invoice_write.commit(db, auth, plan["plan_id"], execution=ex)
    invoice = admin.execute(
        "SELECT purpose,revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
    ).fetchone()
    assert invoice == {
        "purpose": "办公耗材采购" if commit_first else None,
        "revision": 2 if commit_first else 1,
    }
    assert admin.execute("SELECT count(*) AS n FROM operations WHERE state='committed'").fetchone()[
        "n"
    ] == int(commit_first)
    assert invoice_write.get(db, auth, plan["plan_id"])["status"] == (
        "consumed" if commit_first else "revoked"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_failure", [False, True])
async def test_purpose_task_six_turns(db, admin, settings, monkeypatch, refresh_failure):
    import json

    from test_runtime import AUTH, ScriptedModel, answer_plan, model_settings, tool

    from app.identity import create_session
    from app.runtime import Runtime
    from app.turns import accept_turn, read_turn

    admin.execute(
        "UPDATE invoices SET purpose='验收夹具' "
        "WHERE company_id='C001' AND invoice_id IN ('F004','F006','F009')"
    )
    session = str(create_session(db, AUTH, "C001")["session_id"])
    model = ScriptedModel([])
    runtime = Runtime(db, model_settings(settings), model)

    def state(payload):
        return json.loads(payload["messages"][0]["content"].split("\n正式状态：\n", 1)[1])

    def output(payload):
        result = json.loads(payload["messages"][-1]["content"])
        assert result["ok"], result
        return result

    def result(view):
        return lambda p: answer_plan(
            blocks=[
                {
                    "type": "result",
                    "view": view,
                    "result_ref": output(p)["result_ref"],
                }
            ]
        )

    async def run(accepted):
        ex = runtime.claim()
        assert ex.turn_id == accepted["turn_id"]
        await runtime.run(ex)
        turn = read_turn(db, AUTH, ex.turn_id)
        assert turn["status"] in ("completed", "waiting_user"), (
            turn["status"],
            turn["error"],
            turn["final_message"],
            turn["runs"],
        )
        return turn

    async def send(text, *steps):
        model.steps = iter(steps)
        return await run(accept_turn(db, AUTH, session, uid(), text))

    first = await send(
        "帮我检查一下甲企业哪些发票还没填用途。",
        answer_plan(
            blocks=[],
            status="waiting_user",
            clarification={
                "intent": "complete_purposes",
                "company_id": "C001",
                "missing_fields": ["period"],
            },
        ),
    )
    task_id = first["pending_clarification"]["task_id"]
    assert first["pending_clarification"]["missing_fields"] == ["period"]
    assert not admin.execute("SELECT 1 FROM write_plans").fetchone()
    await send("谢谢", answer_plan(blocks=[{"type": "chat_answer", "text": "不客气。"}]))
    assert read_turn(db, AUTH, first["turn_id"])["status"] == "waiting_user"
    checked = await send(
        "2026 年 9 月。",
        tool("check_purposes", {"company_id": "C001", "period": PERIOD}),
        result("purpose_check"),
    )
    assert checked["continuation_of_turn_id"] == first["turn_id"]
    assert checked["final_cards"][0]["missing_count"] == 2
    assert read_turn(db, AUTH, first["turn_id"])["status"] == "resolved"

    async def save(text, purpose, revision):
        def propose(payload):
            current = state(payload)["current_source"]
            return tool(
                "prepare_invoice_write",
                {
                    "company_id": "C001",
                    "invoice_id": "F002",
                    "patch": {"purpose": purpose},
                    "expected_revision": revision,
                    "source_refs": [{"source_id": current["source_id"], "quote": text}],
                },
            )

        def pending(payload):
            plan = output(payload)["data"]
            assert plan["status"] == "needs_confirmation"
            return answer_plan(
                blocks=[{"type": "confirmation", "plan_id": plan["plan_id"]}],
                status="waiting_user",
                clarification={
                    "intent": "update_invoice",
                    "company_id": "C001",
                    "missing_fields": ["write_confirmation"],
                },
            )

        waiting = await send(text, propose, pending)
        plan = next(c for c in waiting["final_cards"] if c["kind"] == "write_plan")
        params = {
            "expected_revision": plan["revision"],
            "intent_hash": plan["intent_hash"],
            "request_id": uid(),
        }
        assert (
            admin.execute(
                "SELECT count(*) AS n FROM operations WHERE state='committed'"
            ).fetchone()["n"]
            == revision - 1
        )
        accepted = invoice_write.confirm(db, AUTH, plan["plan_id"], **params)
        assert (
            invoice_write.confirm(db, AUTH, plan["plan_id"], **params)["turn_id"]
            == accepted["turn_id"]
        )
        confirmed = await run(accepted)
        receipt = next(c for c in confirmed["final_cards"] if c["kind"] == "operation")
        assert receipt["verification_state"] == "verified"
        assert receipt["saved_values"] == {"purpose": purpose}
        pending = [c for c in confirmed["final_cards"] if c["kind"] == "purpose_check"]
        if refresh_failure and revision == 2:
            assert not pending
            assert "待办需要重新检查" in confirmed["final_message"]
        else:
            assert [row["invoice_id"] for row in pending[0]["rows"]] == ["F003"]
        return receipt["operation_id"]

    original = await save(WITHHELD, "办公耗材采购", 1)
    if refresh_failure:
        from app import business

        def fail_check(*args, **kwargs):
            raise AppError("CHECK_UNAVAILABLE", "验收注入：待办刷新暂时不可用。", 503)

        monkeypatch.setattr(business, "check_purposes", fail_check)
    correction = await save(
        "更正甲企业 F002 的用途为会议物料采购。F003 先别动。", "会议物料采购", 2
    )

    def cancel(payload):
        current = state(payload)
        task = current["task"]
        assert str(task["task_id"]) == task_id
        return tool(
            "change_task",
            {
                "company_id": "C001",
                "action": "cancel",
                "expected_revision": task["revision"],
                "target_id": task_id,
                "source_refs": [
                    {"source_id": current["current_source"]["source_id"], "quote": "取消这次"}
                ],
            },
        )

    cancelled = await send(
        "取消这次甲企业 2026 年 9 月的用途补充任务。剩下的发票不办了。",
        cancel,
        result("task"),
    )
    assert "待办已取消" in cancelled["final_message"]
    assert ("F003" in cancelled["final_message"]) is not refresh_failure
    history = await send(
        "这次保存了什么？还有没有待办？",
        tool("get_task", {"company_id": "C001", "include_cancelled": True}),
        result("task"),
    )
    assert [c["operation_id"] for c in history["final_cards"] if c["kind"] == "operation"] == [
        original,
        correction,
    ]
    assert not admin.execute("SELECT current_task_id FROM task_slots").fetchone()["current_task_id"]
    assert (
        admin.execute("SELECT status FROM tasks WHERE task_id=%s", (task_id,)).fetchone()["status"]
        == "cancelled"
    )
    actual = admin.execute(
        "SELECT invoice_id,purpose,revision FROM invoices "
        "WHERE company_id='C001' AND invoice_id IN ('F002','F003') ORDER BY invoice_id"
    ).fetchall()
    assert actual == [
        {"invoice_id": "F002", "purpose": "会议物料采购", "revision": 3},
        {"invoice_id": "F003", "purpose": None, "revision": 1},
    ]
