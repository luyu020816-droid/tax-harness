"""Independent acceptance additions for gaps in the first-phase guide."""

import pytest
from test_runtime import ScriptedModel, model_settings, tool

from app.agent_authority import context_configuration
from app.agent_profiles import AgentDraft, create_draft, publish, set_enabled, update_draft
from app.agent_skills import SkillDraft
from app.agent_skills import publish as publish_skill
from app.agent_skills import set_enabled as enable_skill
from app.identity import Identity, create_session
from app.turns import accept_turn
from app.values import uid


def turn(db, agent_id):
    session = create_session(db, Identity("U001"), "C001")
    return accept_turn(
        db, Identity("U001"), str(session["session_id"]), uid(), "查询发票", agent_id=agent_id
    )


def test_multiple_skills_and_disable_keeps_direct_tool_grant(db):
    auth = Identity("U001")
    skills = [
        publish_skill(
            db,
            auth,
            SkillDraft(
                name=f"验收技能{i}",
                instructions=f"指引{i}",
                tool_grants={"find_invoices": ["invoke"]},
            ),
        )
        for i in range(2)
    ]
    draft = AgentDraft(
        name="组合验收",
        model_profile_id="default",
        tool_grants={"find_invoices": ["invoke"]},
        skill_ids=[s["skill_id"] for s in skills],
    )
    agent = create_draft(db, auth, draft)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    before = turn(db, agent["agent_id"])
    with db.transaction() as conn:
        config = context_configuration(conn, before["turn_id"])
        assert {s["instructions"] for s in config["skills"]} == {"指引0", "指引1"}
    enable_skill(db, auth, skills[0]["skill_id"], skills[0]["revision"], False)
    after = turn(db, agent["agent_id"])
    with db.transaction() as conn:
        config = context_configuration(conn, after["turn_id"])
        assert [s["instructions"] for s in config["skills"]] == ["指引1"]
        assert config["policy"].actions("find_invoices") == frozenset({"invoke"})


def test_publication_keeps_in_flight_prompt_and_changes_new_turn(db):
    auth = Identity("U001")
    draft = AgentDraft(name="版本验收", prompt="旧指引", model_profile_id="default")
    agent = create_draft(db, auth, draft)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    old = turn(db, agent["agent_id"])
    with db.transaction() as conn:
        conn.execute("UPDATE turns SET status='running' WHERE turn_id=%s", (old["turn_id"],))
    agent = update_draft(
        db,
        auth,
        agent["agent_id"],
        agent["revision"],
        draft.model_copy(update={"prompt": "新指引"}),
    )
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    new = turn(db, agent["agent_id"])
    with db.transaction() as conn:
        assert context_configuration(conn, old["turn_id"])["snapshot"]["prompt"] == "旧指引"
        assert context_configuration(conn, new["turn_id"])["snapshot"]["prompt"] == "新指引"


@pytest.mark.asyncio
async def test_revocation_during_model_call_stops_next_dispatch(db, settings):
    from app.agent_tools import set_enabled as enable_tool
    from app.runtime import Runtime
    from app.trace import read_run
    from app.turns import read_turn

    auth = Identity("U001")
    draft = AgentDraft(
        name="在途撤权验收", model_profile_id="default", tool_grants={"find_invoices": ["invoke"]}
    )
    agent = create_draft(db, auth, draft)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    accepted = turn(db, agent["agent_id"])

    def revoke(payload):
        enable_tool(db, auth, "find_invoices", 0, False)
        return tool("find_invoices", {"company_id": "C001", "invoice_id": "F002"})

    model = ScriptedModel([revoke])
    runtime = Runtime(db, model_settings(settings), model, agent_runtime_enabled=True)
    execution = runtime.claim()
    await runtime.run(execution)
    assert len(model.requests) == 1
    assert read_turn(db, auth, accepted["turn_id"])["status"] == "failed"
    trace = read_run(db, auth, execution.run_id)
    assert not any(t["state"] == "done" for t in trace["tools"])
    provenance = trace["contexts"][0]["manifest"]["runtime_provenance"]
    assert len(provenance["source_bundle_id"]) == 64
    assert len(provenance["tool_contracts"]["find_invoices"]) == 64
    assert any(p["name"].lower() == "fastapi" and p["version"] for p in provenance["packages"])
    assert "api_key" not in str(provenance)


def test_real_invoice_commit_finishes_before_waiting_revocation(db, execution_factory, monkeypatch):
    import threading
    import time

    from app import invoice_write, mutations
    from app.agent_authority import bind_turn
    from app.agent_tools import set_enabled as enable_tool

    auth = Identity("U001")
    draft = AgentDraft(
        name="提交竞争验收",
        model_profile_id="default",
        tool_grants={"prepare_invoice_write": ["invoke"], "update_invoice": ["invoke"]},
    )
    agent = create_draft(db, auth, draft)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    text = "把甲企业F002用途填写为办公耗材采购。"
    execution = execution_factory(text)
    with db.transaction() as conn:
        conn.execute("UPDATE turns SET status='queued' WHERE turn_id=%s", (execution.turn_id,))
        bind_turn(conn, auth, execution.turn_id, agent["agent_id"])
        conn.execute("UPDATE turns SET status='running' WHERE turn_id=%s", (execution.turn_id,))
    plan = invoice_write.prepare(
        db,
        auth,
        company_id="C001",
        invoice_id="F002",
        patch={"purpose": "办公耗材采购"},
        expected_revision=1,
        source_id=execution.source_id,
        source_refs=[{"source_id": execution.source_id, "quote": text}],
        execution=execution,
    )
    assert plan["status"] == "authorized"
    execution.active_tool = ("update_invoice", {"plan_id": plan["plan_id"]})
    entered, release, revoked = threading.Event(), threading.Event(), threading.Event()
    failures, receipts = [], []
    original_guard = mutations.guard

    def pause_after_authorization(conn, current):
        result = original_guard(conn, current)
        entered.set()
        assert release.wait(8)
        return result

    monkeypatch.setattr(mutations, "guard", pause_after_authorization)

    def save():
        try:
            receipts.append(invoice_write.commit(db, auth, plan["plan_id"], execution=execution))
        except Exception as exc:
            failures.append(exc)

    def revoke():
        try:
            enable_tool(db, auth, "update_invoice", 0, False)
        except Exception as exc:
            failures.append(exc)
        finally:
            revoked.set()

    writer = threading.Thread(target=save)
    revoker = threading.Thread(target=revoke)
    writer.start()
    try:
        assert entered.wait(5)
        revoker.start()
        deadline = time.monotonic() + 3
        waiting = False
        while time.monotonic() < deadline:
            with db.transaction(readonly=True) as conn:
                waiting = conn.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted) AS waiting"
                ).fetchone()["waiting"]
            if waiting or revoked.wait(0.02):
                break
        assert waiting and not revoked.is_set()
    finally:
        release.set()
        writer.join(8)
        if revoker.ident:
            revoker.join(8)
    assert not writer.is_alive() and not revoker.is_alive()
    assert not failures
    assert len(receipts) == 1 and receipts[0]["commit_state"] == "committed"
    with db.transaction(readonly=True) as conn:
        assert conn.execute(
            "SELECT purpose,revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
        ).fetchone() == {"purpose": "办公耗材采购", "revision": 2}
        assert (
            conn.execute("SELECT count(*) AS n FROM operations WHERE state='committed'").fetchone()[
                "n"
            ]
            == 1
        )
