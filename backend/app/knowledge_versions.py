"""Shared publication and applicability checks for text, files and workbooks."""

from datetime import date

from app import business_dates
from app.errors import AppError
from app.values import month_bounds


def window(as_of=None, period=None):
    if as_of and period:
        raise AppError("INVALID_ARGUMENT", "资料生效日期与业务月份不能同时指定。", 422)
    if period:
        try:
            return month_bounds(period)
        except (ValueError, OverflowError) as exc:
            raise AppError("INVALID_ARGUMENT", "业务月份超出可计算范围。", 422) from exc
    return as_of or business_dates.business_today(), None


def selected_in(conn, auth, document_id, start, end=None):
    return conn.execute(
        "SELECT * FROM knowledge_version_at(%s,%s,%s,%s)", (document_id, start, end, auth.user_id)
    ).fetchone()


def recorded_window(snapshot):
    start = date.fromisoformat(snapshot["start"])
    end = date.fromisoformat(snapshot["end"]) if snapshot.get("period") else None
    return start, end


def timing(start, end=None):
    today = business_dates.business_today()
    return {
        "as_of": start.isoformat() if end is None else None,
        "start": start.isoformat(),
        "end_exclusive": end.isoformat() if end else None,
        "timezone": business_dates.BUSINESS_ZONE,
        "applicability": "future"
        if start > today
        else "historical"
        if (end <= today if end else start < today)
        else "current",
    }


def record_publication(conn, document, version_id):
    conn.execute(
        "INSERT INTO knowledge_publications(document_id,publication_revision,version_id,scope,origin) "
        "VALUES(%s,%s,%s,%s,'publish')",
        (document["document_id"], document["revision"], version_id, document["scope"]),
    )
