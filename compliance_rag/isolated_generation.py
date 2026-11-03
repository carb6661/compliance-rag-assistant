"""Supervise one fixed provider worker; no client-selected executable or pickle IPC."""
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .generation import CompatibleSettings, ModelFailure, Selection, strict_json

MAX_PACKET = 65536
UPSTREAM_REASONS = frozenset({"upstream_http_error", "upstream_response_limit", "upstream_incomplete",
    "upstream_refusal_or_schema", "upstream_transport_or_schema", "invalid_model_selection"})


class ProviderFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=128)
    api_key: str = Field(min_length=1, max_length=4096, repr=False)

    @model_validator(mode="after")
    def validate_provider(self):
        CompatibleSettings(self.base_url, self.model, self.api_key)
        return self


class EvidenceFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    citation_id: str = Field(pattern=r"^C[1-5]$")
    text: str = Field(min_length=1, max_length=2000)


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    protocol: Literal[1]
    provider: ProviderFields = Field(repr=False)
    question: str = Field(min_length=1, max_length=500)
    evidence: list[EvidenceFields] = Field(min_length=1, max_length=5)

    @field_validator("protocol", mode="before")
    @classmethod
    def strict_protocol(cls, value):
        if type(value) is not int:
            raise ValueError("invalid worker protocol")
        return value

    @model_validator(mode="after")
    def bounded_evidence(self):
        if (not self.question.strip() or sum(len(item.text) for item in self.evidence) > 6000
                or len({item.citation_id for item in self.evidence}) != len(self.evidence)):
            raise ValueError("invalid worker evidence budget")
        return self


@dataclass(frozen=True)
class WorkerLimits:
    deadline_seconds: float = 45.0
    max_workers: int = 1
    cleanup_seconds: float = 1.0

    def __post_init__(self):
        for value, low, high in ((self.deadline_seconds, .25, 120), (self.cleanup_seconds, .05, 5)):
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("invalid generation worker limits")
        if type(self.max_workers) is not int or not 1 <= self.max_workers <= 8:
            raise ValueError("invalid generation worker count")

    @classmethod
    def from_env(cls):
        try:
            return cls(deadline_seconds=float(os.getenv("RAG_LLM_DEADLINE_SECONDS", "45")),
                       max_workers=int(os.getenv("RAG_LLM_WORKERS", "1")))
        except ValueError:
            raise ValueError("invalid server generation worker limits; no values echoed") from None


@dataclass(eq=False)
class _OwnedWorker:
    process: object = None
    writer_done: threading.Event = field(default_factory=threading.Event)
    reader_done: threading.Event = field(default_factory=threading.Event)
    output: bytes = field(default=b"", repr=False)
    io_failed: bool = False
    quarantined: bool = False

    def __post_init__(self):
        self.writer_done.set()
        self.reader_done.set()


def _write_request(record, packet):
    try:
        view = memoryview(packet)
        while view:
            written = record.process.stdin.write(view)
            if not written:
                raise OSError()
            view = view[written:]
    except Exception:
        record.io_failed = True
    finally:
        try:
            record.process.stdin.close()
        except OSError:
            record.io_failed = True
        record.writer_done.set()


def _read_response(record):
    body = bytearray()
    try:
        while len(body) <= MAX_PACKET:
            block = record.process.stdout.read(min(4096, MAX_PACKET+1-len(body)))
            if not block:
                break
            body.extend(block)
    except Exception:
        record.io_failed = True
    finally:
        record.output = bytes(body)
        record.reader_done.set()


def _close_pipes(record):
    for stream in (record.process.stdin, record.process.stdout):
        try:
            stream.close()
        except OSError:
            pass
    record.output = b""


def _exited(process):
    try:
        return process.poll() is not None
    except OSError:
        return False


class IsolatedGenerator:
    """Wall-clock budget for selection only. A cleanup failure retains worker capacity."""
    def __init__(self, settings: CompatibleSettings, limits=None):
        # Validate the same bounded representation the child will receive.
        try:
            ProviderFields(base_url=settings.base_url, model=settings.model, api_key=settings.api_key)
        except (ValueError, TypeError):
            raise ValueError("invalid bounded provider configuration; no values echoed") from None
        self.settings, self.limits = settings, limits or WorkerLimits()
        self._lock, self._owned = threading.Lock(), set()

    def _reap_quarantined(self):
        for record in tuple(self._owned):
            if (record.quarantined and _exited(record.process)
                    and record.writer_done.is_set() and record.reader_done.is_set()):
                _close_pipes(record)
                self._owned.remove(record)

    def state(self):
        with self._lock:
            self._reap_quarantined()
            return {"occupied": len(self._owned), "quarantined": sum(r.quarantined for r in self._owned)}

    def _reserve(self):
        with self._lock:
            self._reap_quarantined()
            if len(self._owned) >= self.limits.max_workers:
                raise ModelFailure("generation_worker_busy")
            record = _OwnedWorker()
            self._owned.add(record)
            return record

    @staticmethod
    def _launch():
        worker = Path(__file__).resolve().with_name("provider_worker.py")
        # Do not inherit API tokens, DB/model paths, proxies, Python path overrides,
        # or generation credentials. The one required credential travels over stdin.
        env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ}
        return subprocess.Popen([sys.executable, "-I", str(worker)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=str(worker.parent.parent),
            bufsize=0, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def _cleanup(self, record):
        deadline = time.monotonic()+self.limits.cleanup_seconds
        process = record.process
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()  # Only this exact Popen child, never a PID/name search.
                process.wait(timeout=max(0, deadline-time.monotonic()))
            except (OSError, subprocess.TimeoutExpired):
                pass
            record.writer_done.wait(max(0, deadline-time.monotonic()))
            record.reader_done.wait(max(0, deadline-time.monotonic()))
        confirmed = process is None or (_exited(process) and record.writer_done.is_set() and record.reader_done.is_set())
        with self._lock:
            if confirmed:
                if process is not None:
                    _close_pipes(record)
                self._owned.remove(record)
            else:
                record.quarantined = True  # Never advertise free capacity without confirmation.
        return confirmed

    def select(self, question, hits):
        try:
            request = WorkerRequest(protocol=1, provider=ProviderFields(base_url=self.settings.base_url,
                model=self.settings.model, api_key=self.settings.api_key), question=question,
                evidence=[EvidenceFields(citation_id=h["citation_id"], text=h["evidence_text"]) for h in hits])
            packet = json.dumps(request.model_dump(), ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(packet) > MAX_PACKET:
                raise ValueError()
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ModelFailure("generation_worker_input_invalid") from None
        record = self._reserve()
        deadline = time.monotonic()+self.limits.deadline_seconds
        try:
            try:
                record.process = self._launch()
                if time.monotonic() >= deadline:
                    raise ModelFailure("generation_deadline_exceeded")
                for operation, event, args in ((_write_request, record.writer_done, (record, packet)),
                                                (_read_response, record.reader_done, (record,))):
                    event.clear()
                    try:
                        threading.Thread(target=operation, args=args, name="rag-provider-pipe", daemon=True).start()
                    except RuntimeError:
                        event.set()
                        raise
            except (OSError, RuntimeError, ValueError):
                raise ModelFailure("generation_worker_start_failed") from None
            if not record.reader_done.wait(max(0, deadline-time.monotonic())):
                raise ModelFailure("generation_deadline_exceeded")
            if len(record.output) > MAX_PACKET:
                raise ModelFailure("generation_worker_response_limit")
            try:
                record.process.wait(timeout=max(0, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                raise ModelFailure("generation_deadline_exceeded") from None
            except OSError:
                raise ModelFailure("generation_worker_failed") from None
            if not record.writer_done.wait(max(0, deadline-time.monotonic())) or time.monotonic() >= deadline:
                raise ModelFailure("generation_deadline_exceeded")
            if record.process.returncode != 0 or record.io_failed:
                raise ModelFailure("generation_worker_failed")
            selected = self._decode(record.output)
            if time.monotonic() >= deadline:
                raise ModelFailure("generation_deadline_exceeded")
            return selected
        finally:
            if not self._cleanup(record):
                raise ModelFailure("generation_worker_cleanup_failed") from None

    @staticmethod
    def _decode(output):
        try:
            message = strict_json(output.decode("utf-8"))
            if not isinstance(message, dict) or type(message.get("protocol")) is not int or message["protocol"] != 1:
                raise ValueError()
            if set(message) == {"protocol", "status", "selection"} and message["status"] == "ok":
                return Selection.model_validate(message["selection"]).model_dump()
            if set(message) == {"protocol", "status", "reason"} and message["status"] == "error" and message["reason"] in UPSTREAM_REASONS:
                raise ModelFailure(message["reason"])
            raise ValueError()
        except ModelFailure:
            raise
        except (ValueError, KeyError, TypeError, RecursionError):
            raise ModelFailure("generation_worker_protocol_error") from None
