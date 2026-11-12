from app.runtime import _scope_clarification_needs_review
from app.tool_schemas import FinalReply


def question(field):
    return FinalReply.model_validate(
        {
            "blocks": [],
            "status": "waiting_user",
            "clarification": {"intent": "query_month", "missing_fields": [field]},
        }
    )


def group(name, result):
    return {
        "messages": [{"role": "assistant", "tool_calls": [{"function": {"name": name}}]}],
        "results": [result],
    }


def test_rule_scope_failure_requires_a_second_look():
    failure = group("get_rules", {"ok": False, "error": {"code": "NEEDS_SCOPE_CLARIFICATION"}})
    assert _scope_clarification_needs_review(question("period"), [failure])
    assert not _scope_clarification_needs_review(question("company_id"), [failure])
    assert not _scope_clarification_needs_review(
        question("period"),
        [group("query_month", {"ok": False, "error": {"code": "NEEDS_SCOPE_CLARIFICATION"}})],
    )


def test_existing_successful_knowledge_result_does_not_trigger_reconsideration():
    failure = group("get_rules", {"ok": False, "error": {"code": "NEEDS_SCOPE_CLARIFICATION"}})
    knowledge = group("search_knowledge", {"ok": True, "data": {"kind": "knowledge_search"}})
    assert not _scope_clarification_needs_review(question("period"), [failure, knowledge])
