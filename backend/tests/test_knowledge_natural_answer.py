import json

import pytest
from pydantic import ValidationError
from test_knowledge import uploaded
from test_runtime import ScriptedModel, model_settings, tool

from app.identity import Identity, create_session
from app.knowledge_grounding import validated_indices
from app.runtime import Runtime
from app.tool_schemas import FinalReply
from app.turns import accept_turn, read_turn
from app.values import uid

pytest_plugins = ["test_knowledge"]


def test_claim_protocol_rejects_missing_evidence():
    with pytest.raises(ValidationError):
        FinalReply.model_validate(
            {
                "blocks": [
                    {
                        "type": "knowledge_answer",
                        "coverage": "supported",
                        "claims": [{"text": "437元", "citation_ids": []}],
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        FinalReply.model_validate(
            {
                "blocks": [
                    {
                        "type": "knowledge_answer",
                        "coverage": "insufficient",
                        "claims": [{"text": "437元", "citation_ids": [uid()]}],
                    }
                ]
            }
        )


def test_verdict_quote_binding_and_number_validation():
    claims = [{"text": "南京上限437元", "evidence": {"a": "南京上限1437元"}}]
    raw = json.dumps(
        {
            "verdicts": [
                {
                    "claim_index": 0,
                    "status": "supported",
                    "supporting_quotes": [{"citation_id": "a", "quote": "南京上限1437元"}],
                }
            ]
        },
        ensure_ascii=False,
    )
    assert validated_indices(claims, raw) == set()
    assert validated_indices(claims, "{") == set()


def test_place_quote_and_exception_are_not_silently_rewritten():
    text = "南京住宿上限437元。确需超标时，应入住前取得书面批准。"
    claims = [{"text": "北京住宿上限437元。", "evidence": {"a": text}}]

    def response(quote):
        return json.dumps(
            {
                "verdicts": [
                    {
                        "claim_index": 0,
                        "status": "supported",
                        "supporting_quotes": [{"citation_id": "a", "quote": quote}],
                    }
                ]
            },
            ensure_ascii=False,
        )

    assert validated_indices(claims, response(text)) == set()
    claims[0]["text"] = "南京住宿上限437元。"
    assert validated_indices(claims, response("无此原文")) == set()
    assert validated_indices(claims, response(text)) == set()  # exception omitted


def test_unverified_natural_claim_cannot_be_approved_by_wrong_index():
    claims = [{"text": "南京住宿437元", "evidence": {"a": "南京住宿437元"}}]
    raw = json.dumps(
        {
            "verdicts": [
                {
                    "claim_index": 1,
                    "status": "supported",
                    "supporting_quotes": [{"citation_id": "a", "quote": "南京住宿437元"}],
                }
            ]
        },
        ensure_ascii=False,
    )
    assert validated_indices(claims, raw) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["supported", "unsupported", "invalid"])
async def test_answer_uses_only_verified_actual_citations(db, settings, knowledge, verdict):
    created = uploaded(knowledge, "青杉计划员工到南京出差，住宿报销上限为每人每晚 437 元。")
    auth = Identity("U001")
    session = str(create_session(db, auth, "C001")["session_id"])
    turn = accept_turn(db, auth, session, uid(), "南京出差住宿报销上限是多少？")

    def answer(payload):
        data = json.loads(payload["messages"][-1]["content"])["data"]
        citation = data["evidence"][0]["citation_id"]
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "blocks": [
                        {
                            "type": "knowledge_answer",
                            "coverage": "supported",
                            "claims": [
                                {
                                    "text": "南京出差住宿上限为每人每晚 437 元。",
                                    "citation_ids": [citation],
                                }
                            ],
                        }
                    ],
                    "status": "completed",
                },
                ensure_ascii=False,
            ),
        }

    def check(payload):
        claim = json.loads(payload["messages"][-1]["content"])["claims"][0]
        citation, quote = next(iter(claim["evidence"].items()))
        if verdict == "invalid":
            return {"role": "assistant", "content": "{"}
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "verdicts": [
                        {
                            "claim_index": 0,
                            "status": verdict,
                            "supporting_quotes": [{"citation_id": citation, "quote": quote}],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        }

    model = ScriptedModel(
        [tool("search_knowledge", {"company_id": "C001", "query": "南京住宿报销"}), answer, check]
    )
    runtime = Runtime(db, model_settings(settings), model, retrieval=knowledge[2])
    await runtime.run(runtime.claim())
    result = read_turn(db, auth, turn["turn_id"])
    assert result["status"] == "completed", result
    if verdict == "supported":
        assert "每人每晚 437 元" in result["final_message"]
        assert "来源1" in result["final_message"]
        assert result["final_cards"][0]["kind"] == "knowledge_quote"
        assert result["final_cards"][0]["reference_label"] == "来源1"
        response_source = result["final_response_ref"]
        document = knowledge[0].get(auth, created["document_id"])
        knowledge[0].change(
            auth,
            created["document_id"],
            action="disable",
            expected_revision=document["revision"],
            request_id=uid(),
        )
        hidden = read_turn(db, auth, turn["turn_id"])
        assert "437" not in hidden["final_message"]
        assert hidden["final_cards"] == []
        from app.errors import AppError
        from app.sources import read_source

        with pytest.raises(AppError), db.transaction(readonly=True) as conn:
            read_source(conn, auth, response_source)
        from app.conversation_projection import groups

        accept_turn(db, auth, session, uid(), "继续说")
        execution = runtime.claim()
        excluded = []
        projected = groups(db, execution, excluded=excluded)
        assert "437" not in json.dumps(projected, ensure_ascii=False)
        assert any(item.get("kind") == "knowledge_answer" for item in excluded)
    else:
        if verdict == "invalid":
            assert "暂时无法完成答案核验" in result["final_message"]
        else:
            assert "没有找到足以确认答案的资料" in result["final_message"]
        assert not result["final_cards"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_quote", [False, True])
async def test_irrelevant_retrieval_is_not_cited_when_answer_is_insufficient(
    db, settings, knowledge, legacy_quote
):
    uploaded(knowledge, "南京出差住宿上限每人每晚437元。")
    auth = Identity("U001")
    session = str(create_session(db, auth, "C001")["session_id"])
    turn = accept_turn(db, auth, session, uid(), "北京出差呢？")

    def result_block(payload):
        blocks = [{"type": "knowledge_answer", "coverage": "insufficient", "claims": []}]
        if legacy_quote:
            citation = json.loads(payload["messages"][-1]["content"])["data"]["evidence"][0][
                "citation_id"
            ]
            blocks.append({"type": "knowledge_quote", "citation_id": citation})
        return {
            "role": "assistant",
            "content": json.dumps({"blocks": blocks, "status": "completed"}, ensure_ascii=False),
        }

    model = ScriptedModel(
        [tool("search_knowledge", {"company_id": "C001", "query": "北京出差住宿"}), result_block]
    )
    runtime = Runtime(db, model_settings(settings), model, retrieval=knowledge[2])
    await runtime.run(runtime.claim())
    result = read_turn(db, auth, turn["turn_id"])
    assert result["status"] == "completed", result
    assert "没有找到足以确认答案" in result["final_message"]
    assert "437" not in result["final_message"]
    assert result["final_cards"] == []
    assert len(model.requests) == 2  # no verifier call without claims


@pytest.mark.asyncio
async def test_revoked_document_during_verification_does_not_deliver_claim(db, settings, knowledge):
    created = uploaded(knowledge, "南京出差住宿上限每人每晚437元。")
    auth = Identity("U001")
    session = str(create_session(db, auth, "C001")["session_id"])
    turn = accept_turn(db, auth, session, uid(), "南京住宿限额？")

    def answer(payload):
        citation = json.loads(payload["messages"][-1]["content"])["data"]["evidence"][0][
            "citation_id"
        ]
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "blocks": [
                        {
                            "type": "knowledge_answer",
                            "coverage": "supported",
                            "claims": [{"text": "南京住宿上限437元。", "citation_ids": [citation]}],
                        }
                    ],
                    "status": "completed",
                },
                ensure_ascii=False,
            ),
        }

    def revoke(payload):
        document = knowledge[0].get(auth, created["document_id"])
        knowledge[0].change(
            auth,
            created["document_id"],
            action="disable",
            expected_revision=document["revision"],
            request_id=uid(),
        )
        claim = json.loads(payload["messages"][-1]["content"])["claims"][0]
        citation, quote = next(iter(claim["evidence"].items()))
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "verdicts": [
                        {
                            "claim_index": 0,
                            "status": "supported",
                            "supporting_quotes": [{"citation_id": citation, "quote": quote}],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        }

    runtime = Runtime(
        db,
        model_settings(settings),
        ScriptedModel(
            [tool("search_knowledge", {"company_id": "C001", "query": "南京住宿"}), answer, revoke]
        ),
        retrieval=knowledge[2],
    )
    await runtime.run(runtime.claim())
    result = read_turn(db, auth, turn["turn_id"])
    assert result["status"] != "completed"
    assert not result["final_message"]
