import base64
import json
from decimal import Decimal

from app.errors import AppError
from app.identity import Identity, assert_access, owned
from app.rules import resolve_in, rule_ref
from app.values import digest, is_blank, jsonb, month_bounds, uid

PAGE_SIZE = 10


def _snapshot(conn, company_id: str) -> dict:
    return conn.execute(
        "SELECT company_id,data_revision,rules_epoch,transaction_timestamp() AS as_of "
        "FROM company_heads WHERE company_id=%s",
        (company_id,),
    ).fetchone()


def query_month(db, auth: Identity, company_id: str, period: str, metrics=None) -> dict:
    start, end = month_bounds(period)
    with db.transaction(readonly=True) as conn:
        assert_access(conn, auth, company_id)
        rules = resolve_in(conn, auth, company_id, period, ["amounts", "bank"])
        invoices = conn.execute(
            "SELECT count(*) AS invoice_count,coalesce(sum(amount),0)::numeric(18,2) AS invoice_total "
            "FROM invoices WHERE company_id=%s AND invoice_date>=%s AND invoice_date<%s",
            (company_id, start, end),
        ).fetchone()
        bank = conn.execute(
            "SELECT count(*) AS bank_count,count(*) FILTER(WHERE direction='income') AS income_count,"
            "count(*) FILTER(WHERE direction='expense') AS expense_count,"
            "coalesce(sum(amount) FILTER(WHERE direction='income'),0)::numeric(18,2) AS bank_income,"
            "coalesce(sum(amount) FILTER(WHERE direction='expense'),0)::numeric(18,2) AS bank_expense "
            "FROM bank_entries WHERE company_id=%s AND transaction_date>=%s AND transaction_date<%s",
            (company_id, start, end),
        ).fetchone()
        return {
            "kind": "monthly_summary",
            **_snapshot(conn, company_id),
            "period": period,
            **invoices,
            **bank,
            "has_records": bool(invoices["invoice_count"] or bank["bank_count"]),
            "rule_refs": [rule_ref(r) for r in rules],
        }


def _page(rows: list[dict], cursor: str | None, query_hash: str, head: dict) -> dict:
    offset = 0
    if cursor:
        try:
            data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            offset = int(data["offset"])
            valid = (
                offset >= 0
                and data["query"] == query_hash
                and all(data[k] == head[k] for k in ("data_revision", "rules_epoch"))
            )
            if not valid:
                raise ValueError
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise AppError("STALE_CONTEXT", "分页范围或数据版本已变化，请重新查询。", 409) from exc
    subset = rows[offset : offset + PAGE_SIZE]
    has_more = offset + PAGE_SIZE < len(rows)
    next_cursor = None
    if has_more:
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "offset": offset + PAGE_SIZE,
                    "query": query_hash,
                    "data_revision": head["data_revision"],
                    "rules_epoch": head["rules_epoch"],
                }
            ).encode()
        ).decode()
    return {
        "rows": subset,
        "total": len(rows),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "offset": offset,
    }


def _register_view(db, auth: Identity, session_id: str | None, result: dict) -> dict:
    if not session_id:
        return result
    with db.transaction() as conn:
        owned(conn, "sessions", "session_id", session_id, auth)
        assert_access(conn, auth, result["company_id"])
        head = conn.execute(
            "SELECT * FROM company_heads WHERE company_id=%s FOR SHARE", (result["company_id"],)
        ).fetchone()
        if any(head[k] != result[k] for k in ("data_revision", "rules_epoch")):
            raise AppError("STALE_CONTEXT", "查询期间资料发生变化，请重新查询。", 409)
        view_id = uid()
        keys = [{"invoice_id": r["invoice_id"], "revision": r["revision"]} for r in result["rows"]]
        conn.execute(
            "INSERT INTO result_views(view_id,user_id,session_id,company_id,period,ordered_keys,data_revision,rules_epoch) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                view_id,
                auth.user_id,
                session_id,
                result["company_id"],
                result.get("period"),
                jsonb(keys),
                result["data_revision"],
                result["rules_epoch"],
            ),
        )
    return {**result, "view_id": view_id}


def find_invoices(
    db,
    auth: Identity,
    company_id: str,
    period: str | None = None,
    ids: list[str] | None = None,
    seller: str | None = None,
    item: str | None = None,
    cursor=None,
    session_id=None,
) -> dict:
    filters = ["company_id=%s"]
    params: list = [company_id]
    if period:
        start, end = month_bounds(period)
        filters.extend(["invoice_date>=%s", "invoice_date<%s"])
        params.extend([start, end])
    if ids:
        filters.append("invoice_id=ANY(%s)")
        params.append(ids)
    if seller:
        filters.append("position(lower(%s) in lower(seller))>0")
        params.append(seller)
    if item:
        filters.append("position(lower(%s) in lower(item))>0")
        params.append(item)
    with db.transaction(readonly=True) as conn:
        assert_access(conn, auth, company_id)
        rows = conn.execute(
            "SELECT * FROM invoices WHERE "
            + " AND ".join(filters)
            + " ORDER BY invoice_date,invoice_id",
            params,
        ).fetchall()
        head = _snapshot(conn, company_id)
        page = _page(rows, cursor, digest([company_id, period, ids, seller, item]), head)
        result = {"kind": "invoices", **head, "period": period, **page}
    return _register_view(db, auth, session_id, result)


def check_purposes(
    db, auth: Identity, company_id: str, period: str, cursor=None, session_id=None
) -> dict:
    start, end = month_bounds(period)
    if period != "2026-09":
        raise AppError(
            "PERIOD_UNSUPPORTED", "初赛用途检查范围为 2026 年 9 月；其他月份可查询金额。", 422
        )
    with db.transaction(readonly=True) as conn:
        assert_access(conn, auth, company_id)
        rules = resolve_in(conn, auth, company_id, period, ["purpose"])
        rule = rules[0]
        condition = rule["condition_json"]
        if condition.get("kind") not in ("purpose_all", "purpose_threshold"):
            raise AppError("RULE_UNSUPPORTED", "用途规则条件超出支持范围。", 422)
        rows = conn.execute(
            "SELECT * FROM invoices WHERE company_id=%s AND invoice_date>=%s AND invoice_date<%s "
            "ORDER BY invoice_date,invoice_id",
            (company_id, start, end),
        ).fetchall()
        required = (
            rows
            if condition["kind"] == "purpose_all"
            else [row for row in rows if row["amount"] >= Decimal(condition["threshold"])]
        )
        missing = [r for r in required if is_blank(r["purpose"])]
        head = _snapshot(conn, company_id)
        page = _page(missing, cursor, digest([company_id, period, "purpose"]), head)
        result = {
            "kind": "purpose_check",
            **head,
            "period": period,
            **page,
            "missing_count": len(missing),
            "required_count": len(required),
            "blank_count": sum(is_blank(r["purpose"]) for r in rows),
            "rule_refs": [rule_ref(rule)],
        }
    return _register_view(db, auth, session_id, result)


def acknowledge_view(db, auth: Identity, view_id: str, turn_id: str, event_seq: int) -> dict:
    with db.transaction() as conn:
        view = owned(conn, "result_views", "view_id", view_id, auth)
        turn = owned(conn, "turns", "turn_id", turn_id, auth)
        if str(view["session_id"]) != str(turn["session_id"]):
            raise AppError("SOURCE_INVALIDATED", "列表不属于该会话。", 409)
        visible = [str(c.get("view_id")) for c in turn["final_cards"]]
        if (
            view_id not in visible
            or not conn.execute(
                "SELECT 1 FROM run_events WHERE turn_id=%s AND event_seq=%s AND kind='final'",
                (turn_id, event_seq),
            ).fetchone()
        ):
            raise AppError("SOURCE_INVALIDATED", "列表尚未正式展示。", 409)
        conn.execute(
            "UPDATE result_views SET displayed_event_seq=%s WHERE view_id=%s", (event_seq, view_id)
        )
        return {"view_id": view_id, "displayed": True}
