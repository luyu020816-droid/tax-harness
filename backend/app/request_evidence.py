"""Resolve monthly tool parameters against user evidence, not model guesses."""

import re

from app.errors import AppError
from app.identity import assert_access
from app.sources import read_source
from app.tasks import current_in

PERIOD = re.compile(r"(?<!\d)(\d{4})\s*(?:年|[-/])\s*(1[0-2]|0?[1-9])\s*月?(?!\d)")
NEXT_MONTH = re.compile(r"\s*(?:和|与|及|、)\s*(1[0-2]|0?[1-9])\s*月(?!\d)")
BARE_MONTH = re.compile(r"(?<![\d年/-])(1[0-2]|0?[1-9])\s*月")


def explicit_periods(text: str) -> set[str]:
    periods = set()
    for match in PERIOD.finditer(text):
        year, month = map(int, match.groups())
        periods.add(f"{year:04}-{month:02}")
        offset = match.end()
        while following := NEXT_MONTH.match(text, offset):
            periods.add(f"{year:04}-{int(following[1]):02}")
            offset = following.end()
    return periods


def _task_period_source(conn, execution, task, company_id, period):
    candidates = conn.execute(
        "SELECT DISTINCT s.source_id FROM task_revisions r "
        "CROSS JOIN LATERAL jsonb_array_elements(r.source_refs) ref "
        "JOIN sources s ON s.source_id=(ref->>'source_id')::uuid "
        "WHERE r.task_id=%s AND r.snapshot->>'period'=%s "
        "AND s.user_id=%s AND s.role='user' AND s.source_seq<=%s",
        (task["task_id"], period, execution.auth.user_id, execution.source_seq),
    ).fetchall()
    for row in candidates:
        source = read_source(
            conn,
            execution.auth,
            str(row["source_id"]),
            company_id=company_id,
            allow_suppressed=True,
        )
        if period in explicit_periods(source["content"]):
            return {
                "source_id": str(source["source_id"]),
                "revision": source["revision"],
                "allow_suppressed": True,
            }
    return None


def require_period(conn, execution, company_id: str, period: str) -> dict:
    """Return a verified basis or fail; a company selector supplies no month."""
    assert_access(conn, execution.auth, company_id)
    source = read_source(conn, execution.auth, execution.source_id, allow_suppressed=True)
    current = explicit_periods(source["content"])
    if current:
        if period in current:
            return {
                "kind": "current_source",
                "source_id": str(source["source_id"]),
                "revision": source["revision"],
            }
        raise AppError("NEEDS_SCOPE_CLARIFICATION", "查询月份与本轮明确月份不一致。", 422)
    months = {int(m[1]) for m in BARE_MONTH.finditer(source["content"])}
    years = set(re.findall(r"(?<!\d)(\d{4})\s*年", source["content"]))
    if (months and int(period[5:]) not in months) or (years and period[:4] not in years):
        raise AppError("NEEDS_SCOPE_CLARIFICATION", "查询月份与本轮补充的信息不一致。", 422)
    task = current_in(conn, execution.auth, company_id)
    if task and task["period"] == period:
        if basis := _task_period_source(conn, execution, task, company_id, period):
            return {
                "kind": "task",
                "task_id": str(task["task_id"]),
                "task_revision": task["revision"],
                "generation": task["generation"],
                **basis,
            }
    # Use only the latest same-session user source whose explicit month actually
    # drove a successful query. Assistant text and unexecuted dates are not slots.
    rows = conn.execute(
        "SELECT s.source_id,s.content,e.arguments FROM turns t "
        "JOIN sources s ON s.turn_id=t.turn_id AND s.role='user' "
        "JOIN runs r ON r.turn_id=t.turn_id "
        "JOIN tool_executions e ON e.run_id=r.run_id "
        "WHERE t.user_id=%s AND t.session_id=%s AND t.source_seq<%s "
        "AND t.status IN ('completed','resolved','waiting_user') "
        "AND s.recall_state='active' AND e.state='done' "
        "AND e.name IN ('query_month','get_rules','check_purposes') "
        "AND e.arguments->>'company_id'=%s "
        "ORDER BY t.source_seq DESC,e.tool_call_id LIMIT 12",
        (execution.auth.user_id, execution.session_id, execution.source_seq, company_id),
    ).fetchall()
    if rows:
        latest_source = rows[0]["source_id"]
        latest = {
            row["arguments"].get("period")
            for row in rows
            if row["source_id"] == latest_source
            and row["arguments"].get("period") in explicit_periods(row["content"])
        }
        if latest == {period}:
            confirmed = read_source(
                conn, execution.auth, str(latest_source), company_id=company_id
            )
            return {
                "kind": "confirmed_history",
                "source_id": str(latest_source),
                "revision": confirmed["revision"],
            }
    raise AppError(
        "NEEDS_SCOPE_CLARIFICATION",
        "尚无已确认的查询月份，请只追问缺失的月份，不自行选择样例月份。",
        422,
    )
