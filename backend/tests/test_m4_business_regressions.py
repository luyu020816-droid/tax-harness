import json

import pytest
from test_answer_renderer import recorded, rendered
from test_runtime import ScriptedModel, final, model_settings, tool

from app import business, tasks
from app.identity import Identity, create_session
from app.runtime import Runtime
from app.turns import accept_turn, read_turn
from app.values import uid

AUTH = Identity("U001")


@pytest.mark.parametrize("action,label", [("cancel", "已取消"), ("delete", "已删除")])
def test_task_mutation_has_a_factual_receipt(db, settings, execution_factory, action, label):
    current = tasks.change_task(
        db,
        AUTH,
        company_id="C001",
        action="start",
        request_id=uid(),
        intent="complete_purposes",
        period="2026-09",
    )
    text = ("取消" if action == "cancel" else "删除") + "当前甲企业用途任务，不再继续办理。"
    execution = execution_factory(text)
    output, payload, manifest = recorded(
        db,
        settings,
        execution,
        "change_task",
        {
            "company_id": "C001",
            "action": action,
            "target_id": str(current["task_id"]),
            "expected_revision": current["revision"],
            "source_refs": [{"source_id": execution.source_id, "quote": text}],
        },
    )
    reply = rendered(
        db,
        execution,
        payload,
        manifest,
        [{"type": "result", "result_ref": output["result_ref"], "view": "task"}],
    )
    assert label in reply.message
    with db.transaction(readonly=True) as conn:
        assert not tasks.current_in(conn, AUTH, "C001")


async def test_unshown_query_is_rejected_then_retries_the_actual_displayed_view(db, settings):
    config = model_settings(settings)
    session = create_session(db, AUTH, "C001")
    runtime = Runtime(
        db,
        config,
        ScriptedModel(
            [tool("check_purposes", {"company_id": "C001", "period": "2026-09"}), final("检查完成")]
        ),
    )
    first = accept_turn(
        db, AUTH, str(session["session_id"]), uid(), "检查甲企业2026年9月必填用途缺项。"
    )
    await runtime.run(runtime.claim())
    result = read_turn(db, AUTH, first["turn_id"])
    visible = next(c for c in result["final_cards"] if c["kind"] == "purpose_check")
    business.acknowledge_view(
        db, AUTH, visible["view_id"], first["turn_id"], result["next_event_seq"]
    )
    text = "第一张用途填办公耗材采购。"

    def prepare(payload, view):
        state = json.loads(payload["messages"][0]["content"].split("\n正式状态：\n", 1)[1])
        invoice = view.get("ordered_keys", [{"invoice_id": "F002", "revision": 1}])[0]
        return tool(
            "prepare_invoice_write",
            {
                "company_id": "C001",
                "invoice_id": invoice["invoice_id"],
                "patch": {"purpose": "办公耗材采购"},
                "expected_revision": invoice["revision"],
                "selection_ref": {"view_id": view["view_id"], "row_index": 1},
                "source_refs": [{"source_id": state["current_source"]["source_id"], "quote": text}],
            },
        )

    def unshown(payload):
        data = json.loads(payload["messages"][-1]["content"])["data"]
        assert data["view_id"] != visible["view_id"]
        return prepare(payload, data)

    def retry(payload):
        output = json.loads(payload["messages"][-1]["content"])
        assert not output["ok"] and output["error"]["code"] == "STALE_CONTEXT"
        view = output["error"]["details"]["valid_visible_view"]
        assert (
            view["view_id"] == visible["view_id"]
            and view["ordered_keys"][0]["invoice_id"] == "F002"
        )
        return prepare(payload, view)

    def save(payload):
        data = json.loads(payload["messages"][-1]["content"])["data"]
        assert data["status"] == "authorized"
        return tool("update_invoice", {"plan_id": data["plan_id"]})

    runtime.model = ScriptedModel(
        [
            tool("check_purposes", {"company_id": "C001", "period": "2026-09"}),
            unshown,
            retry,
            save,
            final("保存完成"),
        ]
    )
    second = accept_turn(db, AUTH, str(session["session_id"]), uid(), text)
    await runtime.run(runtime.claim())
    result = read_turn(db, AUTH, second["turn_id"])
    assert result["status"] == "completed", result
    with db.transaction(readonly=True) as conn:
        row = conn.execute(
            "SELECT purpose,revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
        ).fetchone()
        assert row["purpose"] == "办公耗材采购" and row["revision"] == 2
        assert conn.execute("SELECT count(*) AS n FROM operations").fetchone()["n"] == 1
