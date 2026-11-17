import pytest

from app.agent_authority import history_scope_allows
from app.agent_capabilities import (
    ACTION_TOOLS,
    CapabilityPolicy,
    filter_definitions,
    restrict_document_ids,
)
from app.agent_configuration import SkillRequirements, validate_grants, validate_skill_dependencies
from app.errors import AppError
from app.tool_schemas import TOOLS, tool_definitions


def policy(bound, current=None, *, enabled=None, agent_enabled=True):
    return CapabilityPolicy(
        registered=frozenset(TOOLS),
        bound_grants={key: frozenset(value) for key, value in bound.items()},
        current_grants={key: frozenset(value) for key, value in (current or {}).items()},
        enabled_tools=frozenset(TOOLS if enabled is None else enabled),
        agent_enabled=agent_enabled,
    )


def test_capability_requires_both_bound_and_current_grants():
    bound = {"find_invoices": {"invoke"}}
    assert policy(bound, bound).authorize("find_invoices", {}) == "find_invoices"
    with pytest.raises(AppError, match="无权"):
        policy(bound).authorize("find_invoices", {})
    with pytest.raises(AppError, match="无权"):
        policy({}, bound).authorize("find_invoices", {})


def test_disabled_and_unregistered_tools_fail_closed():
    grants = {"find_invoices": {"invoke"}, "made_up": {"invoke"}}
    for p, tool in [
        (policy(grants, grants, enabled=set()), "find_invoices"),
        (policy(grants, grants, agent_enabled=False), "find_invoices"),
        (policy(grants, grants, enabled=grants), "made_up"),
    ]:
        with pytest.raises(AppError):
            p.authorize(tool, {})


def test_multi_action_tool_is_filtered_and_enforced():
    grants = {"manage_preference": {"list"}}
    p = policy(grants, grants)
    definitions = filter_definitions(tool_definitions(), p)
    assert len(definitions) == 1
    assert definitions[0]["function"]["parameters"]["properties"]["action"]["enum"] == ["list"]
    assert p.authorize("manage_preference", {"action": "list"}) == "manage_preference"
    for args in ({"action": "delete"}, {}, {"action": None}):
        with pytest.raises(AppError):
            p.authorize("manage_preference", args)


def test_alias_authorizes_concrete_target_and_is_not_exposed():
    grants = {"search_knowledge": {"invoke"}}
    p = policy(grants, grants)
    assert (
        p.authorize("lookup_knowledge", {"request": {"source_type": "documents"}})
        == "search_knowledge"
    )
    for args in (
        {"request": {"source_type": "rules"}},
        {"request": {"source_type": []}},
        {"request": {}},
        {},
    ):
        with pytest.raises(AppError):
            p.authorize("lookup_knowledge", args)
    names = {item["function"]["name"] for item in filter_definitions(tool_definitions(), p)}
    assert names == {"search_knowledge"}


def test_selected_knowledge_scope_never_expands_or_turns_empty_into_all():
    allowed = frozenset({"a", "b"})
    assert restrict_document_ids("selected", allowed, None) == ["a", "b"]
    assert restrict_document_ids("selected", allowed, ["b"]) == ["b"]
    assert restrict_document_ids("authorized", allowed, None) is None
    for mode, permitted, requested in [
        ("none", allowed, None),
        ("selected", frozenset(), None),
        ("selected", allowed, ["c"]),
        ("invalid", allowed, None),
    ]:
        with pytest.raises(AppError):
            restrict_document_ids(mode, permitted, requested)


def test_capability_filter_does_not_mutate_original_schema():
    original = tool_definitions()
    grants = {"manage_preference": {"list"}}
    filter_definitions(original, policy(grants, grants))
    pref = next(x for x in original if x["function"]["name"] == "manage_preference")
    assert set(pref["function"]["parameters"]["properties"]["action"]["enum"]) == {
        "list",
        "remember",
        "update",
        "delete",
    }


def test_every_tool_with_action_is_explicitly_classified():
    assert {name for name, (schema, _) in TOOLS.items() if "action" in schema.model_fields} == set(
        ACTION_TOOLS
    )
    for name, expected in ACTION_TOOLS.items():
        assert set(TOOLS[name][0].model_json_schema()["properties"]["action"]["enum"]) == expected


def test_grants_reject_unknown_aliases_and_invalid_actions():
    registered = frozenset(TOOLS)
    validate_grants({"find_invoices": frozenset({"invoke"})}, registered)
    for grants in (
        {"fake": frozenset({"invoke"})},
        {"lookup_knowledge": frozenset({"invoke"})},
        {"manage_preference": frozenset({"invoke"})},
        {"manage_preference": frozenset()},
    ):
        with pytest.raises(AppError):
            validate_grants(grants, registered)


def test_skill_dependency_does_not_grant_tool_or_knowledge_access():
    skill = SkillRequirements(
        enabled=True, tools={"search_knowledge": frozenset({"invoke"})}, requires_knowledge=True
    )
    grants = {"search_knowledge": frozenset({"invoke"})}
    validate_skill_dependencies(grants, "authorized", {"knowledge": skill})
    with pytest.raises(AppError):
        validate_skill_dependencies({}, "authorized", {"knowledge": skill})
    with pytest.raises(AppError):
        validate_skill_dependencies(grants, "none", {"knowledge": skill})
    with pytest.raises(AppError):
        validate_skill_dependencies(
            grants, "authorized", {"knowledge": SkillRequirements(False, {})}
        )


def test_restricted_agent_history_requires_equal_or_narrower_document_scope():
    none = {"knowledge_mode": "none", "knowledge_document_ids": []}
    selected = {"knowledge_mode": "selected", "knowledge_document_ids": ["a", "b"]}
    only_a = {"knowledge_mode": "selected", "knowledge_document_ids": ["a"]}
    other = {"knowledge_mode": "selected", "knowledge_document_ids": ["c"]}
    authorized = {"knowledge_mode": "authorized", "knowledge_document_ids": []}
    assert history_scope_allows(selected, none)
    assert history_scope_allows(selected, only_a)
    assert not history_scope_allows(selected, other)
    assert not history_scope_allows(selected, authorized)
    assert not history_scope_allows(none, selected)
    assert not history_scope_allows(none, None)
    assert history_scope_allows(authorized, None)
