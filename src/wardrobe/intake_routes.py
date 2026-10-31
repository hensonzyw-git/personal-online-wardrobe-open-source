"""Typed private HTTP routes; binary upload never passes through a model tool."""

import asyncio
import os
import tempfile
from pathlib import Path

from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse

from .errors import BusinessError
from .intake_contracts import (
    IntakeComplete,
    IntakeCreate,
    IntakeDecisionInput,
    IntakeObserve,
    IntakePatch,
    IntakeResolve,
    IntakeRevision,
    IntakeUploadInput,
)

BINARY = {
    200: {
        "description": "Private authenticated source bytes; source file is never replaced by view JPEG",
        "content": {
            mime: {"schema": {"type": "string", "format": "binary"}}
            for mime in ("image/jpeg", "image/png", "image/heic", "image/heif")
        },
    }
}
JPEG = {
    200: {
        "description": "Private bounded derived JPEG",
        "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}},
    }
}


def register_intake_routes(app, service):
    def p(request):
        return request.scope["wardrobe.principal"]

    def result(value):
        return JSONResponse(value, headers={"Cache-Control": "private, no-store"})

    @app.post("/v1/intakes")
    def create(request: Request, payload: IntakeCreate):
        return result(
            service.create(
                p(request), payload, request.headers.get("Idempotency-Key", "")
            )
        )

    @app.get("/v1/intakes/by-origin/{origin_scope}")
    def by_origin(request: Request, origin_scope: str):
        return result(service.by_origin(p(request), origin_scope))

    @app.get("/v1/intakes/{task_id}")
    def get(request: Request, task_id: str):
        return result(service.get(p(request), task_id))

    @app.patch("/v1/intakes/{task_id}")
    def patch(request: Request, task_id: str, payload: IntakePatch):
        return result(service.patch(p(request), task_id, payload))

    @app.post("/v1/intakes/{task_id}/uploads")
    def prepare(request: Request, task_id: str, payload: IntakeUploadInput):
        return result(
            service.prepare(
                p(request), task_id, payload, request.headers.get("Idempotency-Key", "")
            )
        )

    @app.get("/v1/intakes/{task_id}/uploads/by-key/{key}")
    def upload_by_key(request: Request, task_id: str, key: str):
        return result(service.upload_by_key(p(request), task_id, key))

    @app.get("/v1/intakes/{task_id}/uploads/{upload_id}")
    def status(request: Request, task_id: str, upload_id: str):
        return result(service.upload_status(p(request), task_id, upload_id))

    @app.put("/v1/intakes/{task_id}/uploads/{upload_id}/content")
    async def upload(request: Request, task_id: str, upload_id: str):
        token = request.headers.get("X-Wardrobe-Upload-Token", "")
        principal = p(request)
        maximum = await run_in_threadpool(
            service.check_upload, principal, task_id, upload_id, token
        )
        fd, name = tempfile.mkstemp(dir=service.temporary)
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as file:
                count = 0
                async with asyncio.timeout(120):
                    async for chunk in request.stream():
                        count += len(chunk)
                        if count > maximum:
                            raise BusinessError(
                                "UPLOAD_TOO_LARGE", "Upload exceeds declared bytes", 413
                            )
                        file.write(chunk)
                file.flush()
                os.fsync(file.fileno())
            return result(
                await run_in_threadpool(
                    service.accept_file, principal, task_id, upload_id, token, path
                )
            )
        except TimeoutError:
            raise BusinessError("UPLOAD_INCOMPLETE", "Upload timed out", 408) from None
        finally:
            path.unlink(missing_ok=True)

    @app.post("/v1/intakes/{task_id}/complete")
    def complete(request: Request, task_id: str, payload: IntakeComplete):
        return result(service.complete(p(request), task_id, payload))

    @app.post("/v1/intakes/{task_id}/observations")
    def observe(request: Request, task_id: str, payload: IntakeObserve):
        return result(service.observe(p(request), task_id, payload))

    @app.post("/v1/intakes/{task_id}/freeze")
    def freeze(request: Request, task_id: str, payload: IntakeRevision):
        return result(service.freeze(p(request), task_id, payload))

    @app.post("/v1/intakes/{task_id}/decisions")
    def decide(request: Request, task_id: str, payload: IntakeDecisionInput):
        return result(service.decide(p(request), task_id, payload.assertion))

    @app.post("/v1/intakes/{task_id}/close")
    def close(request: Request, task_id: str):
        return result(service.close(p(request), task_id))

    @app.get("/v1/intakes/{task_id}/attempts/{jti}")
    def attempt(request: Request, task_id: str, jti: str):
        return result(service.attempt(p(request), task_id, jti))

    @app.post("/v1/intakes/{task_id}/attempts/{jti}/resolve")
    def resolve(request: Request, task_id: str, jti: str, payload: IntakeResolve):
        return result(service.attempt(p(request), task_id, jti, payload))

    @app.post("/v1/intakes/{task_id}/images/{image_id}/remove")
    def remove_image(
        request: Request, task_id: str, image_id: str, payload: IntakeRevision
    ):
        return result(service.remove_image(p(request), task_id, image_id, payload))

    @app.get(
        "/v1/intakes/{task_id}/images/{image_id}/preview",
        response_class=FileResponse,
        responses=JPEG,
    )
    def preview(request: Request, task_id: str, image_id: str):
        path, mime = service.preview(p(request), task_id, image_id)
        return FileResponse(
            path, media_type=mime, headers={"Cache-Control": "private, no-store"}
        )

    @app.get(
        "/v1/intakes/{task_id}/images/{image_id}/source",
        response_class=FileResponse,
        responses=BINARY,
    )
    def source(request: Request, task_id: str, image_id: str):
        path, mime = service.draft_source(p(request), task_id, image_id)
        return FileResponse(
            path, media_type=mime, headers={"Cache-Control": "private, no-store"}
        )

    @app.get(
        "/v1/intakes/{task_id}/images/{image_id}/analysis",
        response_class=FileResponse,
        responses=JPEG,
    )
    def analysis(request: Request, task_id: str, image_id: str):
        path, mime = service.draft_source(p(request), task_id, image_id, "analysis")
        return FileResponse(
            path, media_type=mime, headers={"Cache-Control": "private, no-store"}
        )

    @app.get("/v1/intake-receipts/{receipt_id}")
    def receipt(request: Request, receipt_id: str):
        return result(service.receipt(p(request), receipt_id))

    @app.get(
        "/v1/garments/{gid}/sources/{source_id}/content",
        response_class=FileResponse,
        responses=BINARY,
    )
    def published_source(request: Request, gid: str, source_id: str):
        path, mime = service.source_content(p(request), gid, source_id)
        return FileResponse(
            path, media_type=mime, headers={"Cache-Control": "private, no-store"}
        )
