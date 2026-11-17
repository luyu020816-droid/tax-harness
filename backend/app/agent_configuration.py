"""Validation for published agent grants and reusable skill dependencies."""

from collections.abc import Mapping
from dataclasses import dataclass

from app.agent_capabilities import ACTION_TOOLS, ALIASES
from app.errors import AppError


@dataclass(frozen=True)
class SkillRequirements:
    enabled: bool
    tools: Mapping[str, frozenset[str]]
    requires_knowledge: bool = False


def validate_grants(grants: Mapping[str, frozenset[str]], registered: frozenset[str]) -> None:
    """Reject unknown tools, aliases and action grants before publishing."""
    for name, actions in grants.items():
        permitted = ACTION_TOOLS.get(name, frozenset({"invoke"}))
        if (
            name not in registered
            or name in ALIASES
            or not actions
            or not set(actions) <= permitted
        ):
            raise AppError("AGENT_CONFIG_INVALID", "存在未知工具、别名或无效的工具动作授权。", 422)


def validate_skill_dependencies(
    grants: Mapping[str, frozenset[str]],
    knowledge_mode: str,
    skills: Mapping[str, SkillRequirements],
) -> None:
    if knowledge_mode not in {"none", "authorized", "selected"}:
        raise AppError("AGENT_CONFIG_INVALID", "知识资料范围无效。", 422)
    for skill in skills.values():
        if not skill.enabled:
            raise AppError("AGENT_CONFIG_INVALID", "不能发布引用已停用 Skill 的配置。", 422)
        if skill.requires_knowledge and knowledge_mode == "none":
            raise AppError("AGENT_CONFIG_INVALID", "Skill 需要知识资料授权。", 422)
        if any(
            not actions <= grants.get(name, frozenset()) for name, actions in skill.tools.items()
        ):
            raise AppError("AGENT_CONFIG_INVALID", "Skill 依赖的工具动作尚未授权。", 422)
