"""The experiment must actually vary policies and reject false success."""

import json

import pytest

from app import context
from app.config import ROOT


@pytest.mark.parametrize("phrase", ["未找到原文。", "没有找到匹配原文。"])
def test_empty_history_requires_real_empty_tool_result(monkeypatch, phrase):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    from verify_conversations_live import empty_history_answer

    result = {"final_message": phrase, "final_cards": []}
    context = {
        "payload": {
            "messages": [
                {
                    "role": "tool",
                    "content": json.dumps(
                        {
                            "data": {
                                "kind": "conversation_search",
                                "status": "not_found",
                                "matches": [],
                            }
                        }
                    ),
                }
            ]
        }
    }
    assert empty_history_answer(result, [context])
    assert not empty_history_answer(result, [])
    assert not empty_history_answer(
        {**result, "final_cards": [{"kind": "history_quote"}]}, [context]
    )
    assert not empty_history_answer({**result, "final_message": "找到了原文。"}, [context])


@pytest.mark.parametrize(
    "profile,size,search",
    [("fixed12", 12, False), ("budget", 30, False), ("budget_search", 30, True)],
)
def test_comparison_really_changes_projection_and_tools(monkeypatch, profile, size, search):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    from evaluate_conversation_strategies import strategy

    def original(*args, **kwargs):
        return list(range(30))

    monkeypatch.setattr(context, "conversation_groups", original)
    definitions = context.tool_definitions()
    with strategy(profile):
        assert len(context.conversation_groups()) == size
        names = {tool["function"]["name"] for tool in context.tool_definitions()}
        assert ("search_conversations" in names) is search
        assert "read_conversation" in names
        assert "update_invoice" in names  # Business guards/tools are not ablated.
    assert context.conversation_groups is original
    assert context.tool_definitions() == definitions


def test_unknown_experiment_profile_is_rejected(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    from evaluate_conversation_strategies import strategy

    with pytest.raises(ValueError, match="Unknown strategy"), strategy("unknown"):
        pass
