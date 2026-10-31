"""Fixed read/write business endpoints. No generic remote-command interface."""

from typing import Annotated

from fastapi import Body, Request
from pydantic import ValidationError
from starlette.responses import FileResponse, JSONResponse

from .agent_contracts import AgentOperationCancel, AgentRecommendation
from .agent_service import envelope
from .errors import BusinessError
from .intake_routes import BINARY, JPEG
from .recommendations import FeedbackInput
from .seasons import HistorySearch, SearchInput, SeasonInput


def register_agent_routes(app, service):
    def actor(request):
        if request.headers.get("X-Wardrobe-Agent-Protocol") != "1":
            raise BusinessError(
                "AGENT_PROTOCOL_UNSUPPORTED", "Unsupported protocol", 426
            )
        return request.scope["wardrobe.principal"]

    def key(request):
        value = request.headers.get("Idempotency-Key", "")
        if not 1 <= len(value) <= 128:
            raise BusinessError(
                "IDEMPOTENCY_KEY_REQUIRED", "Idempotency key required", 422
            )
        return value

    def query(request, model):
        values = dict(request.query_params)
        if len(values) != len(request.query_params.multi_items()):
            raise BusinessError("INVALID_ARGUMENT", "Duplicate parameters", 422)
        try:
            if "limit" in values:
                values["limit"] = int(values["limit"])
            return model.model_validate(values)
        except (ValueError, ValidationError):
            raise BusinessError("INVALID_ARGUMENT", "Invalid query", 422) from None

    def result(value):
        return JSONResponse(value, headers={"Cache-Control": "private, no-store"})

    @app.get("/v1/agent/capabilities")
    def capabilities(request: Request):
        return result(service.capabilities(request.scope["wardrobe.principal"]))

    @app.get("/v1/agent/garments")
    def garments(request: Request):
        return result(
            envelope(
                service.seasons.search(actor(request), query(request, SearchInput))[
                    "data"
                ]
            )
        )

    @app.get("/v1/agent/garments/{identifier}")
    def garment(request: Request, identifier: str):
        return result(
            envelope(service.catalog.get_garment(actor(request), identifier)["data"])
        )

    @app.get("/v1/agent/history")
    def history(request: Request):
        return result(
            envelope(
                service.seasons.history(actor(request), query(request, HistorySearch))[
                    "data"
                ]
            )
        )

    @app.get("/v1/agent/season")
    def season(request: Request):
        return result(envelope(service.seasons.get(actor(request))["data"]))

    @app.get("/v1/agent/receipts")
    def receipt(request: Request, kind: str, key: str):
        return result(service.command_receipt(actor(request), kind, key))

    @app.put("/v1/agent/season")
    def set_season(request: Request, payload: SeasonInput):
        principal = actor(request)
        with service.db.sessions.begin() as s:
            service.auth(s, principal, "wardrobe.season", write=True)
        return result(
            envelope(service.seasons.set(principal, payload, key(request))["data"])
        )

    @app.post("/v1/agent/feedback")
    def feedback(request: Request, payload: FeedbackInput):
        principal = actor(request)
        with service.db.sessions.begin() as s:
            service.auth(s, principal, "wardrobe.feedback", write=True)
        return result(
            envelope(
                service.recommendations.feedback(principal, payload, key(request))[
                    "data"
                ]
            )
        )

    @app.post("/v1/agent/recommendations", status_code=202)
    def recommend(request: Request, payload: AgentRecommendation):
        return JSONResponse(
            service.recommend(actor(request), payload, key(request)),
            status_code=202,
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/v1/agent/operations/by-key")
    def by_key(request: Request, kind: str, key: str):
        return result(service.by_key(actor(request), kind, key))

    @app.get("/v1/agent/operations/{ref}")
    def operation(request: Request, ref: str):
        return result(service.get(actor(request), ref))

    @app.post("/v1/agent/operations/{ref}/cancel")
    def cancel_operation(
        request: Request,
        ref: str,
        payload: Annotated[
            AgentOperationCancel, Body(default_factory=AgentOperationCancel)
        ],
    ):
        return result(service.cancel_operation(actor(request), ref, key(request)))

    @app.post("/v1/agent/operations/{ref}/resume")
    def resume(request: Request, ref: str):
        return result(service.resume(actor(request), ref, key(request)))

    @app.get("/v1/agent/images/{asset}", response_class=FileResponse, responses=JPEG)
    def catalog_image(request: Request, asset: str):
        path, mime = service.assets.preview(actor(request), asset)
        return FileResponse(
            path, media_type=mime, headers={"Cache-Control": "private, no-store"}
        )

    @app.get(
        "/v1/agent/images/{asset}/original",
        response_class=FileResponse,
        responses=BINARY,
    )
    def catalog_original(request: Request, asset: str):
        path, mime = service.assets.content(actor(request), asset)
        return FileResponse(
            path, media_type=mime, headers={"Cache-Control": "private, no-store"}
        )
