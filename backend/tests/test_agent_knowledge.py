import json

import pytest
from test_knowledge import knowledge as knowledge
from test_knowledge import uploaded

from app.agent_authority import bind_turn
from app.agent_profiles import AgentDraft, create_draft, publish, set_enabled, update_draft
from app.errors import AppError
from app.identity import Identity
from app.tools import ToolExecutor
from app.values import uid


def test_agent_knowledge_search_and_citation_stay_within_selected_documents(
    db, execution_factory, knowledge
):
    first = uploaded(knowledge, text="出差住宿限额为每天三百元。")
    second = uploaded(knowledge, text="出差住宿限额为每天五百元。")
    _, _, retrieval = knowledge
    other = retrieval.search(
        Identity("U001"),
        company_id="C001",
        query="出差住宿限额",
        document_ids=[second["document_id"]],
    )
    assert other["evidence"]
    config = AgentDraft(
        name="限定知识 Agent",
        model_profile_id="default",
        knowledge_mode="selected",
        knowledge_document_ids=[first["document_id"]],
        tool_grants={"search_knowledge": ["invoke"], "read_evidence": ["invoke"]},
    )
    auth = Identity("U001")
    agent = create_draft(db, auth, config)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    execution = execution_factory("出差住宿限额是多少？")
    with db.transaction() as conn:
        conn.execute("UPDATE turns SET status='queued' WHERE turn_id=%s", (execution.turn_id,))
        bind_turn(conn, auth, execution.turn_id, agent["agent_id"])
        conn.execute("UPDATE turns SET status='running' WHERE turn_id=%s", (execution.turn_id,))
    executor = ToolExecutor(db, retrieval)

    def call(name, arguments):
        return {"id": uid(), "function": {"name": name, "arguments": json.dumps(arguments)}}

    found, _ = executor.execute(
        execution, call("search_knowledge", {"company_id": "C001", "query": "出差住宿限额"})
    )
    assert found["ok"] and found["data"]["evidence"]
    assert {item["document_id"] for item in found["data"]["evidence"]} == {first["document_id"]}
    denied, _ = executor.execute(
        execution, call("read_evidence", {"citation_id": other["evidence"][0]["citation_id"]})
    )
    assert not denied["ok"] and denied["error"]["code"] == "AGENT_CAPABILITY_DENIED"
    assert "五百" not in json.dumps(denied, ensure_ascii=False)


def test_agent_with_no_knowledge_cannot_search_even_when_tool_was_granted(
    db, execution_factory, knowledge
):
    auth = Identity("U001")
    config = AgentDraft(
        name="无知识 Agent",
        model_profile_id="default",
        knowledge_mode="none",
        tool_grants={"search_knowledge": ["invoke"]},
    )
    agent = create_draft(db, auth, config)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    execution = execution_factory("出差住宿限额是多少？")
    with db.transaction() as conn:
        conn.execute("UPDATE turns SET status='queued' WHERE turn_id=%s", (execution.turn_id,))
        bind_turn(conn, auth, execution.turn_id, agent["agent_id"])
        conn.execute("UPDATE turns SET status='running' WHERE turn_id=%s", (execution.turn_id,))
    call = {
        "id": uid(),
        "function": {
            "name": "search_knowledge",
            "arguments": json.dumps(
                {
                    "company_id": "C001",
                    "query": "出差住宿限额",
                }
            ),
        },
    }
    result, _ = ToolExecutor(db, knowledge[2]).execute(execution, call)
    assert not result["ok"] and result["error"]["code"] == "AGENT_CAPABILITY_DENIED"


def test_knowledge_attachment_scope_and_in_flight_revocation(
    db, settings, execution_factory, knowledge
):
    from test_runtime import model_settings

    from app import context

    allowed = uploaded(knowledge, text="允许资料的正文。")
    forbidden = uploaded(knowledge, text="不得发送的另一份资料。")
    auth = Identity("U001")
    config = AgentDraft(
        name="附件隔离验收",
        model_profile_id="default",
        knowledge_mode="selected",
        knowledge_document_ids=[allowed["document_id"]],
        tool_grants={"search_knowledge": ["invoke"], "read_evidence": ["invoke"]},
    )
    agent = create_draft(db, auth, config)
    agent = publish(db, auth, agent["agent_id"], agent["revision"])
    agent = set_enabled(db, auth, agent["agent_id"], agent["revision"], True)
    execution = execution_factory("读取附件制度")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE turns SET status='queued',knowledge_upload_ids=%s WHERE turn_id=%s",
            ([forbidden["file_id"]], execution.turn_id),
        )
        bind_turn(conn, auth, execution.turn_id, agent["agent_id"])
        conn.execute("UPDATE turns SET status='running' WHERE turn_id=%s", (execution.turn_id,))
    with pytest.raises(AppError, match="授权范围"):
        context.assemble(db, model_settings(settings), execution, [])
    with db.transaction() as conn:
        conn.execute(
            "UPDATE turns SET knowledge_upload_ids=%s WHERE turn_id=%s",
            ([allowed["file_id"]], execution.turn_id),
        )
    payload, manifest, _ = context.assemble(db, model_settings(settings), execution, [])
    assert manifest["current_knowledge_attachments"][0]["document_id"] == allowed["document_id"]
    agent = update_draft(
        db,
        auth,
        agent["agent_id"],
        agent["revision"],
        config.model_copy(update={"knowledge_document_ids": [forbidden["document_id"]]}),
    )
    publish(db, auth, agent["agent_id"], agent["revision"])
    with pytest.raises(AppError, match="知识范围已缩小"):
        context.validate(db, execution, payload, manifest)
