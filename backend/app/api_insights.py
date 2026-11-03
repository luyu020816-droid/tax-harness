"""Read-only, authenticated observability routes."""

from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app import insights
from app.errors import AppError
from app.values import canonical


def insight_router(db, auth_dependency):
    router = APIRouter(prefix="/api/v1/insights")

    def selected(current, start, end, company, status, model, as_of):
        return insights.filters(current, start, end, company, status, model, as_of)

    @router.get("/summary")
    def summary(
        start: str | None = Query(None, alias="from"),
        end: str | None = Query(None, alias="to"),
        company_id: str | None = None,
        status: str | None = None,
        model: str | None = None,
        as_of: str | None = None,
        current=auth_dependency,
    ):
        return insights.summary(
            db, current, selected(current, start, end, company_id, status, model, as_of)
        )

    @router.get("/evaluations")
    def evaluations(current=auth_dependency):
        from app.insight_evaluations import read

        return read(db, current)

    @router.get("/evaluations/{evaluation_id}")
    def evaluation(evaluation_id: UUID, current=auth_dependency):
        from app.insight_evaluations import read

        return read(db, current, str(evaluation_id))

    @router.get("/requests")
    def requests(
        start: str | None = Query(None, alias="from"),
        end: str | None = Query(None, alias="to"),
        company_id: str | None = None,
        status: str | None = None,
        model: str | None = None,
        as_of: str | None = None,
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=50),
        current=auth_dependency,
    ):
        return insights.requests(
            db,
            current,
            selected(current, start, end, company_id, status, model, as_of),
            cursor,
            limit,
        )

    @router.get("/requests/{turn_id}")
    def detail(turn_id: UUID, current=auth_dependency):
        return insights.detail(db, current, str(turn_id))

    @router.get("/runs/{run_id}")
    def run_request(run_id: UUID, current=auth_dependency):
        from app.insight_access import assert_turn

        with db.transaction(readonly=True) as conn:
            row = conn.execute(
                "SELECT turn_id FROM runs WHERE run_id=%s", (str(run_id),)
            ).fetchone()
            if not row:
                raise AppError("RESOURCE_NOT_FOUND", "运行记录不存在。", 404)
            turn = assert_turn(conn, current, str(row["turn_id"]))
            return {"turn_id": str(turn["turn_id"])}

    @router.get("/runs/{run_id}/spans")
    def spans(run_id: UUID, offset: int = Query(0, ge=0, le=100000), current=auth_dependency):
        return insights.read_spans(db, current, str(run_id), offset)

    @router.get("/spans/{span_id}")
    def node(span_id: UUID, offset: int = Query(0, ge=0), current=auth_dependency):
        return insights.node(db, current, str(span_id), offset)

    @router.get("/requests/{turn_id}/export")
    def export(turn_id: UUID, current=auth_dependency):
        # Metadata and controlled evidence are portable; raw nodes remain individually guarded.
        result = insights.detail(db, current, str(turn_id))
        if len(canonical(result).encode("utf-8")) > 10 * 1024 * 1024:
            raise AppError("EXPORT_TOO_LARGE", "导出超过10MiB，请按执行或节点查看。", 413)
        return JSONResponse(
            result,
            headers={"Content-Disposition": f'attachment; filename="insight-{turn_id}.json"'},
        )

    return router
