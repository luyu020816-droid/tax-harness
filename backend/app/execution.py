from dataclasses import dataclass, field

from app.errors import AppError
from app.identity import Identity
from app.sources import read_source


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


def guard(conn, execution: Execution, *, shared=False) -> dict:
    from app.recovery import in_maintenance

    if in_maintenance(f"dbname={conn.info.dbname}"):
        raise AppError("MAINTENANCE", "恢复核对中，旧执行不能继续提交。", 503)
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
        turn
        and work
        and turn["status"] == "running"
        and turn["active_run_generation"] == execution.generation
        and work["state"] == "running"
        and work["lease_valid"]
        and work["fencing_token"] == execution.fencing_token
    )
    if not valid:
        raise AppError("SOURCE_INVALIDATED", "本次执行已取消、超时或由新执行接管。", 409)
    if user["memory_epoch"] != execution.expected_epoch:
        raise AppError("STALE_CONTEXT", "任务或记忆已更新，本次旧上下文不能继续提交。", 409)
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
