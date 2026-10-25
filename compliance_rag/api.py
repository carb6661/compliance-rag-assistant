"""Loopback-only prototype interface. No file-path, URL-fetch or upload endpoint."""
from typing import Literal

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from .models import IngestConfig, MAX_BYTES, StrictModel
from .parser import InputError, parse_markdown, source_label
from .generation import strict_json

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


app = FastAPI(title="Compliance RAG Assistant · 可溯源证据问答", version="0.3.0")
app.state.rag_service = None
app.add_middleware(BodyLimit)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # FastAPI defaults can include rejected input. Keep diagnostics content-free.
    return JSONResponse({"error": "invalid request schema; no source content echoed"}, status_code=422)


@app.get("/health")
def health():
    return {"status": "ok", "phase": "conditional-knowledge-maintenance-and-qa", "version": "0.3.0"}


@app.post("/v1/parse")
def parse(request: ParseRequest):
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


from .rag_api import attach
attach(app)
