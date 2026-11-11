"""Resolve monthly tool parameters against user evidence, not model guesses."""

import json
import re
from datetime import timedelta, timezone
from itertools import groupby

from app.errors import AppError
from app.identity import assert_access
from app.sources import read_source
from app.tasks import current_in
from app.values import digest, jsonb, uid

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


def request_periods(text, created_at=None):
    """Ignore quoted instructions, negative query clauses and date examples."""

    def quote(match):
        inner = match[0][1:-1]
        return inner if PERIOD.fullmatch(inner) else " "

    text = re.sub(r'“[^”]*”|「[^」]*」|"[^"]*"', quote, text)
    clauses = re.split(r"[，,；;。\n]", text)
    text = "，".join(
        c for c in clauses if not re.search(r"不要查|别查|不查询|无需查询|例如|比如|示例", c)
    )
    periods = explicit_periods(text)
    if created_at:
        today = created_at.astimezone(timezone(timedelta(hours=8))).date()
        if any(w in text for w in ("本月", "这个月")):
            periods.add(f"{today.year:04}-{today.month:02}")
        if "上个月" in text:
            previous = today.replace(day=1) - timedelta(days=1)
            periods.add(f"{previous.year:04}-{previous.month:02}")
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
        if period in request_periods(source["content"], source["created_at"]):
            return {
                "source_id": str(source["source_id"]),
                "revision": source["revision"],
                "allow_suppressed": True,
            }
    return None


def _history_basis(conn, execution, company_id):
    rows = conn.execute(
        "SELECT t.turn_id,t.status,t.source_seq,s.source_id,s.content,s.created_at,e.name,e.arguments,e.result_source_id,b.basis_json FROM turns t "
        "JOIN sources s ON s.turn_id=t.turn_id AND s.role='user' "
        "JOIN runs r ON r.turn_id=t.turn_id JOIN tool_executions e ON e.run_id=r.run_id "
        "LEFT JOIN scope_bindings b ON b.binding_id=(e.arguments->>'scope_ref')::uuid "
        "AND b.user_id=t.user_id AND b.turn_id=t.turn_id "
        "WHERE t.user_id=%s AND t.session_id=%s AND t.source_seq<%s "
        "AND s.recall_state='active' AND e.state='done' "
        "AND t.status IN ('completed','resolved','waiting_user','failed') "
        "AND e.name IN ('query_month','get_rules','check_purposes','find_invoices') "
        "AND e.arguments->>'period' IS NOT NULL "
        "AND e.arguments->>'company_id'=%s ORDER BY t.source_seq DESC,e.tool_call_id LIMIT 12",
        (execution.auth.user_id, execution.session_id, execution.source_seq, company_id),
    ).fetchall()
    skipped_refs, skipped_period = [], None
    for _, entries in groupby(rows, key=lambda row: row["source_id"]):
        candidates = list(entries)
        periods = {row["arguments"].get("period") for row in candidates}
        if len(periods) != 1 or None in periods:
            return None
        period = next(iter(periods))
        for candidate in candidates:
            if candidate["name"] != "find_invoices" and candidate["status"] != "failed":
                continue
            # A failed reply or an invoice search is reusable only when the
            # user explicitly stated the month and the actual result confirms it.
            stated = request_periods(candidate["content"], candidate["created_at"])
            if stated != {period} or any(w in candidate["content"] for w in ("还是", "或者")):
                return None
            if not candidate["result_source_id"]:
                return None
            result = read_source(
                conn, execution.auth, str(candidate["result_source_id"]), company_id=company_id
            )
            try:
                output = json.loads(result["content"])
                data = output["data"]
                valid = (
                    result["role"] == "tool"
                    and str(result["turn_id"]) == str(candidate["turn_id"])
                    and output.get("ok") is True
                    and data.get("kind")
                    == {
                        "find_invoices": "invoices",
                        "query_month": "monthly_summary",
                        "get_rules": "rules",
                        "check_purposes": "purpose_check",
                    }[candidate["name"]]
                    and data.get("company_id") == company_id
                    and data.get("period") == period
                )
            except (TypeError, ValueError, KeyError, AttributeError):
                valid = False
            if not valid:
                return None
            skipped_refs.append(
                {"source_id": str(result["source_id"]), "revision": result["revision"]}
            )
            # A later explicit correction invalidates this earlier query scope.
            from app.turns import mentioned_companies

            later = conn.execute(
                "SELECT s.content,s.created_at FROM turns t JOIN sources s ON s.turn_id=t.turn_id "
                "AND s.role='user' WHERE t.user_id=%s AND t.session_id=%s "
                "AND t.source_seq>%s AND t.source_seq<%s AND s.recall_state='active' "
                "ORDER BY t.source_seq",
                (
                    execution.auth.user_id,
                    execution.session_id,
                    candidate["source_seq"],
                    execution.source_seq,
                ),
            ).fetchall()
            for item in later:
                named = mentioned_companies(conn, item["content"])
                if named and company_id not in named:
                    continue
                stated_later = request_periods(item["content"], item["created_at"])
                if stated_later and stated_later != {period}:
                    return None
                bare = {int(m[1]) for m in BARE_MONTH.finditer(item["content"])}
                if bare and bare != {int(period[5:])}:
                    return None
                years = set(re.findall(r"(?<!\d)(\d{4})\s*年", item["content"]))
                if years and years != {period[:4]}:
                    return None
                if any(w in item["content"] for w in ("下个月", "去年", "明年")):
                    return None
        if skipped_period is not None and period != skipped_period:
            return None
        row = candidates[0]
        # A later check of the same request may have the scope binding even if
        # an earlier tool did not; never select a different month implicitly.
        bound = next((item for item in candidates if item["basis_json"]), None)
        if bound:
            row = bound
        basis = row["basis_json"]
        if basis or period in request_periods(row["content"], row["created_at"]):
            break
        # Only host-generated, verified post-save rechecks may be transparent.
        # Other unproven/ambiguous requests stop the search, rather than revive
        # an older month after the user may have changed scope.
        text = row["content"]
        if BARE_MONTH.search(text) or re.search(
            r"(?<!\d)\d{4}\s*年|本月|这个月|上个月|下个月|去年|今年|明年", text
        ):
            return None
        source = read_source(conn, execution.auth, str(row["source_id"]), company_id=company_id)
        for candidate in candidates:
            if not candidate["result_source_id"]:
                return None
            result = read_source(
                conn, execution.auth, str(candidate["result_source_id"]), company_id=company_id
            )
            try:
                output = json.loads(result["content"])
                data = output["data"]
                valid = (
                    result["role"] == "tool"
                    and str(result["turn_id"]) == str(candidate["turn_id"])
                    and output.get("ok") is True
                    and output.get("origin") == "program_after_save"
                    and data.get("kind") == "purpose_check"
                    and data.get("company_id") == company_id
                    and data.get("period") == period
                )
            except (TypeError, ValueError, KeyError, AttributeError):
                valid = False
            if not valid:
                return None
            skipped_refs.append(
                {"source_id": str(result["source_id"]), "revision": result["revision"]}
            )
        skipped_refs.append({"source_id": str(source["source_id"]), "revision": source["revision"]})
        skipped_period = period
    else:
        return None
    source = read_source(conn, execution.auth, str(row["source_id"]), company_id=company_id)
    refs = [*skipped_refs, *(basis or {}).get("context_sources", [])]
    if basis:
        refs.append(
            {k: v for k, v in basis.items() if k in ("source_id", "revision", "allow_suppressed")}
        )
    for ref in refs:
        original = read_source(
            conn,
            execution.auth,
            ref["source_id"],
            company_id=company_id,
            allow_suppressed=ref.get("allow_suppressed", False),
        )
        if original["revision"] != ref["revision"]:
            raise AppError("SOURCE_INVALIDATED", "已确认月份的依据已变化。", 409)
    return period, {
        "kind": "confirmed_history",
        "source_id": str(source["source_id"]),
        "revision": source["revision"],
        "context_sources": refs,
    }


def require_period(conn, execution, company_id: str, period: str) -> dict:
    """Return a verified basis or fail; a company selector supplies no month."""
    assert_access(conn, execution.auth, company_id)
    source = read_source(conn, execution.auth, execution.source_id, allow_suppressed=True)
    current = request_periods(source["content"], source["created_at"])
    if current:
        if len(current) > 1 and any(word in source["content"] for word in ("还是", "或者")):
            raise AppError("NEEDS_SCOPE_CLARIFICATION", "有多个候选月份，请先确认范围。", 422)
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
    previous = None
    if task and task["period"]:
        if basis := _task_period_source(conn, execution, task, company_id, task["period"]):
            previous = (
                task["period"],
                {
                    "kind": "task",
                    "task_id": str(task["task_id"]),
                    "task_revision": task["revision"],
                    "generation": task["generation"],
                    **basis,
                },
            )
    # A successful query is a confirmed session slot, with its complete source chain.
    previous = _history_basis(conn, execution, company_id) or previous
    if previous:
        previous_period, basis = previous
        if months and len(months) == 1 and period[:4] == previous_period[:4]:
            return {
                "kind": "current_month_with_confirmed_year",
                "source_id": execution.source_id,
                "revision": source["revision"],
                "context_sources": [
                    {
                        k: v
                        for k, v in basis.items()
                        if k in ("source_id", "revision", "allow_suppressed")
                    },
                    *basis.get("context_sources", []),
                ],
            }
        if not months and period == previous_period:
            return basis
    raise AppError(
        "NEEDS_SCOPE_CLARIFICATION",
        "尚无已确认的查询月份，请只追问缺失的月份，不自行选择样例月份。",
        422,
    )


def bind_scope(conn, execution, company_id, period):
    from app.turns import mentioned_companies

    source = read_source(conn, execution.auth, execution.source_id, allow_suppressed=True)
    requested = mentioned_companies(conn, source["content"])
    if requested and company_id not in requested:
        raise AppError("TARGET_SCOPE_MISMATCH", "不能以另一家企业代替本轮明确指定的企业。", 422)
    basis = require_period(conn, execution, company_id, period)
    fingerprint = digest(basis)
    row = conn.execute(
        "INSERT INTO scope_bindings(binding_id,user_id,turn_id,company_id,period,basis_json,basis_hash) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(turn_id,company_id,period,basis_hash) "
        "DO UPDATE SET basis_hash=excluded.basis_hash RETURNING *",
        (
            uid(),
            execution.auth.user_id,
            execution.turn_id,
            company_id,
            period,
            jsonb(basis),
            fingerprint,
        ),
    ).fetchone()
    return {
        "scope_ref": str(row["binding_id"]),
        "company_id": company_id,
        "period": period,
        "basis": basis,
    }


def validate_scope(conn, execution, scope_ref, company_id, period):
    row = conn.execute(
        "SELECT * FROM scope_bindings WHERE binding_id=%s AND user_id=%s AND turn_id=%s",
        (scope_ref, execution.auth.user_id, execution.turn_id),
    ).fetchone()
    if not row or row["company_id"] != company_id or row["period"] != period:
        raise AppError("NEEDS_SCOPE_CLARIFICATION", "范围引用与当前企业月份不一致。", 422)
    assert_access(conn, execution.auth, company_id)
    basis = row["basis_json"]
    if basis.get("source_id"):
        # A current user request may explicitly name several authorized
        # companies. bind_scope already checked this company's user evidence;
        # read_source still validates ownership and access to EVERY source
        # scope. Its default exact-single-company filter is for isolated
        # evidence and must not reject this multi-company request itself.
        current_request = (
            basis.get("kind") == "current_source" and basis["source_id"] == execution.source_id
        )
        source = read_source(
            conn,
            execution.auth,
            basis["source_id"],
            company_id=None if current_request else company_id,
            allow_suppressed=basis.get("allow_suppressed", False),
        )
        if source["revision"] != basis["revision"]:
            raise AppError("SOURCE_INVALIDATED", "范围依据已更正。", 409)
    for ref in basis.get("context_sources", []):
        source = read_source(
            conn,
            execution.auth,
            ref["source_id"],
            company_id=company_id,
            allow_suppressed=ref.get("allow_suppressed", False),
        )
        if source["revision"] != ref["revision"]:
            raise AppError("SOURCE_INVALIDATED", "范围的上下文依据已更正。", 409)
    return basis


def prepare_frame(db, execution, core):
    from app.execution import guard
    from app.request_checklist import bind_waiting_items, compile_items, waiting_items
    from app.turns import mentioned_companies

    with db.transaction() as conn:
        guard(conn, execution, shared=True)
        source = read_source(conn, execution.auth, execution.source_id, allow_suppressed=True)
        companies = mentioned_companies(conn, source["content"])
        origin = None
        inherited = []
        # A bare full month immediately after a checklist clarification is a
        # scoped continuation. Other phrasing can use the explicit resume path.
        if PERIOD.fullmatch(source["content"].strip().rstrip("。.!！").strip()):
            prior = conn.execute(
                "SELECT turn_id,status,final_response_ref,final_cards FROM turns "
                "WHERE user_id=%s AND session_id=%s AND source_seq<%s AND request_kind<>'compact' "
                "ORDER BY source_seq DESC LIMIT 1",
                (execution.auth.user_id, execution.session_id, execution.source_seq),
            ).fetchone()
            if prior and prior["status"] == "waiting_user" and prior["final_response_ref"]:
                items = waiting_items(prior)
                if items and all(
                    i["company_id"]
                    and i["period"] is None
                    and core["selected_company_id"] in (None, i["company_id"])
                    for i in items
                ):
                    try:
                        read_source(conn, execution.auth, str(prior["final_response_ref"]))
                    except AppError as exc:
                        if exc.code not in ("ACCESS_DENIED", "SOURCE_INVALIDATED"):
                            raise
                    else:
                        origin, inherited = str(prior["turn_id"]), items
                        companies = sorted({i["company_id"] for i in items})
        if not companies and core["selected_company_id"]:
            companies = [core["selected_company_id"]]
        available = []
        for company in companies:
            try:
                assert_access(conn, execution.auth, company)
                periods = request_periods(source["content"], source["created_at"])
                if not periods:
                    months = {int(m[1]) for m in BARE_MONTH.finditer(source["content"])}
                    task = current_in(conn, execution.auth, company)
                    if task and task["period"]:
                        periods.add(task["period"])
                    else:
                        rows = conn.execute(
                            "SELECT e.arguments FROM tool_executions e JOIN runs r USING(run_id) JOIN turns t USING(turn_id) "
                            "WHERE t.user_id=%s AND t.session_id=%s AND t.source_seq<%s AND e.state='done' "
                            "AND e.name IN ('query_month','get_rules','check_purposes','find_invoices') "
                            "AND e.arguments->>'period' IS NOT NULL AND e.arguments->>'company_id'=%s "
                            "ORDER BY t.source_seq DESC LIMIT 12",
                            (
                                execution.auth.user_id,
                                execution.session_id,
                                execution.source_seq,
                                company,
                            ),
                        ).fetchall()
                        periods.update(row["arguments"]["period"] for row in rows)
                    if len(months) == 1:
                        periods = {p[:4] + f"-{next(iter(months)):02}" for p in periods}
                for period in sorted(periods):
                    try:
                        available.append(bind_scope(conn, execution, company, period))
                    except AppError as exc:
                        if exc.code not in (
                            "NEEDS_SCOPE_CLARIFICATION",
                            "TARGET_SCOPE_MISMATCH",
                            "ACCESS_DENIED",
                        ):
                            raise
            except AppError as exc:
                if exc.code != "ACCESS_DENIED":
                    raise
        catalogue = conn.execute(
            "SELECT company_id,aliases FROM companies ORDER BY company_id"
        ).fetchall()
        checklist = compile_items(
            source["content"],
            catalogue,
            core["selected_company_id"],
            available,
            source["created_at"],
        )
        if origin:
            continued = bind_waiting_items(inherited, available)
            if all(i["company_id"] and i["period"] for i in continued):
                checklist.extend(continued)
            else:
                origin = None
        return {
            "source_id": execution.source_id,
            "source_revision": source["revision"],
            "received_at": source["created_at"].isoformat(),
            "timezone": "Asia/Shanghai",
            "current_date": source["created_at"]
            .astimezone(timezone(timedelta(hours=8)))
            .date()
            .isoformat(),
            "requested_companies": companies,
            "available_scope_refs": available,
            **({"request_items": checklist} if checklist else {}),
            **({"checklist_origin_turn_id": origin} if origin else {}),
        }
