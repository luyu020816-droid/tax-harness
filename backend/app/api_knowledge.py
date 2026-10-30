from datetime import date
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import Field, ValidationError
from starlette.datastructures import UploadFile

from app.errors import AppError
from app.tool_schemas import StrictModel


class UploadKnowledge(StrictModel):
    company_id: str = Field(pattern=r"^C\d+$")
    title: str = Field(min_length=1, max_length=200)
    request_id: str = Field(min_length=1, max_length=200)
    expected_document_revision: int | None = Field(default=None, ge=1)
    effective_from: date | None = None
    effective_to: date | None = None


class KnowledgeControl(StrictModel):
    request_id: str = Field(min_length=1, max_length=200)
    expected_document_revision: int = Field(ge=1)
    version_id: UUID | None = None


class RetryControl(StrictModel):
    request_id: str = Field(min_length=1, max_length=200)


class RetrievalQuery(StrictModel):
    corpus: str = Field(default="knowledge", pattern="^knowledge$")
    company_id: str = Field(pattern=r"^C\d+$")
    query: str = Field(min_length=1, max_length=2000)
    entities: list[str] = Field(default_factory=list, max_length=8)
    document_ids: list[UUID] = Field(default_factory=list, max_length=20)
    as_of: date | None = None
    period: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    session_id: UUID | None = None


def knowledge_router(store, retrieval, auth_dependency):
    router = APIRouter(prefix="/api/v1")

    async def receive_upload(request, current, document_id=None):
        async with request.form(
            max_files=1, max_fields=7, max_part_size=store.settings.knowledge_max_file_bytes
        ) as form:
            file = form.get("file")
            if not isinstance(file, UploadFile):
                raise AppError("DOCUMENT_UNSUPPORTED", "请选择一份知识资料。", 422)
            try:
                args = UploadKnowledge.model_validate(
                    {
                        k: form.get(k)
                        for k in UploadKnowledge.model_fields
                        if form.get(k) not in (None, "")
                    }
                )
            except ValidationError as exc:
                raise AppError(
                    "INVALID_ARGUMENT", "知识标题、企业、日期或版本参数不正确。", 422
                ) from exc
            if document_id and args.expected_document_revision is None:
                raise AppError("INVALID_ARGUMENT", "更新资料必须提供当前文档revision。", 422)
            content = await file.read(store.settings.knowledge_max_file_bytes + 1)
            # Files are read once; inference belongs to the persistent background job.
            from starlette.concurrency import run_in_threadpool

            return await run_in_threadpool(
                store.upload,
                current,
                name=file.filename or "",
                content=content,
                company_id=args.company_id,
                title=args.title,
                request_id=args.request_id,
                document_id=document_id,
                expected_revision=args.expected_document_revision,
                effective_from=args.effective_from,
                effective_to=args.effective_to,
            )

    @router.post("/knowledge/uploads", status_code=202)
    async def upload(request: Request, current=auth_dependency):
        return await receive_upload(request, current)

    @router.post("/knowledge/documents/{document_id}/versions", status_code=202)
    async def version(document_id: UUID, request: Request, current=auth_dependency):
        return await receive_upload(request, current, str(document_id))

    @router.get("/knowledge/documents")
    def documents(company_id: str, offset: int = 0, current=auth_dependency):
        return store.list(current, company_id, offset)

    @router.get("/knowledge/documents/{document_id}")
    def document(document_id: UUID, current=auth_dependency):
        return store.get(current, str(document_id))

    @router.get("/knowledge/documents/{document_id}/file")
    def file(document_id: UUID, version_id: UUID | None = None, current=auth_dependency):
        original = store.file(current, str(document_id), str(version_id) if version_id else None)
        return Response(
            content=bytes(original["file_bytes"]),
            media_type=original["media_type"],
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(original["original_name"]),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/knowledge/documents/{document_id}/publish")
    def publish(document_id: UUID, body: KnowledgeControl, current=auth_dependency):
        return store.change(
            current,
            str(document_id),
            action="publish",
            expected_revision=body.expected_document_revision,
            request_id=body.request_id,
            version_id=str(body.version_id) if body.version_id else None,
        )

    @router.post("/knowledge/documents/{document_id}/disable")
    def disable(document_id: UUID, body: KnowledgeControl, current=auth_dependency):
        return store.change(
            current,
            str(document_id),
            action="disable",
            expected_revision=body.expected_document_revision,
            request_id=body.request_id,
        )

    @router.get("/knowledge/jobs/{job_id}")
    def job(job_id: UUID, current=auth_dependency):
        return store.job(current, str(job_id))

    @router.post("/knowledge/jobs/{job_id}/retry")
    def retry(job_id: UUID, body: RetryControl, current=auth_dependency):
        return store.retry(current, str(job_id), body.request_id)

    @router.get("/knowledge/ready")
    def ready(current=auth_dependency):
        return retrieval.ready()

    @router.post("/retrieval/search")
    def search(body: RetrievalQuery, current=auth_dependency):
        args = body.model_dump(exclude={"corpus"})
        args["document_ids"] = [str(i) for i in body.document_ids]
        args["session_id"] = str(body.session_id) if body.session_id else None
        return retrieval.search(current, **args)

    @router.get("/evidence/{citation_id}")
    def evidence(citation_id: UUID, current=auth_dependency):
        return retrieval.read(current, str(citation_id))

    return router
