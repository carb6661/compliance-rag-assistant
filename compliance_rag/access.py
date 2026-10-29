"""Single-process API credentials and bounded admission; not a tenant/IdP system."""
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.responses import JSONResponse

from .generation import strict_json
from .authorization import ReadIdentity


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    requests_per_minute: int = Field(default=120, ge=1, le=6000)
    active_total: int = Field(default=4, ge=1, le=32)
    active_per_principal: int = Field(default=1, ge=1, le=8)
    active_llm: int = Field(default=1, ge=1, le=8)


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,32}$")
    role: Literal["reader", "analyst", "maintainer"]
    token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)


class AccessSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    principals: list[Principal] = Field(min_length=1, max_length=16)
    limits: Limits = Field(default_factory=Limits)

    @model_validator(mode="after")
    def unique(self):
        if (len({p.id for p in self.principals}) != len(self.principals)
                or len({p.token_sha256 for p in self.principals}) != len(self.principals)):
            raise ValueError("duplicate principal or credential")
        return self


class AccessPolicy:
    def __init__(self, settings=None):
        # Copy into immutable values. No live env reload or client-selected policy.
        self.principals = tuple(settings.principals) if settings else ()
        self.limits = settings.limits if settings else Limits()

    @classmethod
    def from_env(cls):
        raw = os.getenv("RAG_ACCESS_JSON")
        if raw is None:
            return cls()  # Locked: only liveness is public, never anonymous fallback.
        try:
            if len(raw.encode("utf-8")) > 16384:
                raise ValueError()
            return cls(AccessSettings.model_validate(strict_json(raw)))
        except (ValueError, RecursionError):
            raise ValueError("invalid server access configuration; no values echoed") from None

    def authenticate(self, headers):
        values = [value for key, value in headers if key.lower() == b"authorization"]
        if len(values) != 1:
            return None
        match = re.fullmatch(rb"(?i:Bearer) ([A-Za-z0-9_-]{43,128})", values[0])
        if match is None:
            return None
        digest = hashlib.sha256(match[1]).hexdigest()
        found = None
        # Compare fixed-sized digests without early exit; never return token material.
        for principal in self.principals:
            if hmac.compare_digest(digest, principal.token_sha256):
                found = principal
        return found


class AccessRejected(Exception):
    def __init__(self, code, status, retry_after=None):
        self.code, self.status, self.retry_after = code, status, retry_after
        super().__init__(code)

    def response(self):
        headers = {"Cache-Control": "no-store"}
        if self.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if self.retry_after is not None:
            headers["Retry-After"] = str(self.retry_after)
        return JSONResponse({"error": self.code}, status_code=self.status, headers=headers)


class Admission:
    """Bounded to configured principals. Monotonic sliding window; zero task backlog."""
    def __init__(self, policy, clock=time.monotonic):
        self.limits, self.clock = policy.limits, clock
        self.lock = threading.Lock()
        self.windows = {p.id: deque() for p in policy.principals}
        self.active = {p.id: 0 for p in policy.principals}
        self.total, self.llm = 0, 0

    def open(self, principal):
        with self.lock:
            now = self.clock()
            window = self.windows[principal.id]
            while window and window[0] <= now - 60:
                window.popleft()
            if len(window) >= self.limits.requests_per_minute:
                raise AccessRejected("request_rate_limited", 429, max(1, math.ceil(window[0]+60-now)))
            # Authorized attempts count even if busy, invalid, conflicting or failing later.
            window.append(now)
            if self.total >= self.limits.active_total or self.active[principal.id] >= self.limits.active_per_principal:
                raise AccessRejected("task_capacity_exhausted", 429, 1)
            self.total += 1
            self.active[principal.id] += 1
            return Lease(self, principal)


@dataclass
class Lease:
    gate: Admission
    principal: Principal
    http_open: bool = True
    running: bool = False
    released: bool = False
    started: bool = False
    external: bool = False

    def _release(self):
        if not self.released and not self.http_open and not self.running:
            self.gate.total -= 1
            self.gate.active[self.principal.id] -= 1
            self.released = True

    @contextmanager
    def work(self, *, external=False):
        # The sync worker owns this hold until it really exits. HTTP cancellation
        # must not free a slot while a provider/embedding call still runs.
        with self.gate.lock:
            if not self.http_open or self.started:
                raise AccessRejected("task_no_longer_admitted", 503)
            if external and self.principal.role == "reader":
                raise AccessRejected("external_generation_forbidden", 403)
            if external and self.gate.llm >= self.gate.limits.active_llm:
                raise AccessRejected("generation_capacity_exhausted", 429, 1)
            self.started = self.running = True
            self.external = external
            if external:
                self.gate.llm += 1
        try:
            yield
        finally:
            with self.gate.lock:
                self.running = False
                if self.external:
                    self.gate.llm -= 1
                self._release()

    def close(self):
        with self.gate.lock:
            self.http_open = False
            self._release()


def permitted(principal, method, path):
    # Exact, deny-by-default routes. New endpoints must be deliberately classified.
    query = {("POST", "/v1/search"), ("POST", "/v1/ask")}
    maintenance = {("POST", "/v1/parse"), ("GET", "/v1/library"),
                   ("POST", "/v1/library/source"), ("POST", "/v1/library/ingest"),
                   ("POST", "/v1/library/access"), ("POST", "/v1/library/access/update"),
                   ("POST", "/v1/library/remove"), ("GET", "/openapi.json")}
    return ((method, path) in query or (principal.role == "maintainer" and (
        (method, path) in maintenance or (method == "GET" and path.startswith("/v1/traces/")))))


class AccessBoundary:
    def __init__(self, app, policy, gate):
        self.app, self.policy, self.gate = app, policy, gate

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        if scope["type"] != "http" or (scope["method"], scope["path"]) == ("GET", "/health"):
            return await self.app(scope, receive, send)
        lease = None
        try:
            if not self.policy.principals:
                raise AccessRejected("api_access_not_configured", 503)
            principal = self.policy.authenticate(scope.get("headers", []))
            if principal is None:
                raise AccessRejected("authentication_required", 401)
            if not permitted(principal, scope["method"], scope["path"]):
                raise AccessRejected("operation_forbidden", 403)
            lease = self.gate.open(principal)  # Before reading or parsing a body.
            scope.setdefault("state", {})["access_lease"] = lease

            async def private_send(message):
                if message["type"] == "http.response.start":
                    message = dict(message)
                    message["headers"] = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                    message["headers"].append((b"cache-control", b"no-store"))
                await send(message)

            await self.app(scope, receive, private_send)
        except AccessRejected as error:
            await error.response()(scope, receive, send)
        finally:
            if lease is not None:
                lease.close()


def work(request, *, external=False):
    return request.state.access_lease.work(external=external)


def read_identity(request):
    principal = request.state.access_lease.principal
    return ReadIdentity(principal.id, principal.token_sha256, principal.role == "maintainer")


def initialize(directory):
    """Exclusive private output; token plaintext is never printed to logs."""
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    principals = []
    for role in ("reader", "analyst", "maintainer"):
        token = secrets.token_urlsafe(32)
        with os.fdopen(os.open(directory/(role+".token"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
            stream.write(token)
        principals.append({"id": role, "role": role, "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest()})
    config = AccessSettings(principals=principals).model_dump()
    with os.fdopen(os.open(directory/"access.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Create private API tokens and digest policy; never commit or share the token files.")
    parser.add_argument("directory", help="new private directory, e.g. output/access")
    args = parser.parse_args()
    try:
        initialize(args.directory)
    except (OSError, ValueError):
        parser.exit(2, "Access initialization failed; existing outputs are never overwritten. Check the private directory.\n")
    print("Private access files created; load access.json on the server and one role token on the client. See docs/ACCESS_CONTROL.md.")


if __name__ == "__main__":
    main()
