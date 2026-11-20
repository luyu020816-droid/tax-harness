"""Fail-closed capability decisions for configurable agents.

This module deliberately contains no database or execution side effects.  Its
decisions must be applied both when presenting tools and at the transaction
that performs an operation; presenting a filtered schema is not authorization.
"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass

from app.errors import AppError

# These tools have a single schema for multiple independently authorized actions.
ACTION_TOOLS = {
    "manage_preference": frozenset({"list", "remember", "update", "delete"}),
    "change_task": frozenset({"start", "update", "replace", "cancel", "delete"}),
}

# The model-facing alias delegates to two different tools.  Do not expose the
# alias to an agent: expose its independently authorized concrete tools instead.
ALIASES = {"lookup_knowledge": {"rules": "get_rules", "documents": "search_knowledge"}}


@dataclass(frozen=True)
class CapabilityPolicy:
    """One published grant and the current effective grant/status.

    Keys are registered tool names. Values are allowed action names; tools
    without action parameters use the sole action ``invoke``. Both grants are
    required: a later publication may revoke, but cannot expand an old run.
    """

    registered: frozenset[str]
    bound_grants: Mapping[str, frozenset[str]]
    current_grants: Mapping[str, frozenset[str]]
    enabled_tools: frozenset[str]
    agent_enabled: bool = True

    def actions(self, name: str) -> frozenset[str]:
        if not self.agent_enabled or name not in self.registered or name not in self.enabled_tools:
            return frozenset()
        known = ACTION_TOOLS.get(name, frozenset({"invoke"}))
        return (
            known
            & self.bound_grants.get(name, frozenset())
            & self.current_grants.get(name, frozenset())
        )

    def authorize(self, name: str, arguments: Mapping) -> str:
        target = resolve_target(name, arguments)
        target_arguments = arguments.get("request", {}) if name in ALIASES else arguments
        action = target_arguments.get("action") if target in ACTION_TOOLS else "invoke"
        if not isinstance(action, str) or action not in self.actions(target):
            raise AppError(
                "AGENT_CAPABILITY_DENIED", "当前 Agent 无权使用此能力或该能力已停用。", 403
            )
        return target


def resolve_target(name: str, arguments: Mapping) -> str:
    if name not in ALIASES:
        return name
    request = arguments.get("request")
    source_type = request.get("source_type") if isinstance(request, Mapping) else None
    if not isinstance(source_type, str):
        raise AppError("AGENT_CAPABILITY_DENIED", "无法确定知识查询的授权目标。", 403)
    target = ALIASES[name].get(source_type)
    if target is None:
        raise AppError("AGENT_CAPABILITY_DENIED", "无法确定知识查询的授权目标。", 403)
    return target


def filter_definitions(definitions: list[dict], policy: CapabilityPolicy) -> list[dict]:
    """Return only granted tools, narrowing multi-action schemas where needed."""
    result = []
    for original in definitions:
        name = original["function"]["name"]
        if name in ALIASES:
            continue
        actions = policy.actions(name)
        if not actions:
            continue
        definition = deepcopy(original)
        if name in ACTION_TOOLS:
            properties = definition["function"]["parameters"].get("properties", {})
            action_schema = properties.get("action")
            if not isinstance(action_schema, dict) or not isinstance(
                action_schema.get("enum"), list
            ):
                # A changed schema must never accidentally expose every action.
                continue
            action_schema["enum"] = [
                action for action in action_schema["enum"] if action in actions
            ]
            if not action_schema["enum"]:
                continue
        result.append(definition)
    return result


def offered_actions(definitions):
    available = {}
    for item in definitions:
        function = item["function"]
        name = function["name"]
        available[name] = (
            frozenset(
                function["parameters"].get("properties", {}).get("action", {}).get("enum", [])
            )
            if name in ACTION_TOOLS
            else frozenset({"invoke"})
        )
    return available


def missing_actions(requirements, available):
    return {
        name: sorted(set(actions) - available(name))
        for name, actions in requirements.items()
        if not set(actions) <= available(name)
    }


def restrict_document_ids(
    mode: str, allowed: frozenset[str], requested: list[str] | None
) -> list[str] | None:
    """Apply agent document scope without expanding the caller's selection.

    ``None`` means the existing user/company authorization selects documents;
    an empty list must never be passed to a retrieval API that treats it as all.
    """
    if mode == "none":
        raise AppError("AGENT_CAPABILITY_DENIED", "当前 Agent 未获知识资料访问权限。", 403)
    if mode == "authorized":
        return requested or None
    if mode != "selected" or not allowed:
        raise AppError("AGENT_CAPABILITY_DENIED", "当前 Agent 知识资料范围不可用。", 403)
    if requested is None or not requested:
        return sorted(allowed)
    if not set(requested) <= allowed:
        raise AppError("AGENT_CAPABILITY_DENIED", "所选资料不在当前 Agent 的授权范围内。", 403)
    return requested
