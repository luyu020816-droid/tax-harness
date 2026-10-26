import asyncio
import hashlib
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg.conninfo import conninfo_to_dict
from pydantic import Field

from app import business, identity, preferences, rules, tasks, trace, turns
from app.api_imports import UploadBodyLimit, import_router
from app.config import ROOT, Settings
from app.db import Database
from app.errors import AppError
from app.identity import Identity
from app.invoice_imports import InvoiceImports
from app.model import HttpModel
from app.runtime import Runtime
from app.tool_schemas import StrictModel
from app.values import canonical, safe_json


class LoginBody(StrictModel):
    user_id: str


class SessionBody(StrictModel):
    selected_company_id: str | None = None


class TurnBody(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=16000)
    upload_ids: list[UUID] = Field(default_factory=list, max_length=1)


class ControlBody(StrictModel):
    request_id: str = Field(min_length=1, max_length=200)
    expected_revision: int | None = Field(default=None, ge=1)


class PreferenceBody(ControlBody):
    scope: str | None = None
    key: str | None = None
    value: str | None = Field(default=None, max_length=1000)


class TaskBody(ControlBody):
    action: str = "update"
    company_id: str | None = None
    period: str | None = None
    intent: str | None = None
    unresolved: dict | None = None


class ViewBody(StrictModel):
    turn_id: UUID
    event_seq: int = Field(ge=1)


def create_app(
    settings=None, db=None, model=None, *, start_workers=True, ingest_db=None
) -> FastAPI:
    settings = settings or Settings()
    own_database = db is None
    db = db or Database(settings.database_url)
    model = model or HttpModel(settings)
    runtime = Runtime(db, settings, model)
    own_ingest = ingest_db is None and bool(settings.ingest_database_url)
    ingest_db = ingest_db or (Database(settings.ingest_database_url) if own_ingest else None)
    imports = InvoiceImports(db, ingest_db)
    connection = conninfo_to_dict(settings.database_url)
    database_name = connection.get("dbname", "")
    # Cookies are shared across localhost ports. Keep independent instances isolated.
    cookie_name = "tax_session"
    if database_name not in ("tax_harness", "tax_harness_test"):
        instance = "|".join(connection.get(key, "") for key in ("host", "port", "dbname"))
        cookie_name += "_" + hashlib.sha256(instance.encode()).hexdigest()[:12]

    @asynccontextmanager
    async def lifespan(app):
        if own_database:
            db.open()
        if own_ingest:
            ingest_db.open()
        if start_workers:
            await runtime.start()
        try:
            yield
        finally:
            if start_workers:
                await runtime.stop()
            if own_database:
                db.close()
            if own_ingest:
                ingest_db.close()

    app = FastAPI(title="财税连续办理助手", version="0.1.0", lifespan=lifespan)
    app.state.db, app.state.runtime, app.state.settings = db, runtime, settings

    def auth(request: Request) -> Identity:
        return identity.authenticate(db, request.cookies.get(cookie_name))

    auth_dependency = Depends(auth)
    app.include_router(import_router(imports, auth_dependency))
    app.add_middleware(UploadBodyLimit)
    app.state.imports = imports

    @app.middleware("http")
    async def same_origin(request, call_next):
        from app.recovery import in_maintenance

        if (
            request.url.path.startswith("/api/")
            and request.url.path not in ("/api/v1/health", "/api/v1/ready")
            and in_maintenance(settings.database_url)
        ):
            return JSONResponse(
                {"code": "MAINTENANCE", "message": "数据库恢复核对中，业务与记忆暂不开放。"},
                status_code=503,
            )
        origin = request.headers.get("origin")
        if request.method in ("POST", "PATCH", "DELETE", "PUT") and origin:
            if urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse(
                    {"code": "ACCESS_DENIED", "message": "拒绝跨站修改请求。"}, status_code=403
                )
        return await call_next(request)

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse(
            {**exc.as_dict(), "request_id": request.headers.get("x-request-id")},
            status_code=exc.status,
        )

    @app.exception_handler(psycopg.Error)
    async def database_error(request, exc):
        return JSONResponse(
            {
                "code": "DB_UNAVAILABLE",
                "message": "数据库操作未能完成，请核对已有操作状态。",
                "retryable": True,
            },
            status_code=503,
        )

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "simulation": True}

    @app.get("/api/v1/ready")
    def ready():
        from app.recovery import in_maintenance

        maintenance = in_maintenance(settings.database_url)
        try:
            database_ready = db.ready()
        except psycopg.Error:
            database_ready = False
        return JSONResponse(
            {
                "database": database_ready,
                "model": settings.model_configured,
                "model_mode": model.mode,
                "maintenance": maintenance,
                "invoice_import_ready": imports.available(),
            },
            status_code=200 if database_ready and not maintenance else 503,
        )

    @app.get("/api/v1/demo/users")
    def demo_users():
        if not settings.demo_mode:
            raise AppError("ACCESS_DENIED", "模拟入口已关闭。", 403)
        with db.transaction(readonly=True) as conn:
            return conn.execute(
                "SELECT user_id,display_name FROM users ORDER BY user_id"
            ).fetchall()

    @app.post("/api/v1/demo/login")
    def login(body: LoginBody):
        if not settings.demo_mode:
            raise AppError("ACCESS_DENIED", "模拟登录已关闭。", 403)
        response = JSONResponse({"user_id": body.user_id})
        response.set_cookie(
            cookie_name,
            identity.login(db, body.user_id),
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            max_age=43200,
        )
        return response

    @app.get("/api/v1/me")
    def me(current=auth_dependency):
        return {
            **identity.me(db, current),
            "model_ready": settings.model_configured,
            "model_mode": model.mode,
            "invoice_import_ready": imports.available(),
        }

    @app.get("/api/v1/sessions")
    def sessions(current=auth_dependency):
        return turns.list_sessions(db, current)

    @app.post("/api/v1/sessions", status_code=201)
    def new_session(body: SessionBody, current=auth_dependency):
        return safe_json(identity.create_session(db, current, body.selected_company_id))

    @app.patch("/api/v1/sessions/{session_id}")
    def change_session(session_id: UUID, body: SessionBody, current=auth_dependency):
        return safe_json(
            identity.switch_company(db, current, str(session_id), body.selected_company_id)
        )

    @app.get("/api/v1/sessions/{session_id}/messages")
    def messages(session_id: UUID, cursor: int | None = None, current=auth_dependency):
        return turns.session_messages(db, current, str(session_id), cursor)

    @app.post("/api/v1/sessions/{session_id}/turns", status_code=202)
    def send(session_id: UUID, body: TurnBody, current=auth_dependency):
        for secret in (settings.model_api_key, urlparse(settings.database_url).password):
            if secret and len(secret) >= 8 and secret in body.text:
                raise AppError("INVALID_ARGUMENT", "消息包含本地配置凭据，请移除后发送。")
        result = turns.accept_turn(
            db,
            current,
            str(session_id),
            body.client_request_id,
            body.text,
            upload_ids=[str(x) for x in body.upload_ids],
        )
        return {**result, "events_url": f"/api/v1/turns/{result['turn_id']}/events"}

    @app.get("/api/v1/turns/{turn_id}")
    def turn(turn_id: UUID, current=auth_dependency):
        return turns.read_turn(db, current, str(turn_id))

    @app.post("/api/v1/turns/{turn_id}/cancel")
    def cancel(turn_id: UUID, body: ControlBody, current=auth_dependency):
        return turns.cancel_turn(db, current, str(turn_id), body.request_id, body.expected_revision)

    @app.get("/api/v1/pending-clarifications")
    def pending(current=auth_dependency):
        return turns.list_pending(db, current)

    @app.delete("/api/v1/turns/{turn_id}/pending-clarification")
    def delete_pending(turn_id: UUID, body: ControlBody, current=auth_dependency):
        return turns.cancel_turn(
            db, current, str(turn_id), body.request_id, body.expected_revision, delete_pending=True
        )

    @app.get("/api/v1/turns/{turn_id}/events")
    async def events(turn_id: UUID, request: Request, current=auth_dependency):
        await asyncio.to_thread(turns.read_turn, db, current, str(turn_id))
        try:
            after = int(request.headers.get("last-event-id", "0"))
        except ValueError as exc:
            raise AppError("INVALID_ARGUMENT", "事件游标无效。") from exc

        async def stream():
            nonlocal after
            while not await request.is_disconnected():

                def read_events(after_cursor=after):
                    with db.transaction(readonly=True) as conn:
                        identity.owned(conn, "turns", "turn_id", str(turn_id), current)
                        turns._assert_turn_access(conn, current, str(turn_id))
                        rows = conn.execute(
                            "SELECT * FROM run_events WHERE turn_id=%s AND event_seq>%s ORDER BY event_seq LIMIT 100",
                            (turn_id, after_cursor),
                        ).fetchall()
                        state = conn.execute(
                            "SELECT status FROM turns WHERE turn_id=%s", (turn_id,)
                        ).fetchone()["status"]
                        return rows, state

                rows, state = await asyncio.to_thread(read_events)
                for row in rows:
                    after = row["event_seq"]
                    data = (
                        {"step": row["payload"].get("step")}
                        if row["kind"] == "model_finished"
                        else row["payload"]
                    )
                    yield f"id: {after}\nevent: {row['kind']}\ndata: {canonical(data)}\n\n"
                if state not in ("queued", "running") and len(rows) < 100:
                    return
                if not rows:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.4)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/tasks")
    def task_list(company_id: str | None = None, current=auth_dependency):
        return safe_json(tasks.list_tasks(db, current, company_id))

    @app.patch("/api/v1/tasks/{task_id}")
    def edit_task(task_id: UUID, body: TaskBody, current=auth_dependency):
        with db.transaction(readonly=True) as conn:
            old = identity.owned(conn, "tasks", "task_id", str(task_id), current)
        args = body.model_dump(exclude_unset=True)
        args.setdefault("company_id", old["company_id"])
        args.setdefault("action", "update")
        return safe_json(tasks.change_task(db, current, target_id=str(task_id), **args))

    @app.delete("/api/v1/tasks/{task_id}")
    def delete_task(task_id: UUID, body: ControlBody, current=auth_dependency):
        with db.transaction(readonly=True) as conn:
            old = identity.owned(conn, "tasks", "task_id", str(task_id), current)
        return safe_json(
            tasks.change_task(
                db,
                current,
                company_id=old["company_id"],
                action="delete",
                target_id=str(task_id),
                **body.model_dump(),
            )
        )

    @app.get("/api/v1/preferences")
    def preference_list(company_id: str | None = None, current=auth_dependency):
        return safe_json(preferences.list_preferences(db, current, company_id))

    @app.post("/api/v1/preferences", status_code=201)
    def new_preference(body: PreferenceBody, current=auth_dependency):
        args = body.model_dump(exclude_unset=True)
        args["scope"] = args.get("scope") or "@user"
        return safe_json(preferences.change_preference(db, current, action="remember", **args))

    @app.patch("/api/v1/preferences/{preference_id}")
    def edit_preference(preference_id: UUID, body: PreferenceBody, current=auth_dependency):
        with db.transaction(readonly=True) as conn:
            old = identity.owned(conn, "preferences", "preference_id", str(preference_id), current)
        args = body.model_dump(exclude_unset=True)
        args["scope"] = args.get("scope") or old["scope_key"]
        return safe_json(
            preferences.change_preference(
                db, current, action="update", target_id=str(preference_id), **args
            )
        )

    @app.delete("/api/v1/preferences/{preference_id}")
    def delete_preference(preference_id: UUID, body: ControlBody, current=auth_dependency):
        with db.transaction(readonly=True) as conn:
            old = identity.owned(conn, "preferences", "preference_id", str(preference_id), current)
        return safe_json(
            preferences.change_preference(
                db,
                current,
                action="delete",
                scope=old["scope_key"],
                target_id=str(preference_id),
                **body.model_dump(),
            )
        )

    @app.get("/api/v1/companies/{company_id}/invoices")
    def invoices(
        company_id: str,
        period: str | None = None,
        cursor: str | None = None,
        current=auth_dependency,
    ):
        return safe_json(
            business.find_invoices(db, current, company_id=company_id, period=period, cursor=cursor)
        )

    @app.get("/api/v1/rules")
    def applicable_rules(company_id: str, period: str, topic: str, current=auth_dependency):
        return safe_json(rules.get_rules(db, current, company_id, period, topic.split(",")))

    @app.get("/api/v1/sources/{source_id}")
    def source_details(source_id: UUID, current=auth_dependency):
        from app.sources import read_source

        with db.transaction(readonly=True) as conn:
            return safe_json(read_source(conn, current, str(source_id), allow_suppressed=True))

    @app.get("/api/v1/runs")
    def runs(company_id: str | None = None, current=auth_dependency):
        return trace.list_runs(db, current, company_id)

    @app.get("/api/v1/runs/{run_id}")
    def run_details(run_id: UUID, current=auth_dependency):
        return trace.read_run(db, current, str(run_id))

    @app.get("/api/v1/runs/{run_id}/export")
    def export_run(run_id: UUID, current=auth_dependency):
        result = trace.read_run(db, current, str(run_id))
        return JSONResponse(
            result, headers={"Content-Disposition": f'attachment; filename="run-{run_id}.json"'}
        )

    @app.post("/api/v1/views/{view_id}/displayed")
    def displayed(view_id: UUID, body: ViewBody, current=auth_dependency):
        return business.acknowledge_view(
            db, current, str(view_id), str(body.turn_id), body.event_seq
        )

    assets = ROOT / "frontend/dist/assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path.startswith("api/"):
            raise AppError("RESOURCE_NOT_FOUND", "接口不存在。", 404)
        index = ROOT / "frontend/dist/index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse({"message": "前端尚未构建，请运行 npm run build。"}, status_code=503)

    return app
