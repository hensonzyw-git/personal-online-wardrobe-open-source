import asyncio
import json
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field, ValidationError, create_model
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse

from .annotations import AnnotationService, SuggestionInput
from .assets import AssetService, GarmentInput, UploadInput, create_garment
from .auth import Authenticator, PrivateServiceMiddleware
from .catalog import CatalogService
from .catalog_import import get_product_evidence
from .changes import ChangeService, ProposalInput
from .config import Settings
from .contracts import ListQuery, Principal, StrictModel
from .db import Database, load_cursor_key, load_private_key
from .errors import BusinessError
from .intake_contracts import IntakeCreate, IntakePatch
from .products import ProductDraftInput, ProductLookup, ProductService
from .recommendations import FeedbackInput, RecommendationInput, RecommendationService
from .seasons import HistorySearch, SearchInput, SeasonInput, SeasonService

IMAGE_CONTENT_RESPONSES = {
    200: {
        "description": "Authenticated image bytes, not JSON. Attach the fetched image using the client's supported image/file output; embedding the protected URL alone does not supply Bearer authentication.",
        "content": {
            mime: {"schema": {"type": "string", "format": "binary"}}
            for mime in ("image/png", "image/jpeg", "image/heic", "image/heif")
        },
        "headers": {
            "Cache-Control": {
                "schema": {"type": "string"},
                "description": "private, no-store",
            }
        },
    }
}


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    weather=None,
    evaluator=None,
    product_provider=None,
):
    settings = settings or Settings.from_env()
    database = database or Database(settings.data_root / "wardrobe.sqlite")
    catalog = CatalogService(
        database.sessions,
        load_cursor_key(settings.data_root),
        settings.data_root / "assets" / "original",
    )
    authenticator = Authenticator(database.sessions, settings.audience)
    if weather is None and settings.weather_provider == "open-meteo":
        from .open_meteo import OpenMeteo

        weather = OpenMeteo()
    if evaluator is None and settings.model_provider == "deepseek":
        from .outfit_provider import DeepSeekEvaluator, ModelCallQuota

        if (
            settings.model_credentials_file is None
            or settings.model_call_limit is None
            or settings.model_ledger_expected_used is None
        ):
            raise ValueError(
                "DeepSeek requires a private credential file, explicit call quota and expected ledger usage"
            )
        evaluator = DeepSeekEvaluator(
            settings.model_credentials_file,
            ModelCallQuota(
                settings.data_root / "model-calls.sqlite",
                settings.model_call_limit,
                expected_used=settings.model_ledger_expected_used,
            ),
        )
    assets = AssetService(
        database, settings.data_root, load_private_key(settings.data_root, "upload.key")
    )
    changes = ChangeService(database, assets, settings.audience)
    recommendations = RecommendationService(
        database,
        catalog,
        assets,
        settings.audience,
        weather,
        evaluator,
        default_timezone=settings.timezone,
    )
    seasons = SeasonService(database, catalog)
    annotations = AnnotationService(database, catalog)
    products = ProductService(
        database,
        assets,
        changes,
        product_provider,
    )
    from .intake_routes import register_intake_routes
    from .intakes import IntakeService

    intakes = IntakeService(
        database,
        settings.data_root,
        load_private_key(settings.data_root, "upload.key"),
        enabled=settings.intake_enabled,
    )
    from .agent_service import AgentService

    agents = AgentService(database, settings, recommendations, seasons, catalog, assets)
    mcp = MCPServer("Open Wardrobe")

    class GetArgs(StrictModel):
        garment_id: str = Field(max_length=36)

    class ChangeArgs(StrictModel):
        proposal_id: str = Field(max_length=36)

    class RecommendationArgs(StrictModel):
        recommendation_id: str = Field(max_length=36)

    class ProductDraftArgs(StrictModel):
        draft_id: str = Field(min_length=36, max_length=36)

    class ProductPublicationArgs(ProductDraftArgs):
        # Key bounds (1-128) are enforced by the service layer
        # (writes.idempotent) so MCP and HTTP converge on
        # IDEMPOTENCY_KEY_REQUIRED instead of MCP short-circuiting
        # with INVALID_ARGUMENT at the schema boundary.
        key: str

    class PageArgs(StrictModel):
        limit: int = Field(default=50, ge=1, le=100)
        cursor: str | None = Field(default=None, max_length=4096)

    def write_args(model):
        return create_model(
            model.__name__ + "Tool",
            __base__=StrictModel,
            payload=(model, ...),
            # Key bounds (1-128) are enforced by the service layer
            # (writes.idempotent) so MCP and HTTP converge on
            # IDEMPOTENCY_KEY_REQUIRED instead of MCP short-circuiting
            # with INVALID_ARGUMENT at the schema boundary.
            key=(str, ...),
        )

    class EmptyArgs(StrictModel):
        pass

    class FeedbackArgs(StrictModel):
        event_id: str = Field(max_length=36)

    schemas = {
        "wardrobe.get_season": EmptyArgs,
        "wardrobe.set_season": write_args(SeasonInput),
        "wardrobe.search_garments": SearchInput,
        "wardrobe.search_history": HistorySearch,
        "wardrobe.get_feedback": FeedbackArgs,
        "wardrobe.list_garments": ListQuery,
        "wardrobe.get_garment": GetArgs,
        "wardrobe.get_product_evidence": GetArgs,
        "wardrobe.get_change": ChangeArgs,
        "wardrobe.prepare_asset_upload": write_args(UploadInput),
        "wardrobe.add_garment": write_args(GarmentInput),
        "wardrobe.propose_change": write_args(ProposalInput),
        "wardrobe.recommend_outfits": write_args(RecommendationInput),
        "wardrobe.get_recommendation": RecommendationArgs,
        "wardrobe.add_feedback": write_args(FeedbackInput),
        "wardrobe.list_history": PageArgs,
        "wardrobe.list_annotation_review": PageArgs,
        "wardrobe.suggest_annotations": write_args(SuggestionInput),
        "wardrobe.lookup_product": ProductLookup,
        "wardrobe.prepare_product_draft": write_args(ProductDraftInput),
        "wardrobe.get_product_draft": ProductDraftArgs,
        "wardrobe.propose_product_publication": ProductPublicationArgs,
    }

    def mcp_result(value, *, error=False):
        return CallToolResult(
            content=[
                TextContent(type="text", text=json.dumps(value, ensure_ascii=False))
            ],
            structuredContent=value,
            isError=error,
        )

    async def strict_tools(ctx, call_next):
        if (
            ctx.method == "tools/call"
            and ctx.params
            and ctx.params.get("name") in schemas
        ):
            try:
                schemas[ctx.params["name"]].model_validate(
                    ctx.params.get("arguments", {})
                )
            except ValidationError:
                return mcp_result(
                    BusinessError(
                        "INVALID_ARGUMENT",
                        "Tool arguments do not match the strict contract",
                        422,
                    ).response(),
                    error=True,
                )
        return await call_next(ctx)

    mcp.middleware.append(strict_tools)

    def http_principal(ctx: Context) -> Principal:
        request = ctx.request_context.request
        principal = (
            request.scope.get("wardrobe.principal") if request is not None else None
        )
        if not isinstance(principal, Principal):
            raise BusinessError(
                "UNAUTHORIZED", "Authenticated HTTP context is required", 401
            )
        return principal

    async def tool_response(callback, *args) -> CallToolResult:
        try:
            value = await run_in_threadpool(callback, *args)
            return mcp_result(
                value, error=value.get("status") in {"failed", "cancelled"}
            )
        except BusinessError as error:
            value = error.response()
            return mcp_result(value, error=True)
        except SQLAlchemyError:
            return mcp_result(
                BusinessError(
                    "DATABASE_UNAVAILABLE",
                    "Database operation failed; query the original receipt before retrying a write",
                    503,
                ).response(),
                error=True,
            )
        except Exception as error:  # noqa: BLE001 -- redact failures before the SDK handles them
            value = BusinessError(
                "INTERNAL_ERROR",
                "Internal operation failed; query the original receipt before retrying a write",
                500,
            ).response()
            logging.getLogger(__name__).error(
                "Unexpected MCP tool failure: type=%s request_id=%s",
                type(error).__name__,
                value["request_id"],
            )
            return mcp_result(value, error=True)

    @mcp.tool(
        name="wardrobe.lookup_product",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def lookup_product(
        ctx: Context, article_number: str, page: int = 1, limit: int = 20
    ) -> CallToolResult:
        """Query one configured product provider candidate page. Detail must exactly match the screenshot article number."""
        return await tool_response(
            products.lookup,
            http_principal(ctx),
            ProductLookup(article_number=article_number, page=page, limit=limit),
        )

    @mcp.tool(
        name="wardrobe.prepare_product_draft",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=True
        ),
    )
    async def prepare_product_draft(
        ctx: Context, payload: ProductDraftInput, key: str
    ) -> CallToolResult:
        """Prepare unpublished product evidence. Caller extracts two screenshots; send owned size, sales color and wardrobe assets. No publication."""
        return await tool_response(products.prepare, http_principal(ctx), payload, key)

    @mcp.tool(
        name="wardrobe.get_product_draft",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=False
        ),
    )
    async def get_product_draft(ctx: Context, draft_id: str) -> CallToolResult:
        """Read one owner's product draft, provenance and unresolved conflicts."""
        return await tool_response(products.get, http_principal(ctx), draft_id)

    @mcp.tool(
        name="wardrobe.propose_product_publication",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=False
        ),
    )
    async def propose_product_publication(
        ctx: Context, draft_id: str, key: str
    ) -> CallToolResult:
        """Propose product publication. Only a trusted human adapter can sign and execute approval."""
        return await tool_response(products.propose, http_principal(ctx), draft_id, key)

    @mcp.tool(name="wardrobe.get_season")
    async def get_season(ctx: Context):
        return await tool_response(seasons.get, http_principal(ctx))

    @mcp.tool(name="wardrobe.set_season")
    async def set_season(ctx: Context, payload: SeasonInput, key: str):
        return await tool_response(seasons.set, http_principal(ctx), payload, key)

    @mcp.tool(name="wardrobe.search_garments")
    async def search_garments(
        ctx: Context,
        view: str = "current",
        category: str | None = None,
        category_group: str | None = None,
        q: str | None = None,
        color: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ):
        return await tool_response(
            seasons.search,
            http_principal(ctx),
            SearchInput(
                view=view,
                category=category,
                category_group=category_group,
                q=q,
                color=color,
                limit=limit,
                cursor=cursor,
            ),
        )

    @mcp.tool(name="wardrobe.search_history")
    async def search_history(
        ctx: Context,
        kind: str | None = None,
        recommendation_id: str | None = None,
        outfit_id: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ):
        return await tool_response(
            seasons.history,
            http_principal(ctx),
            HistorySearch(
                kind=kind,
                recommendation_id=recommendation_id,
                outfit_id=outfit_id,
                limit=limit,
                cursor=cursor,
            ),
        )

    @mcp.tool(name="wardrobe.get_feedback")
    async def get_feedback(ctx: Context, event_id: str):
        return await tool_response(seasons.feedback, http_principal(ctx), event_id)

    @mcp.tool(
        name="wardrobe.list_garments",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=False
        ),
    )
    async def list_garments(
        ctx: Context,
        limit: int = 50,
        cursor: str | None = None,
        category: str | None = None,
    ) -> CallToolResult:
        """List one authenticated owner's active garments; follow next_cursor for all pages."""
        try:
            query = ListQuery(limit=limit, cursor=cursor, category=category)
            principal = http_principal(ctx)
        except ValidationError:
            return mcp_result(
                BusinessError(
                    "INVALID_ARGUMENT", "Invalid list arguments", 422
                ).response(),
                error=True,
            )
        return await tool_response(catalog.list_garments, principal, query)

    @mcp.tool(
        name="wardrobe.get_garment",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=False
        ),
    )
    async def get_garment(ctx: Context, garment_id: str) -> CallToolResult:
        """Get an active garment by UUID within the authenticated owner's catalog."""
        return await tool_response(catalog.get_garment, http_principal(ctx), garment_id)

    @mcp.tool(
        name="wardrobe.get_product_evidence",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=False
        ),
    )
    async def product_evidence(ctx: Context, garment_id: str) -> CallToolResult:
        """Read complete catalog snapshots and match status. Catalog sizes are not owned sizes."""
        return await tool_response(
            get_product_evidence, database.sessions, http_principal(ctx), garment_id
        )

    @mcp.tool(
        name="wardrobe.prepare_asset_upload",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=False
        ),
    )
    async def prepare_asset_upload(
        ctx: Context, payload: UploadInput, key: str
    ) -> CallToolResult:
        """Prepare a bounded image upload; transfer binary data over the returned API path."""
        return await tool_response(assets.prepare, http_principal(ctx), payload, key)

    @mcp.tool(
        name="wardrobe.add_garment",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=False
        ),
    )
    async def add_garment(
        ctx: Context, payload: GarmentInput, key: str
    ) -> CallToolResult:
        """Create a new garment with verified assets; never overwrite or merge existing garments."""
        return await tool_response(
            create_garment, database, http_principal(ctx), payload, key
        )

    @mcp.tool(
        name="wardrobe.propose_change",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=False
        ),
    )
    async def propose_change(
        ctx: Context, payload: ProposalInput, key: str
    ) -> CallToolResult:
        """Freeze specific changes for human review. This tool never approves or executes them."""
        return await tool_response(changes.propose, http_principal(ctx), payload, key)

    @mcp.tool(
        name="wardrobe.get_change",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=False
        ),
    )
    async def get_change(ctx: Context, proposal_id: str) -> CallToolResult:
        """Read a proposal and its authoritative execution receipt, expiry or conflict."""
        return await tool_response(changes.get, http_principal(ctx), proposal_id)

    @mcp.tool(
        name="wardrobe.recommend_outfits",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=True
        ),
    )
    async def recommend_outfits(
        ctx: Context, payload: RecommendationInput, key: str
    ) -> CallToolResult:
        """Submit one durable weather/outfit job. Poll its original ID; never interpret accepted as an outfit."""
        return await tool_response(
            recommendations.submit, http_principal(ctx), payload, key
        )

    @mcp.tool(
        name="wardrobe.get_recommendation",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=False
        ),
    )
    async def get_recommendation(
        ctx: Context, recommendation_id: str
    ) -> CallToolResult:
        """Read running/terminal state of the original job without creating another recommendation."""
        return await tool_response(
            recommendations.get, http_principal(ctx), recommendation_id
        )

    @mcp.tool(
        name="wardrobe.add_feedback",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=False
        ),
    )
    async def add_feedback(
        ctx: Context, payload: FeedbackInput, key: str
    ) -> CallToolResult:
        """Append explicit shown/adopted/worn/rejected feedback. Corrections require a human proposal."""
        return await tool_response(
            recommendations.feedback, http_principal(ctx), payload, key
        )

    @mcp.tool(
        name="wardrobe.list_history",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=False
        ),
    )
    async def list_history(
        ctx: Context, limit: int = 50, cursor: str | None = None
    ) -> CallToolResult:
        """Page generated and caller-reported events without inventing wear history."""
        return await tool_response(
            recommendations.history,
            http_principal(ctx),
            PageArgs(limit=limit, cursor=cursor),
        )

    @mcp.tool(
        name="wardrobe.list_annotation_review",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=False
        ),
    )
    async def list_annotation_review(
        ctx: Context, limit: int = 50, cursor: str | None = None
    ) -> CallToolResult:
        """Read pending suggestion versions and conflicts; publication requires a human proposal."""
        return await tool_response(
            annotations.list_review,
            http_principal(ctx),
            PageArgs(limit=limit, cursor=cursor),
        )

    @mcp.tool(
        name="wardrobe.suggest_annotations",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=False
        ),
    )
    async def suggest_annotations(
        ctx: Context, payload: SuggestionInput, key: str
    ) -> CallToolResult:
        """Save caller-supplied draft labels. They never become effective until human publication."""
        return await tool_response(
            annotations.suggest, http_principal(ctx), payload, key
        )

    allowed_hosts = [
        f"[{h}]:*" if ":" in h else f"{h}:*" for h in settings.allowed_hosts
    ]
    allowed_hosts += [f"[{h}]" if ":" in h else h for h in settings.allowed_hosts]

    @mcp.tool(name="wardrobe.prepare_garment_intake")
    async def prepare_garment_intake(
        ctx: Context, payload: IntakeCreate, key: str
    ) -> CallToolResult:
        """Prepare a draft only. Creating a garment requires a trusted human decision."""
        return await tool_response(intakes.create, http_principal(ctx), payload, key)

    @mcp.tool(name="wardrobe.get_garment_intake")
    async def get_garment_intake(ctx: Context, task_id: str) -> CallToolResult:
        return await tool_response(intakes.get, http_principal(ctx), task_id)

    @mcp.tool(name="wardrobe.update_garment_intake")
    async def update_garment_intake(
        ctx: Context, task_id: str, payload: IntakePatch
    ) -> CallToolResult:
        """Edit draft values only, never evidence, verification, upload bindings or receipt."""
        return await tool_response(intakes.patch, http_principal(ctx), task_id, payload)

    @mcp.tool(name="wardrobe.close_garment_intake")
    async def close_garment_intake(ctx: Context, task_id: str) -> CallToolResult:
        return await tool_response(intakes.close, http_principal(ctx), task_id)

    mcp_app = mcp.streamable_http_app(
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=list(settings.allowed_origins),
        ),
    )

    @asynccontextmanager
    async def lifespan(_app):
        database.assert_initialized()
        async with mcp.session_manager.run():
            worker = asyncio.create_task(recommendations.run())
            agent_worker = asyncio.create_task(agents.run())
            try:
                yield
            finally:
                agent_worker.cancel()
                try:
                    await agent_worker
                except asyncio.CancelledError:
                    pass
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
        database.close()

    app = FastAPI(
        title="Open Wardrobe",
        version=version("open-wardrobe"),
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.database, app.state.catalog, app.state.mcp = database, catalog, mcp
    app.state.assets, app.state.changes = assets, changes
    app.state.recommendations = recommendations
    app.state.products = products
    app.state.agents = agents
    app.add_middleware(
        PrivateServiceMiddleware, authenticator=authenticator, settings=settings
    )

    @app.exception_handler(BusinessError)
    async def business_error(_request, error):
        from .agent_service import error_response, operation_from_path

        return JSONResponse(
            error_response(error, operation_from_path(_request.url.path))
            if _request.url.path.startswith("/v1/agent/")
            else error.response(),
            status_code=error.http_status,
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _error):
        error = BusinessError(
            "INVALID_ARGUMENT", "Request arguments do not match the contract", 422
        )
        from .agent_service import error_response, operation_from_path

        return JSONResponse(
            error_response(error, operation_from_path(_request.url.path))
            if _request.url.path.startswith("/v1/agent/")
            else error.response(),
            status_code=422,
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_request, _error):
        error = BusinessError(
            "DATABASE_UNAVAILABLE",
            "Database operation failed; query the original receipt before retrying a write",
            503,
        )
        from .agent_service import error_response, operation_from_path

        return JSONResponse(
            error_response(error, operation_from_path(_request.url.path))
            if _request.url.path.startswith("/v1/agent/")
            else error.response(),
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    register_intake_routes(app, intakes)
    from .agent_routes import register_agent_routes

    register_agent_routes(app, agents)

    @app.get("/v1/garments")
    def api_list(request: Request):
        if any(
            key not in {"limit", "cursor", "category"} for key in request.query_params
        ):
            raise BusinessError("INVALID_ARGUMENT", "Unknown query parameter", 422)
        if any(
            len(request.query_params.getlist(key)) != 1 for key in request.query_params
        ):
            raise BusinessError("INVALID_ARGUMENT", "Duplicate query parameter", 422)
        try:
            args = dict(request.query_params)
            if "limit" in args:
                args["limit"] = int(args["limit"])
            query = ListQuery.model_validate(args)
        except (ValidationError, ValueError):
            raise BusinessError(
                "INVALID_ARGUMENT", "Invalid list arguments", 422
            ) from None
        return JSONResponse(
            catalog.list_garments(request.scope["wardrobe.principal"], query),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/garments/{garment_id}")
    def api_get(request: Request, garment_id: str):
        return JSONResponse(
            catalog.get_garment(request.scope["wardrobe.principal"], garment_id),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/garments/{garment_id}/product-evidence")
    def api_product_evidence(request: Request, garment_id: str):
        return JSONResponse(
            get_product_evidence(
                database.sessions, request.scope["wardrobe.principal"], garment_id
            ),
            headers={"Cache-Control": "no-store"},
        )

    def idempotency_key(request):
        return request.headers.get("Idempotency-Key", "")

    @app.post("/v1/products/lookup")
    def api_product_lookup(request: Request, payload: ProductLookup):
        return JSONResponse(
            products.lookup(request.scope["wardrobe.principal"], payload),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/product-drafts")
    def api_product_prepare(request: Request, payload: ProductDraftInput):
        return JSONResponse(
            products.prepare(
                request.scope["wardrobe.principal"], payload, idempotency_key(request)
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/product-drafts/{draft_id}")
    def api_product_get(request: Request, draft_id: str):
        return JSONResponse(
            products.get(request.scope["wardrobe.principal"], draft_id),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/product-drafts/{draft_id}/proposal")
    def api_product_propose(request: Request, draft_id: str):
        return JSONResponse(
            products.propose(
                request.scope["wardrobe.principal"], draft_id, idempotency_key(request)
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/v1/product-drafts/{draft_id}/assets/{asset_id}/content",
        response_class=FileResponse,
        responses=IMAGE_CONTENT_RESPONSES,
    )
    def api_product_image(request: Request, draft_id: str, asset_id: str):
        path, mime = products.content(
            request.scope["wardrobe.principal"], draft_id, asset_id
        )
        return FileResponse(
            path, media_type=mime, headers={"Cache-Control": "private, no-store"}
        )

    @app.post("/v1/asset-uploads")
    def api_prepare(request: Request, payload: UploadInput):
        return JSONResponse(
            assets.prepare(
                request.scope["wardrobe.principal"], payload, idempotency_key(request)
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.put("/v1/asset-uploads/{upload_id}/content")
    async def api_upload(request: Request, upload_id: str):
        principal = request.scope["wardrobe.principal"]
        token = request.headers.get("X-Wardrobe-Upload-Token", "")
        maximum = await run_in_threadpool(
            assets.check_upload, principal, upload_id, token
        )
        descriptor, filename = tempfile.mkstemp(dir=assets.temporary)
        path = Path(filename)
        try:
            with os.fdopen(descriptor, "wb") as file:
                size = 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > maximum:
                        raise BusinessError(
                            "UPLOAD_TOO_LARGE", "Upload exceeds its declared size", 413
                        )
                    file.write(chunk)
                file.flush()
                os.fsync(file.fileno())
            result = await run_in_threadpool(
                assets.accept_file, principal, upload_id, token, path
            )
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        finally:
            path.unlink(missing_ok=True)

    @app.get("/v1/asset-uploads/{upload_id}")
    def api_upload_status(request: Request, upload_id: str):
        return JSONResponse(
            assets.status(request.scope["wardrobe.principal"], upload_id),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/garments")
    def api_create(request: Request, payload: GarmentInput):
        return JSONResponse(
            create_garment(
                database,
                request.scope["wardrobe.principal"],
                payload,
                idempotency_key(request),
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/v1/assets/{asset_id}/content",
        response_class=FileResponse,
        responses=IMAGE_CONTENT_RESPONSES,
    )
    def api_asset(request: Request, asset_id: str):
        path, mime = assets.content(request.scope["wardrobe.principal"], asset_id)
        return FileResponse(
            path, media_type=mime, headers={"Cache-Control": "private, no-store"}
        )

    @app.get(
        "/v1/assets/{asset_id}/preview",
        response_class=FileResponse,
        responses={
            200: {
                "description": "Authenticated JPEG thumbnail, at most 150 KiB and 768 pixels per edge; attach the fetched bytes, not the protected URL.",
                "content": {
                    "image/jpeg": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
    )
    def api_asset_preview(request: Request, asset_id: str):
        try:
            path, mime = assets.preview(request.scope["wardrobe.principal"], asset_id)
        except BusinessError as error:
            if error.code == "PREVIEW_BUSY":
                return JSONResponse(
                    error.response(),
                    status_code=503,
                    headers={"Cache-Control": "private, no-store", "Retry-After": "2"},
                )
            raise
        return FileResponse(
            path,
            media_type=mime,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/v1/change-proposals")
    def api_propose(request: Request, payload: ProposalInput):
        return JSONResponse(
            changes.propose(
                request.scope["wardrobe.principal"], payload, idempotency_key(request)
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/change-proposals/{proposal_id}")
    def api_change(request: Request, proposal_id: str):
        return JSONResponse(
            changes.get(request.scope["wardrobe.principal"], proposal_id),
            headers={"Cache-Control": "no-store"},
        )

    class SignedDecision(StrictModel):
        assertion: str = Field(min_length=1, max_length=32768)

    @app.post("/v1/human-decisions")
    def api_decide(request: Request, payload: SignedDecision):
        return JSONResponse(
            changes.decide(request.scope["wardrobe.principal"], payload.assertion),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/change-proposals/{proposal_id}/cancel")
    def api_cancel(request: Request, proposal_id: str):
        return JSONResponse(
            changes.cancel(request.scope["wardrobe.principal"], proposal_id),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/change-proposals/{proposal_id}/preview-grants")
    def api_preview_grant(request: Request, proposal_id: str, payload: SignedDecision):
        _path, _mime, asset_id, expires = changes.preview(
            request.scope["wardrobe.principal"], payload.assertion, proposal_id
        )
        return JSONResponse(
            {
                "schema_version": "wardrobe/1.0",
                "status": "succeeded",
                "data": {
                    "asset_id": asset_id,
                    "expires_at": expires,
                    "content_url": f"/v1/change-proposals/{proposal_id}/preview/{asset_id}",
                    "required_header": "X-Wardrobe-Review-Assertion",
                },
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/change-proposals/{proposal_id}/preview/{asset_id}")
    def api_preview(request: Request, proposal_id: str, asset_id: str):
        path, mime, _asset, _expiry = changes.preview(
            request.scope["wardrobe.principal"],
            request.headers.get("X-Wardrobe-Review-Assertion", ""),
            proposal_id,
            asset_id,
        )
        return FileResponse(
            path, media_type=mime, headers={"Cache-Control": "private, no-store"}
        )

    @app.post("/v1/recommendations")
    def api_recommend(request: Request, payload: RecommendationInput):
        return JSONResponse(
            recommendations.submit(
                request.scope["wardrobe.principal"], payload, idempotency_key(request)
            ),
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/recommendations/{recommendation_id}")
    def api_recommendation(request: Request, recommendation_id: str):
        return JSONResponse(
            recommendations.get(request.scope["wardrobe.principal"], recommendation_id),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/feedback")
    def api_feedback(request: Request, payload: FeedbackInput):
        return JSONResponse(
            recommendations.feedback(
                request.scope["wardrobe.principal"], payload, idempotency_key(request)
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/history")
    def api_history(request: Request):
        try:
            args = dict(request.query_params)
            if any(len(request.query_params.getlist(key)) != 1 for key in args):
                raise ValueError()
            if "limit" in args:
                args["limit"] = int(args["limit"])
            query = PageArgs.model_validate(args)
        except (ValueError, ValidationError):
            raise BusinessError(
                "INVALID_ARGUMENT", "Invalid history pagination arguments", 422
            ) from None
        return JSONResponse(
            recommendations.history(request.scope["wardrobe.principal"], query),
            headers={"Cache-Control": "no-store"},
        )

    def search_query(request, model):
        args = dict(request.query_params)
        if any(len(request.query_params.getlist(k)) != 1 for k in args):
            raise BusinessError("INVALID_ARGUMENT", "Repeated query parameters", 422)
        try:
            if "limit" in args:
                args["limit"] = int(args["limit"])
            return model.model_validate(args)
        except (ValueError, ValidationError):
            raise BusinessError(
                "INVALID_ARGUMENT", "Invalid search parameters", 422
            ) from None

    @app.get("/v1/season")
    def api_season(request: Request):
        return seasons.get(request.scope["wardrobe.principal"])

    @app.put("/v1/season")
    def api_set_season(request: Request, payload: SeasonInput):
        return seasons.set(
            request.scope["wardrobe.principal"], payload, idempotency_key(request)
        )

    @app.get("/v1/garment-search")
    def api_search(request: Request):
        return seasons.search(
            request.scope["wardrobe.principal"], search_query(request, SearchInput)
        )

    @app.get("/v1/history-search")
    def api_history_search(request: Request):
        return seasons.history(
            request.scope["wardrobe.principal"], search_query(request, HistorySearch)
        )

    @app.get("/v1/feedback/{event_id}")
    def api_get_feedback(request: Request, event_id: str):
        return seasons.feedback(request.scope["wardrobe.principal"], event_id)

    @app.post("/v1/annotation-suggestions")
    def api_annotation_suggestion(request: Request, payload: SuggestionInput):
        return JSONResponse(
            annotations.suggest(
                request.scope["wardrobe.principal"], payload, idempotency_key(request)
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/annotation-review")
    def api_annotation_review(request: Request):
        try:
            args = dict(request.query_params)
            if any(len(request.query_params.getlist(key)) != 1 for key in args):
                raise ValueError()
            if "limit" in args:
                args["limit"] = int(args["limit"])
            query = PageArgs.model_validate(args)
        except (ValueError, ValidationError):
            raise BusinessError(
                "INVALID_ARGUMENT", "Invalid review pagination arguments", 422
            ) from None
        return JSONResponse(
            annotations.list_review(request.scope["wardrobe.principal"], query),
            headers={"Cache-Control": "no-store"},
        )

    app.mount("/", mcp_app)
    schema = app.openapi()
    schema.setdefault("components", {}).setdefault("securitySchemes", {})[
        "WardrobeBearer"
    ] = {
        "type": "http",
        "scheme": "bearer",
        "description": "Pre-registered owner/client/channel grant; no OAuth discovery",
    }
    for path, operations in schema["paths"].items():
        for operation in operations.values():
            if isinstance(operation, dict):
                operation["security"] = (
                    [] if path == "/healthz" else [{"WardrobeBearer": []}]
                )
    return app
