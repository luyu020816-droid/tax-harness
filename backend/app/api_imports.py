from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field, ValidationError

from app.config import ROOT
from app.errors import AppError
from app.tool_schemas import StrictModel
from app.xlsx_invoices import MAX_FILE_BYTES


class UploadBodyLimit:
    """Bound actual request bytes before multipart parsing, including chunked bodies."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("path") != "/api/v1/uploads/invoices"
            or scope.get("method") != "POST"
        ):
            return await self.app(scope, receive, send)
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_FILE_BYTES + 65536:
                return await JSONResponse(
                    {"code": "UPLOAD_TOO_LARGE", "message": "上传请求超过 5 MiB 文件上限。"},
                    status_code=413,
                )(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body"):
                break
        delivered = False

        async def buffered_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, buffered_receive, send)


class UploadArgs(StrictModel):
    company_id: str = Field(pattern=r"^C\d+$")
    session_id: UUID
    client_request_id: str = Field(min_length=1, max_length=200)


class CreateBody(StrictModel):
    upload_id: UUID
    session_id: UUID
    client_request_id: str = Field(min_length=1, max_length=200)


class PreviewBody(StrictModel):
    session_id: UUID
    expected_revision: int = Field(ge=1)
    client_request_id: str = Field(min_length=1, max_length=200)
    policy: Literal["strict", "skip_issues"] = "strict"


class ConfirmBody(StrictModel):
    session_id: UUID
    expected_revision: int = Field(ge=1)
    preview_hash: str = Field(min_length=64, max_length=64)
    confirmation_token: str = Field(min_length=1, max_length=100)
    client_request_id: str = Field(min_length=1, max_length=200)


class CancelBody(StrictModel):
    expected_revision: int = Field(ge=1)
    client_request_id: str = Field(min_length=1, max_length=200)


def import_router(service, auth_dependency):
    router = APIRouter(prefix="/api/v1")

    @router.get("/invoice-import-template")
    def template(current=auth_dependency):
        return FileResponse(ROOT / "examples/invoice-append.xlsx", filename="invoice-append.xlsx")

    @router.post("/uploads/invoices", status_code=201)
    async def upload(request: Request, current=auth_dependency):
        async with request.form(max_files=1, max_fields=3) as form:
            file = form.get("file")
            if not isinstance(file, UploadFile):
                # Starlette supplies its own UploadFile base instance.
                from starlette.datastructures import UploadFile as StarletteUpload

                if not isinstance(file, StarletteUpload):
                    raise AppError("WORKBOOK_UNSUPPORTED", "请选择一个 XLSX 文件。", 422)
            try:
                args = UploadArgs.model_validate(
                    {k: form.get(k) for k in ("company_id", "session_id", "client_request_id")}
                )
            except ValidationError as exc:
                raise AppError(
                    "INVALID_ARGUMENT", "上传的企业、会话或请求编号不合法。", 422
                ) from exc
            content = await file.read(MAX_FILE_BYTES + 1)
            import asyncio

            return await asyncio.to_thread(
                service.upload,
                current,
                **args.model_dump(mode="json"),
                filename=file.filename,
                content=content,
            )

    @router.post("/invoice-imports", status_code=201)
    def create(body: CreateBody, current=auth_dependency):
        return service.create(current, **body.model_dump(mode="json"))

    @router.post("/invoice-imports/{batch_id}/preview")
    def preview(batch_id: UUID, body: PreviewBody, current=auth_dependency):
        return service.preview(current, str(batch_id), **body.model_dump(mode="json"))

    @router.get("/invoice-imports")
    def list_batches(company_id: str | None = None, cursor: int = 0, current=auth_dependency):
        return service.list(current, company_id, cursor)

    @router.get("/invoice-imports/{batch_id}")
    def get(batch_id: UUID, cursor: int = 0, current=auth_dependency):
        return service.get(current, str(batch_id), cursor)

    @router.post("/invoice-imports/{batch_id}/confirm")
    def confirm(batch_id: UUID, body: ConfirmBody, current=auth_dependency):
        return service.confirm(current, str(batch_id), **body.model_dump(mode="json"))

    @router.post("/invoice-imports/{batch_id}/cancel")
    def cancel(batch_id: UUID, body: CancelBody, current=auth_dependency):
        return service.cancel(current, str(batch_id), **body.model_dump(mode="json"))

    return router
