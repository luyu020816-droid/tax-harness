import json
import threading
import time

import pytest

from app import invoice_write
from app.agent_authority import authorize_tool, bind_turn, policy_for_turn, validate_snapshot
from app.agent_profiles import AgentDraft, create_draft, publish, set_enabled, update_draft
from app.agent_tools import set_enabled as set_tool
from app.errors import AppError
from app.execution import guard
from app.identity import Identity, create_session
from app.runtime import Runtime
from app.tools import ToolExecutor
from app.turns import accept_turn, read_turn
from app.values import uid


def new_turn(db, auth):
    session = create_session(db, auth, "C001")
    return accept_turn(db, auth, str(session["session_id"]), uid(), "查询发票")


def test_disabled_global_tool_blocks_legacy_execution_and_pending_confirmation(
    db, execution_factory
):
    auth = Identity("U001")
    text = "请安排把甲企业F002用途填写为办公耗材采购。"
    execution = execution_factory(text)
    executor = ToolExecutor(db)
    call = {
        "id": uid(),
        "function": {
            "name": "prepare_invoice_write",
            "arguments": json.dumps(
                {
                    "company_id": "C001",
                    "invoice_id": "F002",
                    "patch": {"purpose": "办公耗材采购"},
                    "expected_revision": 1,
                    "source_refs": [{"source_id": execution.source_id, "quote": text}],
                }
            ),
        },
    }
    result, _ = executor.execute(execution, call)
    assert result["ok"] and result["data"]["status"] == "needs_confirmation"
    plan = result["data"]
    set_tool(db, auth, "update_invoice", 0, False)
    denied, _ = executor.execute(
        execution,
        {
            "id": uid(),
            "function": {
                "name": "update_invoice",
                "arguments": json.dumps({"plan_id": plan["plan_id"]}),
            },
        },
    )
    assert denied["error"]["code"] == "AGENT_CAPABILITY_DENIED"
    with pytest.raises(AppError, match="停用"):
        invoice_write.confirm(
            db,
            auth,
            plan["plan_id"],
            expected_revision=plan["revision"],
            intent_hash=plan["intent_hash"],
            request_id=uid(),
        )
    with db.transaction(readonly=True) as conn:
        assert conn.execute("SELECT count(*) n FROM operations").fetchone()["n"] == 0


def test_turn_acceptance_binds_agent_atomically_and_is_idempotent(db):
    auth = Identity("U001")
    config = AgentDraft(name="原子绑定", model_profile_id="default")
    agent = create_draft(db, auth, config)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    session = create_session(db, auth, "C001")
    request_id = uid()
    args = (db, auth, str(session["session_id"]), request_id, "查询发票")
    turn = accept_turn(*args, agent_id=agent["agent_id"])
    assert accept_turn(*args, agent_id=agent["agent_id"])["turn_id"] == turn["turn_id"]
    with pytest.raises(AppError, match="内容不同"):
        accept_turn(*args)
    with db.transaction(readonly=True) as conn:
        binding = conn.execute(
            "SELECT agent_id,agent_version FROM agent_turn_bindings WHERE turn_id=%s",
            (turn["turn_id"],),
        ).fetchone()
    assert str(binding["agent_id"]) == agent["agent_id"] and binding["agent_version"] == 1


def test_worker_does_not_fall_back_when_bound_model_is_unavailable(db, settings):
    auth = Identity("U001")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO agent_model_profiles(profile_id,name,version,model_id,base_url) "
            "VALUES('missing','缺失模型',1,'test','https://example.test/v1')"
        )
    agent = create_draft(db, auth, AgentDraft(name="缺少模型", model_profile_id="missing"))
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    session = create_session(db, auth, "C001")
    turn = accept_turn(
        db, auth, str(session["session_id"]), uid(), "查询发票", agent_id=agent["agent_id"]
    )
    runtime = Runtime(db, settings, object(), agent_runtime_enabled=True)
    assert runtime.claim() is None
    result = read_turn(db, auth, turn["turn_id"])
    assert result["status"] == "failed"
    assert result["error"]["code"] == "MODEL_CONFIG_UNAVAILABLE"


def test_binding_preserves_old_grants_and_honors_revocation(db):
    auth = Identity("U001")
    config = AgentDraft(
        name="查询", model_profile_id="default", tool_grants={"find_invoices": ["invoke"]}
    )
    agent = create_draft(db, auth, config)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    turn = new_turn(db, auth)
    with db.transaction() as conn:
        binding = bind_turn(conn, auth, turn["turn_id"], agent["agent_id"])
        authorize_tool(conn, turn["turn_id"], "find_invoices", {})
    assert binding["version"] == 1
    expanded = config.model_copy(
        update={"tool_grants": {"find_invoices": ["invoke"], "get_rules": ["invoke"]}}
    )
    agent = update_draft(db, auth, agent["agent_id"], agent["revision"], expanded)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    with db.transaction() as conn:
        authorize_tool(conn, turn["turn_id"], "find_invoices", {})
        validate_snapshot(conn, turn["turn_id"])
        with pytest.raises(AppError):
            authorize_tool(conn, turn["turn_id"], "get_rules", {})
    reduced = config.model_copy(update={"tool_grants": {}})
    agent = update_draft(db, auth, agent["agent_id"], agent["revision"], reduced)
    publish(db, auth, agent["agent_id"], agent["revision"])
    with db.transaction() as conn, pytest.raises(AppError):
        authorize_tool(conn, turn["turn_id"], "find_invoices", {})
    with db.transaction() as conn, pytest.raises(AppError, match="撤销"):
        validate_snapshot(conn, turn["turn_id"])


def test_disabled_tool_is_not_bound_and_agent_disable_revokes_old_run(db):
    auth = Identity("U001")
    config = AgentDraft(
        name="查询", model_profile_id="default", tool_grants={"find_invoices": ["invoke"]}
    )
    agent = create_draft(db, auth, config)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    old_turn = new_turn(db, auth)
    with db.transaction() as conn:
        bind_turn(conn, auth, old_turn["turn_id"], agent["agent_id"])
    set_tool(db, auth, "find_invoices", 0, False)
    next_turn = new_turn(db, auth)
    with db.transaction() as conn:
        next_binding = bind_turn(conn, auth, next_turn["turn_id"], agent["agent_id"])
        assert next_binding["snapshot"]["tool_grants"] == {}
        with pytest.raises(AppError):
            authorize_tool(conn, old_turn["turn_id"], "find_invoices", {})
    set_enabled(db, auth, agent["agent_id"], agent["revision"], False)
    with db.transaction() as conn:
        assert policy_for_turn(conn, old_turn["turn_id"]).agent_enabled is False
        with pytest.raises(AppError):
            authorize_tool(conn, old_turn["turn_id"], "find_invoices", {})


def test_real_tool_executor_records_denial_and_allows_granted_query(db, execution_factory):
    auth = Identity("U001")
    config = AgentDraft(
        name="只读 Agent", model_profile_id="default", tool_grants={"find_invoices": ["invoke"]}
    )
    agent = create_draft(db, auth, config)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    execution = execution_factory("查询甲企业发票")
    with db.transaction() as conn:
        conn.execute("UPDATE turns SET status='queued' WHERE turn_id=%s", (execution.turn_id,))
        bind_turn(conn, auth, execution.turn_id, agent["agent_id"])
        conn.execute("UPDATE turns SET status='running' WHERE turn_id=%s", (execution.turn_id,))
    executor = ToolExecutor(db)

    def call(name, args):
        return {"id": uid(), "function": {"name": name, "arguments": json.dumps(args)}}

    allowed, _ = executor.execute(execution, call("find_invoices", {"company_id": "C001"}))
    assert allowed["ok"]
    rejected, _ = executor.execute(execution, call("update_invoice", {"plan_id": uid()}))
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "AGENT_CAPABILITY_DENIED"
    with db.transaction(readonly=True) as conn:
        assert conn.execute("SELECT count(*) n FROM operations").fetchone()["n"] == 0
        assert (
            conn.execute(
                "SELECT count(*) n FROM tool_executions WHERE run_id=%s AND name='update_invoice' "
                "AND state='failed'",
                (execution.run_id,),
            ).fetchone()["n"]
            == 1
        )


def test_revoked_agent_cannot_confirm_an_existing_write_plan(db, execution_factory):
    auth = Identity("U001")
    config = AgentDraft(
        name="办理 Agent",
        model_profile_id="default",
        tool_grants={"prepare_invoice_write": ["invoke"], "update_invoice": ["invoke"]},
    )
    agent = create_draft(db, auth, config)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    text = "请安排把甲企业F002用途填写为办公耗材采购。"
    execution = execution_factory(text)
    with db.transaction() as conn:
        conn.execute("UPDATE turns SET status='queued' WHERE turn_id=%s", (execution.turn_id,))
        bind_turn(conn, auth, execution.turn_id, agent["agent_id"])
        conn.execute("UPDATE turns SET status='running' WHERE turn_id=%s", (execution.turn_id,))
    call = {
        "id": uid(),
        "function": {
            "name": "prepare_invoice_write",
            "arguments": json.dumps(
                {
                    "company_id": "C001",
                    "invoice_id": "F002",
                    "patch": {"purpose": "办公耗材采购"},
                    "expected_revision": 1,
                    "source_refs": [{"source_id": execution.source_id, "quote": text}],
                }
            ),
        },
    }
    output, _ = ToolExecutor(db).execute(execution, call)
    assert output["ok"] and output["data"]["status"] == "needs_confirmation"
    plan = output["data"]
    reduced = config.model_copy(update={"tool_grants": {"prepare_invoice_write": ["invoke"]}})
    agent = update_draft(db, auth, agent["agent_id"], agent["revision"], reduced)
    publish(db, auth, agent["agent_id"], agent["revision"])
    with pytest.raises(AppError, match="无权"):
        invoice_write.confirm(
            db,
            auth,
            plan["plan_id"],
            expected_revision=plan["revision"],
            intent_hash=plan["intent_hash"],
            request_id=uid(),
        )
    with db.transaction(readonly=True) as conn:
        assert conn.execute("SELECT count(*) n FROM operations").fetchone()["n"] == 0
        row = conn.execute(
            "SELECT status,confirmation_turn_id FROM write_plans WHERE plan_id=%s",
            (plan["plan_id"],),
        ).fetchone()
        assert row["status"] == "needs_confirmation" and row["confirmation_turn_id"] is None


def test_committed_receipt_survives_agent_revocation(db, settings, execution_factory):
    auth = Identity("U001")
    config = AgentDraft(
        name="回执 Agent",
        model_profile_id="default",
        tool_grants={"prepare_invoice_write": ["invoke"], "update_invoice": ["invoke"]},
    )
    agent = create_draft(db, auth, config)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    text = "把甲企业F002用途填写为办公耗材采购。"
    execution = execution_factory(text)
    with db.transaction() as conn:
        conn.execute("UPDATE turns SET status='queued' WHERE turn_id=%s", (execution.turn_id,))
        bind_turn(conn, auth, execution.turn_id, agent["agent_id"])
        conn.execute("UPDATE turns SET status='running' WHERE turn_id=%s", (execution.turn_id,))
    executor = ToolExecutor(db)

    def call(name, args):
        return {"id": uid(), "function": {"name": name, "arguments": json.dumps(args)}}

    prepared, _ = executor.execute(
        execution,
        call(
            "prepare_invoice_write",
            {
                "company_id": "C001",
                "invoice_id": "F002",
                "patch": {"purpose": "办公耗材采购"},
                "expected_revision": 1,
                "source_refs": [{"source_id": execution.source_id, "quote": text}],
            },
        ),
    )
    assert prepared["ok"] and prepared["data"]["status"] == "authorized"
    saved, _ = executor.execute(
        execution, call("update_invoice", {"plan_id": prepared["data"]["plan_id"]})
    )
    assert saved["ok"]
    set_tool(db, auth, "update_invoice", 0, False)
    Runtime(db, settings, object()).fail(
        execution, AppError("AGENT_AUTHORITY_CHANGED", "Agent 能力已停用。", 409)
    )
    result = read_turn(db, auth, execution.turn_id)
    assert result["status"] == "failed"
    assert any(card["kind"] == "operation" for card in result["final_cards"])
    with db.transaction(readonly=True) as conn:
        assert (
            conn.execute("SELECT count(*) n FROM operations WHERE state='committed'").fetchone()[
                "n"
            ]
            == 1
        )


def test_tool_revocation_waits_for_in_flight_authorized_transaction(db, execution_factory):
    auth = Identity("U001")
    config = AgentDraft(
        name="事务 Agent", model_profile_id="default", tool_grants={"find_invoices": ["invoke"]}
    )
    agent = create_draft(db, auth, config)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    execution = execution_factory("查询甲企业发票")
    with db.transaction() as conn:
        conn.execute("UPDATE turns SET status='queued' WHERE turn_id=%s", (execution.turn_id,))
        bind_turn(conn, auth, execution.turn_id, agent["agent_id"])
        conn.execute("UPDATE turns SET status='running' WHERE turn_id=%s", (execution.turn_id,))
    execution.active_tool = ("find_invoices", {"company_id": "C001"})

    started, finished = threading.Event(), threading.Event()
    failures = []

    def revoke():
        try:
            started.set()
            set_tool(db, auth, "find_invoices", 0, False)
        except Exception as exc:
            failures.append(exc)
        finally:
            finished.set()

    with db.transaction() as conn:
        guard(conn, execution)
        thread = threading.Thread(target=revoke)
        thread.start()
        assert started.wait(2)
        deadline = time.monotonic() + 2
        waiting = False
        while time.monotonic() < deadline:
            waiting = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted) AS waiting"
            ).fetchone()["waiting"]
            if waiting or finished.is_set():
                break
            finished.wait(0.02)
        assert waiting and not finished.is_set()
    assert finished.wait(5)
    thread.join()
    assert not failures
    with db.transaction() as conn, pytest.raises(AppError):
        guard(conn, execution)
