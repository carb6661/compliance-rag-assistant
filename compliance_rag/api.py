"""Loopback-only prototype interface. No file-path, URL-fetch or upload endpoint."""
from typing import Literal

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from .models import IngestConfig, MAX_BYTES, StrictModel
from .parser import InputError, parse_markdown, source_label

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
        headers = dict(scope.get("headers", []))
        if scope["method"] == "POST" and headers.get(b"content-type", b"").split(b";")[0].strip().lower() != b"application/json":
            return await JSONResponse({"error": "application/json required"}, status_code=415)(scope, receive, send)
        size, messages = 0, []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > MAX_BODY:
                return await JSONResponse({"error": "request exceeds 2 MiB"}, status_code=413)(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break
        async def replay():
            if messages:
                return messages.pop(0)
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.app(scope, replay, send)


app = FastAPI(title="Compliance RAG Assistant · 结构化入库", version="0.1.0")
app.add_middleware(BodyLimit)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # FastAPI defaults can include rejected input. Keep diagnostics content-free.
    return JSONResponse({"error": "invalid request schema; no source content echoed"}, status_code=422)


@app.get("/health")
def health():
    return {"status": "ok", "phase": "structured-ingestion", "version": "0.1.0"}


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
