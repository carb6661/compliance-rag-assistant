import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading

import anyio
from fastapi.testclient import TestClient
import pytest

from compliance_rag.access import AccessBoundary, AccessPolicy, AccessRejected, AccessSettings, Admission, initialize
from compliance_rag.api import create_app
from compliance_rag.evaluation import seed
from compliance_rag.generation import RagService
from compliance_rag.knowledge import KnowledgeStore

TOKENS = {role: "synthetic_" + role + "_" + "b" * 43 for role in ("reader", "analyst", "maintainer")}
QUESTION = {"question": "进入记录保留多久？", "scope": {
    "standard_code": "TRAINING-POLICY", "edition": "v2", "security_level": "第三级"}}


def policy(**limits):
    return AccessPolicy(AccessSettings(principals=[{"id": role, "role": role,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest()} for role, token in TOKENS.items()], limits=limits))


def headers(role):
    return {"Authorization": "Bearer " + TOKENS[role]}


@pytest.fixture
def app(tmp_path):
    app = create_app(policy())
    store = KnowledgeStore(tmp_path/"data.sqlite3")
    seed(store)
    app.state.rag_service = RagService(store)
    return app


def test_missing_configuration_locks_all_business_but_not_liveness(monkeypatch):
    monkeypatch.delenv("RAG_ACCESS_JSON", raising=False)
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/health").json()["access_configured"] is False
        for path in ("/v1/library", "/openapi.json", "/not-yet-implemented"):
            response = client.get(path)
            assert response.status_code == 503
            assert response.json() == {"error": "api_access_not_configured"}
    assert app.state.rag_service is None


@pytest.mark.parametrize("raw", ["", "null", "{}", "{bad", '{"principals":[],"secret":"PRIVATE"}',
    '{"principals":[],"principals":[]}', "x"*16385])
def test_invalid_env_is_not_anonymous_fallback_and_never_echoes(monkeypatch, raw):
    monkeypatch.setenv("RAG_ACCESS_JSON", raw)
    with pytest.raises(ValueError, match="invalid server access configuration") as error:
        create_app()
    assert "PRIVATE" not in str(error.value)


def test_settings_copy_does_not_change_after_construction():
    settings = AccessSettings(principals=[p.model_dump() for p in policy().principals])
    access = AccessPolicy(settings)
    settings.principals.clear()
    assert len(access.principals) == 3


@pytest.mark.parametrize("change", [
    {"role": "admin"}, {"token_sha256": "short"}, {"id": "../bad"}, {"raw_token": "PRIVATE"}])
def test_invalid_principal_config(change):
    entry = policy().principals[0].model_dump()
    with pytest.raises(ValueError):
        AccessSettings(principals=[{**entry, **change}])


@pytest.mark.parametrize("limit", [{"active_total": True}, {"active_total": 0}, {"requests_per_minute": 6001},
    {"active_per_principal": 9}, {"active_llm": 0}, {"unknown": 1}])
def test_invalid_limits_fail_closed(limit):
    with pytest.raises(ValueError):
        policy(**limit)


def test_duplicate_identity_or_digest_is_rejected():
    entry = policy().principals[0].model_dump()
    for other in (entry, {**entry, "id": "different"}, {**entry, "token_sha256": "f"*64}):
        with pytest.raises(ValueError):
            AccessSettings(principals=[entry, other])


@pytest.mark.parametrize("authorization", [None, "Basic not-a-token", "Bearer short", "Bearer " + "z"*43,
    "Bearer " + TOKENS["reader"] + ",other", "Bearer  " + TOKENS["reader"], "Bearer " + "z"*129])
def test_invalid_credentials_do_not_reveal_or_initialize(authorization):
    app = create_app(policy())
    with TestClient(app) as client:
        response = client.post("/v1/library/ingest", content="PRIVATE",
            headers={} if authorization is None else {"Authorization": authorization})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": "authentication_required"}
    assert app.state.rag_service is None


def test_duplicate_authorization_and_query_token_are_not_credentials(app):
    with TestClient(app) as client:
        assert client.get("/v1/library", headers=[("Authorization", "Bearer "+TOKENS["maintainer"])]*2).status_code == 401
        assert client.get("/v1/library?token="+TOKENS["maintainer"]).status_code == 401
        assert client.get("/v1/library", headers={"Authorization": "bearer "+TOKENS["maintainer"]}).status_code == 200


@pytest.mark.parametrize("role", ["reader", "analyst"])
@pytest.mark.parametrize("method,path", [("POST", "/v1/library/ingest"), ("POST", "/v1/library/remove"),
    ("POST", "/v1/library/source"), ("GET", "/v1/library"), ("POST", "/v1/parse"),
    ("GET", "/v1/traces/"+"a"*32), ("GET", "/openapi.json")])
def test_query_roles_cannot_maintain_even_with_malformed_body(role, method, path):
    app = create_app(policy())
    with TestClient(app) as client:
        response = client.request(method, path, content="not json", headers=headers(role))
    assert response.status_code == 403
    assert response.json() == {"error": "operation_forbidden"}
    assert app.state.rag_service is None
    assert app.state.admission.total == 0


def test_unknown_new_route_and_method_are_denied_by_default(app):
    with TestClient(app) as client:
        for method, path in (("GET", "/future-export"), ("DELETE", "/v1/library"), ("POST", "/v1/library/ingest/")):
            assert client.request(method, path, headers=headers("maintainer")).status_code == 403


def test_all_roles_can_query_but_role_in_body_cannot_escalate(app):
    with TestClient(app) as client:
        for role in TOKENS:
            result = client.post("/v1/ask", json=QUESTION, headers=headers(role))
            assert result.status_code == 200 and "180天" in result.json()["answer"]
            assert result.headers["cache-control"] == "no-store"
        assert client.post("/v1/ask", json={**QUESTION, "role": "maintainer"}, headers=headers("reader")).status_code == 422


class BlockingSelector:
    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = 0

    def select(self, question, hits):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(10):
            raise RuntimeError("test worker failed to release")
        return {"abstain": True, "excerpts": []}


def test_reader_cannot_trigger_external_work_even_when_provider_available(app):
    generator = BlockingSelector()
    app.state.rag_service.generator = generator
    with TestClient(app) as client:
        result = client.post("/v1/ask", json={**QUESTION, "generation": "llm"}, headers=headers("reader"))
    assert result.status_code == 403
    assert generator.calls == 0
    assert app.state.admission.total == app.state.admission.llm == 0


def test_real_rag_route_enforces_global_generation_limit_and_recovers(app):
    generator = BlockingSelector()
    app.state.rag_service.generator = generator
    with TestClient(app) as client, ThreadPoolExecutor(1) as pool:
        first = pool.submit(client.post, "/v1/ask", json={**QUESTION, "generation": "llm"}, headers=headers("analyst"))
        try:
            assert generator.entered.wait(5)
            denied = client.post("/v1/ask", json={**QUESTION, "generation": "llm"}, headers=headers("maintainer"))
            assert denied.status_code == 429 and denied.json()["error"] == "generation_capacity_exhausted"
            # LLM saturation does not block a different principal's local query.
            assert client.post("/v1/ask", json=QUESTION, headers=headers("reader")).status_code == 200
            assert generator.calls == 1
        finally:
            generator.release.set()
        assert first.result(5).json()["reason"] == "model_abstained"
        assert client.post("/v1/ask", json={**QUESTION, "generation": "llm"}, headers=headers("maintainer")).status_code == 200
    assert generator.calls == 2
    assert app.state.admission.total == app.state.admission.llm == 0


def test_exact_monotonic_rolling_window_including_failed_admitted_requests():
    now = [100.0]
    gate = Admission(policy(requests_per_minute=2), clock=lambda: now[0])
    person = policy().principals[0]
    gate.open(person).close()
    now[0] = 101
    gate.open(person).close()
    now[0] = 159.5
    with pytest.raises(AccessRejected) as error:
        gate.open(person)
    assert error.value.retry_after == 1
    now[0] = 160
    gate.open(person).close()
    assert list(gate.windows[person.id]) == [101, 160]


def test_parallel_rate_admission_is_atomic_and_state_stays_bounded():
    access = policy(requests_per_minute=5, active_total=32, active_per_principal=8)
    gate = Admission(access, clock=lambda: 10.0)
    def attempt(_):
        try:
            gate.open(access.principals[0]).close()
            return 200
        except AccessRejected as error:
            return error.status
    with ThreadPoolExecutor(8) as pool:
        statuses = list(pool.map(attempt, range(32)))
    assert statuses.count(200) == 5 and statuses.count(429) == 27
    assert gate.total == 0 and len(gate.windows) == 3


def test_per_principal_and_total_capacity_are_independent():
    access = policy(active_total=2)
    gate = Admission(access)
    one = gate.open(access.principals[0])
    with pytest.raises(AccessRejected, match="task_capacity_exhausted"):
        gate.open(access.principals[0])
    two = gate.open(access.principals[1])
    with pytest.raises(AccessRejected):
        gate.open(access.principals[2])
    one.close()
    three = gate.open(access.principals[2])
    two.close()
    three.close()
    assert gate.total == 0


def test_http_completion_cannot_release_a_running_sync_worker():
    access = policy(active_total=1)
    gate = Admission(access)
    lease = gate.open(access.principals[1])
    entered, release = threading.Event(), threading.Event()
    def blocking():
        with lease.work(external=True):
            entered.set()
            assert release.wait(5)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(blocking)
        try:
            assert entered.wait(5)
            lease.close()
            lease.close()  # cleanup is idempotent
            assert gate.total == gate.llm == 1
            with pytest.raises(AccessRejected):
                gate.open(access.principals[1])
        finally:
            release.set()
        future.result(5)
    assert gate.total == gate.llm == 0


def test_cancelled_before_worker_starts_must_not_execute_later():
    access = policy()
    gate = Admission(access)
    lease = gate.open(access.principals[0])
    lease.close()
    with pytest.raises(AccessRejected, match="task_no_longer_admitted"):
        with lease.work():
            pytest.fail("cancelled work ran")
    assert gate.total == 0


def test_body_not_read_for_unauthorized_forbidden_or_busy():
    async def run():
        access = policy(active_total=1)
        gate = Admission(access)
        called = []
        async def app(scope, receive, send):
            called.append(True)
        async def receive():
            raise AssertionError("body must not be read")
        def scope(role=None, path="/v1/ask"):
            return {"type": "http", "method": "POST", "path": path, "headers": [] if role is None else
                    [(b"authorization", headers(role)["Authorization"].encode())]}
        boundary = AccessBoundary(app, access, gate)
        async def status(request):
            messages = []
            async def send(message):
                messages.append(message)
            await boundary(request, receive, send)
            return messages[0]["status"]
        assert await status(scope()) == 401
        assert await status(scope("reader", "/v1/library/ingest")) == 403
        lease = gate.open(access.principals[0])
        try:
            assert await status(scope("analyst")) == 429
        finally:
            lease.close()
        assert not called
    asyncio.run(run())


def test_real_asgi_cancellation_retains_worker_hold():
    async def run():
        access = policy(active_total=1)
        gate = Admission(access)
        entered, release = threading.Event(), threading.Event()
        finished = threading.Event()
        def worker(lease):
            try:
                with lease.work(external=True):
                    entered.set()
                    assert release.wait(5)
            finally:
                finished.set()
        async def downstream(scope, receive, send):
            await anyio.to_thread.run_sync(worker, scope["state"]["access_lease"], abandon_on_cancel=True)
        async def unused(*args):
            pass
        boundary = AccessBoundary(downstream, access, gate)
        scope = {"type": "http", "method": "POST", "path": "/v1/ask",
                 "headers": [(b"authorization", headers("analyst")["Authorization"].encode())]}
        task = asyncio.create_task(boundary(scope, unused, unused))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert gate.total == gate.llm == 1
            with pytest.raises(AccessRejected):
                gate.open(access.principals[0])
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 5)
        assert gate.total == gate.llm == 0
    asyncio.run(run())


def test_worker_and_body_failures_release_slots_but_consume_rate(app):
    with TestClient(app) as client:
        for body in ("not-json", '{"x":1,"x":2}', "{}"):
            assert client.post("/v1/ask", content=body, headers={**headers("reader"), "Content-Type": "application/json"}).status_code == 422
        app.state.rag_service.store.search = lambda _: (_ for _ in ()).throw(ValueError("PRIVATE"))
        result = client.post("/v1/search", json=QUESTION, headers=headers("reader"))
        assert result.status_code == 503 and "PRIVATE" not in result.text
    assert app.state.admission.total == 0
    assert len(app.state.admission.windows["reader"]) == 4


def test_initialize_uses_random_tokens_private_files_and_never_overwrites(tmp_path):
    directory = tmp_path/"access"
    initialize(directory)
    config = json.loads((directory/"access.json").read_text())
    access = AccessPolicy(AccessSettings.model_validate(config))
    tokens = [(directory/(role+".token")).read_text() for role in TOKENS]
    assert len(set(tokens)) == 3
    for role, token in zip(TOKENS, tokens):
        assert len(token) == 43
        assert token not in json.dumps(config)
        assert access.authenticate([(b"authorization", ("Bearer "+token).encode())]).role == role
    with pytest.raises(FileExistsError):
        initialize(directory)
    assert tokens == [(directory/(role+".token")).read_text() for role in TOKENS]


def test_demo_matches_checked_in_evidence():
    from compliance_rag.access_demo import demonstrate
    expected = Path(__file__).resolve().parents[1]/"examples/evaluation/access-admission.json"
    assert demonstrate() == json.loads(expected.read_text(encoding="utf-8"))


def test_liveness_does_not_read_an_unauthenticated_body():
    async def run():
        app = create_app(AccessPolicy())
        async def receive():
            raise AssertionError("liveness must not read any request body")
        messages = []
        async def send(message):
            messages.append(message)
        await app({"type": "http", "method": "GET", "path": "/health", "query_string": b"",
                   "headers": [(b"host", b"localhost")], "scheme": "http", "server": ("localhost", 80),
                   "client": ("127.0.0.1", 1), "http_version": "1.1"}, receive, send)
        assert messages[0]["status"] == 200
    asyncio.run(run())


def test_openapi_records_bearer_without_token_and_health_exception(app):
    with TestClient(app) as client:
        result = client.get("/openapi.json", headers=headers("maintainer"))
        assert result.status_code == 200
        schema = result.json()
        assert schema["security"] == [{"LocalBearer": []}]
        assert schema["paths"]["/health"]["get"]["security"] == []
        assert not any(token in result.text for token in TOKENS.values())
        assert client.get("/docs", headers=headers("maintainer")).status_code == 403


def test_body_budget_and_unexpected_worker_failure_release_capacity(app):
    from compliance_rag.api import MAX_BODY
    with TestClient(app, raise_server_exceptions=False) as client:
        big = client.post("/v1/ask", content="x"*(MAX_BODY+1), headers={**headers("reader"), "Content-Type": "application/json"})
        assert big.status_code == 413
        app.state.rag_service.store.search = lambda _: (_ for _ in ()).throw(RuntimeError("PRIVATE"))
        failed = client.post("/v1/ask", json=QUESTION, headers=headers("reader"))
        assert failed.status_code == 500 and "PRIVATE" not in failed.text
        assert failed.headers["cache-control"] == "no-store"
    assert app.state.admission.total == 0


def test_server_env_configuration_is_snapshotted_not_reloaded_per_request(monkeypatch):
    entries = [p.model_dump() for p in policy().principals]
    monkeypatch.setenv("RAG_ACCESS_JSON", json.dumps({"principals": entries}))
    app = create_app()
    monkeypatch.setenv("RAG_ACCESS_JSON", "invalid after startup")
    assert app.state.access_policy.authenticate([(b"authorization", headers("reader")["Authorization"].encode())]).role == "reader"
