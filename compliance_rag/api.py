"""Authenticated loopback prototype. No file-path, URL-fetch or upload endpoint."""
from typing import Literal

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from pydantic import Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from .models import IngestConfig, MAX_BYTES, StrictModel
from .parser import InputError, parse_markdown, source_label
from .generation import strict_json
from .access import AccessBoundary, AccessPolicy, AccessRejected, Admission, work

MAX_BODY = 2 * MAX_BYTES


class ParseRequest(StrictModel):
    text: str = Field(max_length=MAX_BYTES)
    source_name: str = Field(default="input.md", max_length=256)
    config: IngestConfig = Field(default_factory=IngestConfig)
    mode: Literal["strict", "partial"] = "strict"

    @field_validator("source_name")
    @classmethod
    def relative_source(cls, value):
        return source_label(value)


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["method"] != "POST":
            # All inputs are POST JSON. Public liveness never buffers a GET body.
            return await self.app(scope, receive, send)
        raw_headers = scope.get("headers", [])
        if sum(key.lower() == b"content-type" for key, _ in raw_headers) > 1:
            return await JSONResponse({"error": "duplicate content type"}, status_code=400)(scope, receive, send)
        headers = dict(raw_headers)
        if scope["method"] == "POST" and headers.get(b"content-type", b"").split(b";")[0].strip().lower() != b"application/json":
            return await JSONResponse({"error": "application/json required"}, status_code=415)(scope, receive, send)
        size, frames, body = 0, 0, bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            frames += 1
            if size > MAX_BODY or frames > 4096:
                return await JSONResponse({"error": "request exceeds byte/frame budget"}, status_code=413)(scope, receive, send)
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        if scope["method"] == "POST":
            try:
                strict_json(bytes(body).decode("utf-8"))
            except (ValueError, RecursionError):
                return await JSONResponse({"error": "invalid or ambiguous JSON"}, status_code=422)(scope, receive, send)
        replayed = False
        async def replay():
            nonlocal replayed
            payload = b"" if replayed else bytes(body)
            replayed = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await self.app(scope, replay, send)


router = APIRouter()


async def validation_error(request, exc):
    # FastAPI defaults can include rejected input. Keep diagnostics content-free.
    return JSONResponse({"error": "invalid request schema; no source content echoed"}, status_code=422)


@router.get("/health")
def health(request: Request):
    return {"status": "ok", "phase": "source-authorized-evidence-qa", "version": "0.5.0",
            "access_configured": bool(request.app.state.access_policy.principals)}


@router.post("/v1/parse")
def parse(payload: ParseRequest, request: Request):
    with work(request):
        return parse_payload(payload)


def parse_payload(request):
    try:
        result = parse_markdown(request.text, request.source_name, request.config)
    except (InputError, UnicodeError, RecursionError):
        return JSONResponse({"error": "invalid text/configuration or input limit"}, status_code=422)
    blocked = bool(result.errors and request.mode == "strict")
    docs = [] if blocked else result.documents
    body = {"status": "blocked" if blocked else ("partial" if result.errors else "success"),
            "candidate_chunks": len(result.documents), "accepted_chunks": len(docs),
            "source_sha256": result.source_hash, "config_sha256": result.config_hash,
            "diagnostics": [d.model_dump() for d in result.diagnostics],
            "documents": [{"page_content": d.page_content, "metadata": d.metadata} for d in docs]}
    return JSONResponse(body, status_code=422 if blocked else 200)


def create_app(policy=None):
    policy = AccessPolicy.from_env() if policy is None else policy
    app = FastAPI(title="Compliance RAG Assistant · 可溯源证据问答", version="0.5.0",
                  docs_url=None, redoc_url=None)
    app.state.rag_service = None
    app.state.access_policy = policy
    app.state.admission = Admission(policy)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(AccessRejected, lambda request, error: error.response())
    app.add_exception_handler(Exception, lambda request, error: JSONResponse(
        {"error": "internal service failure; no details echoed"}, status_code=500,
        headers={"Cache-Control": "no-store"}))
    app.include_router(router)
    from .rag_api import attach
    attach(app)
    app.add_middleware(BodyLimit)
    app.add_middleware(AccessBoundary, policy=policy, gate=app.state.admission)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    def authenticated_schema():
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            schema.setdefault("components", {})["securitySchemes"] = {
                "LocalBearer": {"type": "http", "scheme": "bearer",
                    "description": "Local server-issued opaque token. Roles and limits: docs/ACCESS_CONTROL.md. Not OAuth/OIDC."}}
            schema["security"] = [{"LocalBearer": []}]
            schema["paths"]["/health"]["get"]["security"] = []
            app.openapi_schema = schema
        return app.openapi_schema
    app.openapi = authenticated_schema
    return app


app = create_app()
