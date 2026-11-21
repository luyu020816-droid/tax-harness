from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.errors import AppError
from app.identity import Identity
from app.sources import read_source

if TYPE_CHECKING:
    from app.config import Settings
    from app.model import ModelAdapter


@dataclass
class Execution:
    auth: Identity
    turn_id: str
    session_id: str
    source_id: str
    source_seq: int
    work_id: str
    run_id: str
    generation: int
    fencing_token: int
    expected_epoch: int
    observed_heads: dict = field(default_factory=dict)
    import_context: bool = False
    reference_context: bool = False
    active_tool: tuple[str, dict] | None = field(default=None, repr=False)
    model_configuration: tuple["Settings", "ModelAdapter"] | None = field(default=None, repr=False)
    invoice_recovery_id: str | None = None
    memory_companies: frozenset[str] = field(default_factory=frozenset)
    history_companies: tuple[str, ...] | None = None


def memory_is_current(conn, execution, epoch):
    from app.memory_changes import compatible_epoch

    return compatible_epoch(
        conn, execution.auth, execution.memory_companies, execution.expected_epoch, epoch,
    )


def guard(conn, execution: Execution, *, shared=False, receipt_only=False) -> dict:
    from app.recovery import in_maintenance

    if in_maintenance(f"dbname={conn.info.dbname}"):
        raise AppError("MAINTENANCE", "恢复核对中，旧执行不能继续提交。", 503)
    # Configuration writers take the exclusive form before touching user rows.
    # Acquire the shared lock first to preserve that ordering in business writes.
    from app.agent_authority import LOCK_ID

    conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (LOCK_ID,))
    suffix = " FOR SHARE" if shared else " FOR UPDATE"
    user = conn.execute(
        "SELECT * FROM users WHERE user_id=%s" + suffix, (execution.auth.user_id,)
    ).fetchone()
    turn = conn.execute(
        "SELECT * FROM turns WHERE turn_id=%s AND user_id=%s" + suffix,
        (execution.turn_id, execution.auth.user_id),
    ).fetchone()
    work = conn.execute(
        "SELECT *,lease_until>clock_timestamp() AS lease_valid FROM work_items WHERE work_id=%s"
        + suffix,
        (execution.work_id,),
    ).fetchone()
    valid = (
        user
        and user["is_active"]
        and turn
        and work
        and turn["status"] == "running"
        and turn["active_run_generation"] == execution.generation
        and work["state"] == "running"
        and work["lease_valid"]
        and work["fencing_token"] == execution.fencing_token
    )
    session = conn.execute(
        "SELECT deleted_at FROM sessions WHERE session_id=%s", (execution.session_id,)
    ).fetchone()
    valid = valid and session and not session["deleted_at"]
    if not valid:
        raise AppError("SOURCE_INVALIDATED", "本次执行已取消、超时或由新执行接管。", 409)
    if not memory_is_current(conn, execution, user["memory_epoch"]):
        raise AppError("STALE_CONTEXT", "任务或记忆已更新，本次旧上下文不能继续提交。", 409)
    from app.agent_authority import authorize_tool, policy_for_turn, validate_snapshot

    policy = None if receipt_only else policy_for_turn(conn, execution.turn_id)
    if policy is not None:
        if not policy.agent_enabled:
            raise AppError("AGENT_UNAVAILABLE", "Agent 已停用，不能继续执行。", 409)
        validate_snapshot(conn, execution.turn_id)
    if not receipt_only and execution.active_tool:
        authorize_tool(conn, execution.turn_id, *execution.active_tool)
    read_source(conn, execution.auth, execution.source_id, allow_suppressed=True)
    return user


def guard_company(conn, execution: Execution | None, company_id: str) -> dict:
    head = conn.execute(
        "SELECT * FROM company_heads WHERE company_id=%s FOR UPDATE", (company_id,)
    ).fetchone()
    observed = execution.observed_heads.get(company_id) if execution else None
    if observed and any(head[k] != observed[k] for k in ("data_revision", "rules_epoch")):
        raise AppError("STALE_CONTEXT", "企业资料或规则已变化，请重新查询后继续。", 409)
    return head
