"""Private local application endpoints; endpoints cannot set paths, providers or secrets."""
import os
import sqlite3
import threading

from fastapi import APIRouter, Request
from pydantic import Field, field_validator
from starlette.responses import JSONResponse

from .generation import AskRequest, RagService, generator_from_env
from .knowledge import KnowledgeStore, SearchRequest
from .models import IngestConfig, MAX_BYTES, StrictModel
from .parser import InputError, source_label
from .conditions import WriteRejected, from_headers
from .access import work

router = APIRouter()
LOCK = threading.Lock()


class IngestRequest(StrictModel):
    text: str = Field(max_length=MAX_BYTES)
    source_name: str = Field(min_length=1, max_length=256)
    config: IngestConfig = Field(default_factory=IngestConfig)

    @field_validator("source_name")
    @classmethod
    def validate_label(cls, value):
        return source_label(value)


class DeleteRequest(StrictModel):
    source_name: str = Field(min_length=1, max_length=256)

    @field_validator("source_name")
    @classmethod
    def validate_label(cls, value):
        return source_label(value)


def service(request):
    with LOCK:
        if request.app.state.rag_service is None:
            embedder = None
            model_path = os.getenv("RAG_EMBEDDING_MODEL_PATH")
            if model_path:
                from .embeddings import ChineseEmbedder
                embedder = ChineseEmbedder(model_path)
            store = KnowledgeStore(os.getenv("RAG_DB_PATH", "output/knowledge.sqlite3"), embedder)
            request.app.state.rag_service = RagService(store, generator_from_env())
        return request.app.state.rag_service


@router.get("/v1/library")
def inventory(request: Request):
    with work(request):
        return JSONResponse(service(request).store.inventory(), headers={"Cache-Control":"no-store"})


@router.post("/v1/library/ingest")
def ingest(payload: IngestRequest, request: Request):
    with work(request):
        condition = from_headers(request.headers)
        result = service(request).store.ingest(payload.text, payload.source_name, payload.config, condition=condition)
        return JSONResponse(result, headers={"ETag": result["source_etag"], "Cache-Control": "no-store"})


@router.post("/v1/library/source")
def source(payload: DeleteRequest, request: Request):
    with work(request):
        result = service(request).store.source(payload.source_name)
        if result is None:
            return JSONResponse({"error": "source not found"}, status_code=404, headers={"Cache-Control": "no-store"})
        return JSONResponse(result, headers={"ETag": result["source_etag"], "Cache-Control": "no-store"})


@router.post("/v1/library/remove")
def remove(payload: DeleteRequest, request: Request):
    with work(request):
        condition = from_headers(request.headers, delete=True)
        return JSONResponse(service(request).store.delete(payload.source_name, condition=condition),
                            headers={"Cache-Control":"no-store"})


@router.post("/v1/search")
def search(payload: SearchRequest, request: Request):
    with work(request):
        return service(request).store.search(payload)


@router.post("/v1/ask")
def ask(payload: AskRequest, request: Request):
    with work(request, external=payload.generation == "llm"):
        return service(request).ask(payload)


@router.get("/v1/traces/{trace_id}")
def trace(trace_id: str, request: Request):
    with work(request):
        return read_trace(trace_id, request)


def read_trace(trace_id, request):
    if len(trace_id) != 32 or any(c not in "0123456789abcdef" for c in trace_id):
        return JSONResponse({"error": "trace not found"}, status_code=404)
    result = service(request).store.trace(trace_id)
    return result if result else JSONResponse({"error": "trace not found"}, status_code=404)


def attach(app):
    app.include_router(router)

    @app.exception_handler(WriteRejected)
    def rejected_write(request, error):
        return JSONResponse({"error": error.code, "advice": "Read and review the current source before retrying; never overwrite automatically."},
                            status_code=error.status, headers={"Cache-Control": "no-store"})

    @app.exception_handler(InputError)
    def invalid(request, error):
        return JSONResponse({"error": "invalid source, scope or index configuration; no content echoed"}, status_code=422)

    @app.exception_handler(sqlite3.Error)
    def unavailable(request, error):
        return JSONResponse({"error": "knowledge store unavailable; no partial result"}, status_code=503)

    @app.exception_handler(ValueError)
    def bad_configuration(request, error):
        return JSONResponse({"error": "model or index configuration failed validation"}, status_code=503)
