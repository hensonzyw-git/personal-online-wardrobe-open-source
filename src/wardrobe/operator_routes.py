"""HTTP-only operator surface. Every request has the dedicated identity boundary."""

from fastapi import Request
from pydantic import ValidationError
from starlette.responses import FileResponse, JSONResponse

from .errors import BusinessError
from .operator_contracts import (
    OperatorCategoryQuery,
    OperatorDetailQuery,
    OperatorListQuery,
    OperatorRevision,
    OperatorUpdate,
)
from .operator_service import validate_key


def query_args(request, model):
    args = dict(request.query_params)
    if any(len(request.query_params.getlist(key)) != 1 for key in args):
        raise BusinessError("INVALID_ARGUMENT", "Repeated query parameters", 422)
    if "cursor" in args and not 1 <= len(args["cursor"]) <= 4096:
        raise BusinessError("INVALID_CURSOR", "Cursor is invalid for this query", 422)
    try:
        if "limit" in args:
            if not args["limit"].isascii() or not args["limit"].isdigit():
                raise ValueError()
            args["limit"] = int(args["limit"])
        return model.model_validate(args)
    except (ValueError, ValidationError):
        raise BusinessError(
            "INVALID_ARGUMENT", "Invalid query parameters", 422
        ) from None


def idempotency_key(request):
    if request.query_params:
        raise BusinessError("INVALID_ARGUMENT", "Write queries are not supported", 422)
    keys = request.headers.getlist("Idempotency-Key")
    if len(keys) > 1:
        raise BusinessError("INVALID_ARGUMENT", "Repeated idempotency header", 422)
    key = keys[0] if keys else ""
    validate_key(key)
    return key


def register_operator_routes(app, service):
    prefix = "/v1/operator"

    @app.get(prefix + "/garments")
    def garments(request: Request):
        return service.list_garments(
            request.scope["wardrobe.principal"], query_args(request, OperatorListQuery)
        )

    @app.get(prefix + "/categories")
    def categories(request: Request):
        return service.categories(
            request.scope["wardrobe.principal"],
            query_args(request, OperatorCategoryQuery),
        )

    @app.get(prefix + "/garments/{garment_id}")
    def garment(request: Request, garment_id: str):
        query = query_args(request, OperatorDetailQuery)
        return service.get_garment(
            request.scope["wardrobe.principal"], garment_id, query.state
        )

    @app.patch(prefix + "/garments/{garment_id}")
    def update(request: Request, garment_id: str, payload: OperatorUpdate):
        return service.update(
            request.scope["wardrobe.principal"],
            garment_id,
            payload,
            idempotency_key(request),
        )

    @app.post(prefix + "/garments/{garment_id}/delete")
    def delete(request: Request, garment_id: str, payload: OperatorRevision):
        return service.delete(
            request.scope["wardrobe.principal"],
            garment_id,
            payload,
            idempotency_key(request),
        )

    @app.post(prefix + "/garments/{garment_id}/restore")
    def restore(request: Request, garment_id: str, payload: OperatorRevision):
        return service.restore(
            request.scope["wardrobe.principal"],
            garment_id,
            payload,
            idempotency_key(request),
        )

    def image(request, garment_id, asset_id, variant):
        if request.query_params:
            raise BusinessError(
                "INVALID_ARGUMENT", "Image queries are not supported", 422
            )
        if "range" in request.headers:
            return JSONResponse(
                BusinessError(
                    "RANGE_NOT_SUPPORTED", "Image ranges are not supported", 416
                ).response(),
                status_code=416,
            )
        try:
            path, mime = service.image(
                request.scope["wardrobe.principal"], garment_id, asset_id, variant
            )
        except BusinessError as error:
            if error.code == "PREVIEW_BUSY":
                return JSONResponse(
                    error.response(), status_code=503, headers={"Retry-After": "2"}
                )
            raise
        return FileResponse(
            path,
            media_type=mime,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get(prefix + "/garments/{garment_id}/assets/{asset_id}/content")
    def content(request: Request, garment_id: str, asset_id: str):
        return image(request, garment_id, asset_id, "content")

    @app.get(prefix + "/garments/{garment_id}/assets/{asset_id}/preview")
    def preview(request: Request, garment_id: str, asset_id: str):
        return image(request, garment_id, asset_id, "preview")
