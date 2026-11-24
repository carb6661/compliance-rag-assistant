from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from compliance_rag import audit, __version__
from compliance_rag.access import AccessPolicy, AccessSettings
from compliance_rag.api import create_app
from compliance_rag.authorization import ReaderGrant
from compliance_rag.conditions import WriteCondition, WriteRejected
from compliance_rag.evaluation import DATA, sample_config
from compliance_rag.generation import RagService
from compliance_rag.knowledge import KnowledgeStore
from compliance_rag.parser import canonical_json, sha

NAME = "synthetic/policy.md"
TEXT = (DATA/"v2.md").read_text(encoding="utf-8")
GRANT = ReaderGrant("synthetic-reader", "a"*64)


def match(store):
    return WriteCondition.match(store.source(NAME)["source_etag"])


def access_match(store):
    return WriteCondition.match(store.source_access(NAME)["access_etag"])


def put(store, text=TEXT, condition=None, actor=None):
    return store.ingest(text, NAME, sample_config("v2"), condition=condition or WriteCondition.create(), actor=actor)


@pytest.fixture
def store(tmp_path):
    result = KnowledgeStore(tmp_path/"audit.sqlite3")
    put(result)
    return result


def snapshot(store):
    with store.connect() as db:
        return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in ("sources", "chunks", "source_grants", "settings", "maintenance_events", "sqlite_sequence")}


def mutate(store, action):
    if action == "create":
        store.ingest(TEXT, "other.md", sample_config("v2"), condition=WriteCondition.create())
    elif action == "replace":
        put(store, TEXT.replace("180天", "181天"), match(store))
    elif action == "delete":
        store.delete(NAME, condition=match(store))
    else:
        store.replace_access(NAME, [GRANT], condition=access_match(store))


def test_lifecycle_survives_deletion_and_restart_without_raw_content(store):
    actor = audit.AuditActor("api", "synthetic-admin")
    store.replace_access(NAME, [GRANT], condition=access_match(store), actor=actor)
    put(store, TEXT.replace("180天", "181天"), match(store), actor)
    store.delete(NAME, condition=match(store), actor=actor)
    events = KnowledgeStore(store.path).audit_page()["events"]
    assert [e["action"] for e in events] == ["source_created", "source_access_replaced", "source_replaced", "source_deleted"]
    assert [e["revision"] for e in events] == [1, 2, 3, 4]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert events[0]["actor_kind"] == "local" and events[0]["actor_id"] is None
    assert events[1]["actor_kind"] == "api" and events[1]["actor_id"] == actor.principal_id
    assert events[0]["before_state"] is None and events[-1]["after_state"] is None
    assert events[1]["after_state"]["readers"] == [GRANT.principal_id]
    assert events[2]["before_state"]["readers"] == [GRANT.principal_id] and events[2]["after_state"]["readers"] == []
    assert events[1]["before_state"]["source_etag"] == events[1]["after_state"]["source_etag"]
    assert events[1]["before_state"]["access_etag"] != events[1]["after_state"]["access_etag"]
    for event in events:
        assert datetime.fromisoformat(event["at_utc"].replace("Z", "+00:00")).utcoffset().total_seconds() == 0
    encoded = json.dumps(events, ensure_ascii=False)
    assert "180天" not in encoded and "181天" not in encoded and GRANT.credential_sha256 not in encoded
    assert not {"normalized_text", "question", "prompt", "token", "token_sha256"} & set(events[0])


@pytest.mark.parametrize("action", ["create", "replace", "delete", "access"])
@pytest.mark.parametrize("stage", ["before-insert", "after-insert"])
def test_audit_failure_rolls_back_all_data_and_sequence(store, monkeypatch, action, stage):
    baseline = snapshot(store)
    original = audit.append
    def fail(db, **kwargs):
        if stage == "after-insert":
            original(db, **kwargs)
        raise sqlite3.OperationalError("synthetic audit disk error")
    with monkeypatch.context() as context:
        context.setattr(audit, "append", fail)
        with pytest.raises(sqlite3.Error):
            mutate(store, action)
    assert snapshot(store) == baseline
    mutate(store, action)
    assert len(store.audit_page()["events"]) == 2


def test_noops_and_precondition_failures_do_not_fabricate_changes(store):
    before = snapshot(store)
    assert put(store, condition=match(store))["status"] == "unchanged"
    assert store.replace_access(NAME, [], condition=access_match(store))["status"] == "unchanged"
    with pytest.raises(WriteRejected):
        put(store)
    with pytest.raises(WriteRejected):
        store.delete(NAME, condition=WriteCondition.match('"' + "f"*64 + '"'))
    with pytest.raises(WriteRejected):
        store.replace_access(NAME, [GRANT], condition=match(store))
    assert snapshot(store) == before


@pytest.mark.parametrize("action", ["create", "replace", "delete", "access"])
def test_capacity_refuses_mutations_without_deleting_history(store, monkeypatch, action):
    before = snapshot(store)
    monkeypatch.setattr(audit, "MAX_EVENTS", 1)
    with pytest.raises(audit.AuditCapacityError):
        mutate(store, action)
    assert snapshot(store) == before
    assert put(store, condition=match(store))["status"] == "unchanged"
    assert store.audit_page()["events"]


def test_parallel_cas_losers_have_no_committed_event(store):
    condition = match(store)
    def change(i):
        try:
            return put(store, TEXT.replace("180天", f"{200+i}天"), condition)["status"]
        except WriteRejected:
            return "conflict"
    with ThreadPoolExecutor(8) as pool:
        outcomes = list(pool.map(change, range(8)))
    assert outcomes.count("indexed") == 1 and outcomes.count("conflict") == 7
    assert len(store.audit_page()["events"]) == 2 and store.revision() == 2


def test_distinct_source_commits_have_unique_ordered_revisions(store):
    def create(i):
        return KnowledgeStore(store.path).ingest(TEXT, f"parallel-{i}.md", sample_config("v2"), condition=WriteCondition.create())
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(create, range(6)))
    events = store.audit_page()["events"]
    assert [e["revision"] for e in events] == list(range(1, 8))
    assert len({e["source"] for e in events}) == 7


def test_concurrent_writers_cannot_overrun_audit_capacity(store, monkeypatch):
    monkeypatch.setattr(audit, "MAX_EVENTS", 2)
    def create(i):
        try:
            return store.ingest(TEXT, f"cap-{i}.md", sample_config("v2"), condition=WriteCondition.create())["status"]
        except audit.AuditCapacityError:
            return "audit_full"
    with ThreadPoolExecutor(4) as pool:
        outcomes = list(pool.map(create, range(8)))
    assert outcomes.count("indexed") == 1 and outcomes.count("audit_full") == 7
    assert len(store.audit_page()["events"]) == 2 and store.revision() == 2
    assert len({s["source"] for s in store.inventory()["sources"]}) == 2


def test_cli_capacity_failure_has_explicit_exit_and_preserves_data(store, monkeypatch, capsys):
    from compliance_rag.rag_cli import main
    before = snapshot(store)
    monkeypatch.setattr(audit, "MAX_EVENTS", 1)
    code = main(["remove", NAME, "--db", str(store.path), "--if-match", store.source(NAME)["source_etag"]])
    assert code == 5 and "audit_capacity_exhausted" in capsys.readouterr().out
    assert snapshot(store) == before


def test_pagination_excludes_new_commits_and_binds_database_instance(store):
    for _ in range(3):
        put(store, TEXT.replace("180天", str(store.revision())+"天"), match(store))
    first = store.audit_page(audit.AuditPageRequest(limit=1))
    mutate(store, "create")
    events, cursor = first["events"], first["next_cursor"]
    while cursor:
        page = KnowledgeStore(store.path).audit_page(audit.AuditPageRequest(limit=2, cursor=cursor))
        assert page["snapshot"] == first["snapshot"]
        events.extend(page["events"])
        cursor = page["next_cursor"]
    assert len(events) == 4 and len(store.audit_page()["events"]) == 5
    foreign = audit.AuditCursor(**{**first["next_cursor"], "instance_id": "0"*32})
    with pytest.raises(audit.AuditCursorError):
        store.audit_page(audit.AuditPageRequest(cursor=foreign))


@pytest.mark.parametrize("payload", [{"limit": True}, {"limit": 0}, {"limit": 101}, {"limit": "1"},
    {"cursor": {"instance_id": "a"*32, "after_seq": True, "through_seq": 5}},
    {"cursor": {"instance_id": "a"*32, "after_seq": 6, "through_seq": 5}}, {"actor_id": "FORGED"}])
def test_strict_bounded_page_schema(payload):
    with pytest.raises(ValidationError):
        audit.AuditPageRequest.model_validate(payload)


@pytest.mark.parametrize("actor", [{"kind": "api", "principal_id": "admin"}, "admin", True])
def test_local_callers_cannot_accidentally_supply_untyped_actors(store, actor):
    before = snapshot(store)
    with pytest.raises(ValueError):
        put(store, TEXT, match(store), actor)
    assert snapshot(store) == before


@pytest.mark.parametrize("kind,identifier", [("local", "admin"), ("api", None), ("api", "x\n"), ("unknown", None)])
def test_actor_invariants(kind, identifier):
    with pytest.raises(ValueError):
        audit.AuditActor(kind, identifier)


def test_actor_is_immutable():
    actor = audit.AuditActor("api", "synthetic-admin")
    with pytest.raises(FrozenInstanceError):
        actor.principal_id = "other"


def schema_three(path):
    """Independent pre-audit schema3; no current database relabeling."""
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE sources(name TEXT PRIMARY KEY,source_hash TEXT NOT NULL,config_hash TEXT NOT NULL,
              normalized_text TEXT NOT NULL,generation INTEGER NOT NULL DEFAULT 0,access_generation INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,source TEXT NOT NULL REFERENCES sources(name) ON DELETE CASCADE,
              standard_code TEXT NOT NULL,edition TEXT NOT NULL,level TEXT NOT NULL,security_class TEXT NOT NULL,
              title TEXT NOT NULL,content TEXT NOT NULL,metadata TEXT NOT NULL,vector BLOB);
            CREATE TABLE traces(trace_id TEXT PRIMARY KEY,revision INTEGER,status TEXT,reason TEXT,retrieval TEXT,
              generation TEXT,candidates INTEGER,citations INTEGER,total_ms REAL);
            CREATE TABLE source_grants(source TEXT NOT NULL REFERENCES sources(name) ON DELETE CASCADE,
              principal_id TEXT NOT NULL,credential_sha256 TEXT NOT NULL,PRIMARY KEY(source,principal_id));
        """)
        db.executemany("INSERT INTO settings VALUES (?,?)", [("schema","3"),("revision","9"),
            ("embedding","none"),("dimension","0"),("instance_id","e"*32)])
        db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?)", (NAME,"1"*64,"2"*64,TEXT,7,8))
        db.execute("INSERT INTO source_grants VALUES (?,?,?)", (NAME, GRANT.principal_id,GRANT.credential_sha256))
        db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?)", ("c"*64,NAME,"SYNTHETIC","v2","第三级",
            "安全物理环境","synthetic-title","synthetic-body","{}",b"retained-vector-bytes"))


def test_schema3_migration_preserves_source_etag_acl_rows_and_history_floor(tmp_path):
    path = tmp_path/"legacy.sqlite3"
    schema_three(path)
    with sqlite3.connect(path) as db:
        before = {table: list(db.execute(f"SELECT * FROM {table}")) for table in ("sources", "chunks", "source_grants")}
    expected = '"' + sha(canonical_json(["e"*32,NAME,7,"1"*64,"2"*64]).encode()) + '"'
    store = KnowledgeStore(path)
    assert store.source(NAME)["source_etag"] == expected and store.revision() == 9
    assert store.audit_page()["snapshot"]["audit_start_revision"] == 9
    assert store.audit_page()["events"] == []  # No fabricated pre-upgrade actor history.
    with store.connect() as db:
        for table, rows in before.items():
            assert [tuple(r) for r in db.execute(f"SELECT * FROM {table}")] == rows
    store.delete(NAME, condition=match(store))
    assert store.audit_page()["events"][0]["revision"] == 10


def test_migration_failure_rolls_back_table_floor_and_version(tmp_path, monkeypatch):
    path = tmp_path/"legacy.sqlite3"
    schema_three(path)
    original = audit.migrate
    def fail(db):
        original(db)
        raise RuntimeError("synthetic migration failure")
    with monkeypatch.context() as context:
        context.setattr(audit, "migrate", fail)
        with pytest.raises(RuntimeError):
            KnowledgeStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM settings WHERE key='schema'").fetchone()[0] == "3"
        assert db.execute("SELECT name FROM sqlite_master WHERE name='maintenance_events'").fetchone() is None
        assert db.execute("SELECT value FROM settings WHERE key='audit_start_revision'").fetchone() is None
    assert KnowledgeStore(path).revision() == 9


def test_concurrent_migration_creates_one_empty_ledger(tmp_path):
    path = tmp_path/"legacy.sqlite3"
    schema_three(path)
    with ThreadPoolExecutor(4) as pool:
        pages = list(pool.map(lambda _: KnowledgeStore(path).audit_page(), range(8)))
    assert pages == [pages[0]]*8 and pages[0]["events"] == []


def test_schema4_missing_ledger_fails_closed(store):
    with store.connect() as db:
        db.execute("DROP TABLE maintenance_events")
    with pytest.raises(sqlite3.Error):
        KnowledgeStore(store.path)


@pytest.fixture
def client(store):
    # Explicit synthetic credentials; no deployment defaults.
    tokens = {role: "synthetic_audit_" + role + "_" + "q"*43 for role in ("reader", "analyst", "maintainer")}
    settings = AccessSettings(principals=[{"id": "synthetic-"+role, "role": role,
        "token_sha256": hashlib.sha256(value.encode()).hexdigest()} for role, value in tokens.items()])
    app = create_app(AccessPolicy(settings))
    app.state.rag_service = RagService(store)
    with TestClient(app, headers={"Authorization": "Bearer " + tokens["maintainer"]}) as client:
        client.synthetic_tokens = tokens
        yield client


def test_api_attribution_ignores_client_header_and_rejects_body_forgery(client, store):
    payload = {"text": TEXT.replace("180天", "181天"), "source_name": NAME, "config": sample_config("v2").model_dump()}
    tag = store.source(NAME)["source_etag"]
    assert client.post("/v1/library/ingest", json={**payload, "actor": "FORGED_MARKER"},
        headers={"If-Match": tag}).status_code == 422
    response = client.post("/v1/library/ingest", json=payload, headers={"If-Match": tag, "X-Actor-ID": "FORGED_MARKER"})
    assert response.status_code == 200
    events = client.post("/v1/audit/events", json={}).json()["events"]
    assert events[-1]["actor_id"] == "synthetic-maintainer"
    assert "FORGED_MARKER" not in json.dumps(events)
    assert client.get("/health").json()["version"] == __version__


@pytest.mark.parametrize("role,status", [(None,401), ("reader",403), ("analyst",403), ("maintainer",200)])
def test_api_events_are_private_maintainer_only(client, store, role, status):
    client.headers.clear()
    headers = {"Authorization": "Bearer " + client.synthetic_tokens[role]} if role else {}
    response = client.post("/v1/audit/events", json={}, headers=headers)
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    assert len(store.audit_page()["events"]) == 1


def test_api_cursor_schema_and_foreign_database_errors(client):
    assert client.post("/v1/audit/events", json={"limit": True}).status_code == 422
    response = client.post("/v1/audit/events", json={"cursor": {"instance_id": "0"*32, "after_seq": 0, "through_seq": 1}})
    assert response.status_code == 409 and "0"*32 not in response.text


def test_api_audit_failure_refuses_without_changing_knowledge(client, store, monkeypatch):
    before = snapshot(store)
    monkeypatch.setattr(audit, "MAX_EVENTS", 1)
    response = client.post("/v1/library/remove", json={"source_name": NAME}, headers={"If-Match": store.source(NAME)["source_etag"]})
    assert response.status_code == 503 and "audit_capacity_exhausted" in response.text
    assert snapshot(store) == before


def test_response_delivery_failure_does_not_erase_committed_event(client, store):
    async def receive():
        return {"type":"http.request", "body":json.dumps({"source_name":NAME}).encode(), "more_body":False}
    async def send(message):
        raise OSError("synthetic downstream disconnect")
    import asyncio
    scope = {"type":"http", "method":"POST", "path":"/v1/library/remove", "query_string":b"",
        "headers":[(b"host",b"testserver"),(b"content-type",b"application/json"),
                   (b"authorization",("Bearer "+client.synthetic_tokens["maintainer"]).encode()),
                   (b"if-match",store.source(NAME)["source_etag"].encode())]}
    with pytest.raises(OSError):
        asyncio.run(client.app(scope, receive, send))
    assert store.source(NAME) is None
    assert store.audit_page()["events"][-1]["action"] == "source_deleted"


def test_fixed_demo_report():
    from compliance_rag.audit_demo import demonstrate
    expected = json.loads(Path("examples/evaluation/maintenance-audit.json").read_text(encoding="utf-8"))
    assert demonstrate() == expected


def test_exporter_uses_actual_asgi_snapshot(client, store):
    from compliance_rag.audit_export import collect
    for i in range(51):
        put(store, TEXT.replace("180天", f"{500+i}天"), match(store))
    exported = collect(client, page_size=50)
    assert len(exported["events"]) == 52  # Actual 50+2 page traversal, not just first-page copying.
    assert exported["events"] == store.audit_page(audit.AuditPageRequest(limit=100))["events"]
    assert exported["snapshot"] == store.audit_page()["snapshot"]


@pytest.mark.parametrize("mutation", ["duplicate-sequence", "future-sequence", "changed-snapshot", "stuck-cursor", "short-terminal", "too-large"])
def test_exporter_rejects_malformed_or_inconsistent_pages(mutation):
    import httpx
    from compliance_rag.audit_export import collect
    snap = {"instance_id": "a"*32, "through_seq": 2, "audit_start_revision": 0}
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if mutation == "too-large":
            return httpx.Response(200, content=b"x"*524289)
        if calls == 1:
            data = {"snapshot": snap, "events": [{"seq": 1}],
                "next_cursor": {"instance_id": "a"*32, "after_seq": 1, "through_seq": 2}}
            if mutation == "stuck-cursor":
                data["next_cursor"]["after_seq"] = 0
            if mutation == "short-terminal":
                data["next_cursor"] = None
        else:
            data = {"snapshot": {**snap, "instance_id": "b"*32} if mutation == "changed-snapshot" else snap,
                "events": [{"seq": 1 if mutation == "duplicate-sequence" else 3 if mutation == "future-sequence" else 2}], "next_cursor": None}
        return httpx.Response(200, json=data)
    with httpx.Client(base_url="http://testserver", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            collect(client)


def test_export_cli_keeps_credentials_and_bad_url_out_of_errors(tmp_path, capsys, monkeypatch):
    from compliance_rag.audit_export import main
    monkeypatch.setenv("RAG_API_TOKEN", "PRIVATE_MARKER")
    out = tmp_path/"private.json"
    assert main(["--out", str(out), "--url", "https://PRIVATE_MARKER.invalid"]) == 2
    assert "PRIVATE_MARKER" not in capsys.readouterr().out and not out.exists()
